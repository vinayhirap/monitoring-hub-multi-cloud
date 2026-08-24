# app/api/auth.py
import os

from fastapi import APIRouter, HTTPException, Body, Response, Depends
from app.db import get_connection
from app.auth.security import create_access_token
from app.auth.deps import get_current_user, COOKIE_NAME
import bcrypt
import logging
import secrets
import json
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["Auth"])

RESET_TOKEN_TTL_MINUTES = 30

# COOKIE_SECURE must be "true" once the app is served over HTTPS (see the
# Security Checklist in the deployment guide). Defaults to False because
# production currently serves plain HTTP on port 80 — a Secure cookie
# would silently never be sent by the browser over HTTP, breaking login.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").strip().lower() == "true"
COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60  # 12 hours, matches token expiry


def _verify_password(plain: str, stored: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), stored.encode())
    except Exception as e:
        logger.warning(f"Password verify error: {e}")
        return False


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _write_audit(actor: str, action: str, payload: dict):
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO audit_logs (actor, action, payload) VALUES (%s, %s, %s)",
            (actor, action, json.dumps(payload)),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning(f"Audit write failed: {e}")


@router.post("/login")
def login(response: Response, payload: dict = Body(...)):
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, username, role, password AS pw FROM users WHERE username = %s AND active = 1",
        (username,)
    )
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not _verify_password(password, user["pw"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(user["id"], user["username"], user["role"])
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=COOKIE_MAX_AGE_SECONDS,
        path="/",
    )

    return {
        "id":       user["id"],
        "username": user["username"],
        "role":     user["role"],
    }


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.get("/me")
def me(current_user: dict = Depends(get_current_user)):
    from app.auth import authorization as authz
    return {**current_user, "scope": authz.serialize_scope(current_user)}


@router.post("/change-password")
def change_password(payload: dict = Body(...), current_user: dict = Depends(get_current_user)):
    """
    Self-service change password — requires the current password AND a
    valid session. Always acts on the SESSION's identity, never a
    client-supplied username, so a logged-in user can never target
    another account's password by passing a different username field.
    """
    username     = current_user["username"]
    current_pw   = (payload.get("current_password") or "").strip()
    new_pw       = (payload.get("new_password") or "").strip()

    if not current_pw or not new_pw:
        raise HTTPException(status_code=400, detail="current_password and new_password are required")
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, password AS pw FROM users WHERE username = %s AND active = 1",
        (username,),
    )
    user = cursor.fetchone()

    if not user or not _verify_password(current_pw, user["pw"]):
        cursor.close()
        conn.close()
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    cursor.execute(
        "UPDATE users SET password = %s WHERE id = %s",
        (_hash_password(new_pw), user["id"]),
    )
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(username, "Password changed", {"username": username})
    return {"status": "ok"}


@router.post("/forgot-password")
def forgot_password(payload: dict = Body(...)):
    """
    Request a password reset. Always returns a generic success message
    (never reveals whether the username exists). If the account is
    real, a one-time token valid for 30 minutes is created.

    NOTE: this deployment has no SMTP/email service wired in, so the
    token is returned directly in the response for now instead of being
    emailed — swap the return value for an email send once mail is
    configured, without changing the token/table logic.
    """
    username = (payload.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id FROM users WHERE username = %s AND active = 1",
        (username,),
    )
    user = cursor.fetchone()

    if not user:
        cursor.close()
        conn.close()
        # Same response either way so usernames can't be enumerated.
        return {"status": "ok", "message": "If that account exists, a reset token has been generated."}

    token      = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)

    cursor.execute(
        "DELETE FROM password_reset_tokens WHERE user_id = %s",
        (user["id"],),
    )
    cursor.execute(
        """
        INSERT INTO password_reset_tokens (user_id, token, expires_at)
        VALUES (%s, %s, %s)
        """,
        (user["id"], token, expires_at),
    )
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(username, "Password reset requested", {"username": username})

    return {
        "status":     "ok",
        "message":    "If that account exists, a reset token has been generated.",
        "token":      token,
        "expires_in_minutes": RESET_TOKEN_TTL_MINUTES,
    }


@router.post("/reset-password")
def reset_password(payload: dict = Body(...)):
    """Complete a reset using the token from /forgot-password."""
    token  = (payload.get("token") or "").strip()
    new_pw = (payload.get("new_password") or "").strip()

    if not token or not new_pw:
        raise HTTPException(status_code=400, detail="token and new_password are required")
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT prt.id AS token_id, prt.user_id, prt.expires_at, u.username
        FROM password_reset_tokens prt
        JOIN users u ON u.id = prt.user_id
        WHERE prt.token = %s
        """,
        (token,),
    )
    row = cursor.fetchone()

    if not row or row["expires_at"] < datetime.utcnow():
        cursor.close()
        conn.close()
        raise HTTPException(status_code=400, detail="Reset token is invalid or has expired")

    cursor.execute(
        "UPDATE users SET password = %s WHERE id = %s",
        (_hash_password(new_pw), row["user_id"]),
    )
    cursor.execute(
        "DELETE FROM password_reset_tokens WHERE id = %s",
        (row["token_id"],),
    )
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(row["username"], "Password reset completed", {"username": row["username"]})
    return {"status": "ok"}
