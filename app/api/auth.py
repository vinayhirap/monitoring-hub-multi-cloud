# app/api/auth.py
import os

from fastapi import APIRouter, HTTPException, Body, Response, Request, Depends
from app.db import get_connection
from app.auth.security import create_access_token
from app.auth.deps import get_current_user, COOKIE_NAME
from app.auth.rate_limit import (
    enforce_login_rate_limit,
    enforce_forgot_password_rate_limit,
    enforce_reset_password_rate_limit,
)
from app.email import mailer
import bcrypt
import logging
import secrets
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


from app.audit import write_audit as _write_audit
# NOTE: previously a local copy taking a raw `payload` dict with no
# {"role": ...} key at all -- so "Password changed"/"Password reset
# requested"/"Password reset completed" audit rows never carried the
# actor's actual role, and (see Compliance.jsx) the UI badge silently
# defaulted a missing role to "ADMIN" regardless of who the actor
# really was. Also: successful/failed logins and logouts were never
# audited AT ALL before this fix -- for a NOC/compliance tool, that's
# the single biggest gap in the audit trail, bigger than any of the
# role-misattribution bugs fixed elsewhere alongside this change.


@router.post("/login")
def login(request: Request, response: Response, payload: dict = Body(...)):
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")

    # SECURITY: this endpoint had no rate limiting at all -- unlimited
    # password guesses against any account. Checked AFTER validating
    # username/password are present (no point spending a rate-limit
    # slot on an obviously-malformed request) but BEFORE the DB lookup
    # (no point querying the DB for a request we're about to reject).
    enforce_login_rate_limit(request, username)

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
        _write_audit(username, "Login failed", request=request,
                      payload={"username": username, "reason": "unknown username"})
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not _verify_password(password, user["pw"]):
        _write_audit(username, "Login failed", request=request,
                      payload={"username": username, "reason": "incorrect password"})
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

    _write_audit(user["username"], "Login successful", role=user["role"].upper(), request=request,
                  payload={"username": user["username"]})

    return {
        "id":       user["id"],
        "username": user["username"],
        "role":     user["role"],
    }


@router.post("/logout")
def logout(request: Request, response: Response):
    # Best-effort actor lookup for the audit entry only -- logout must
    # still succeed (200 + cookie cleared) even with no/expired/invalid
    # session, so this never raises the way get_current_user() would.
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            from app.auth.security import decode_token
            claims = decode_token(token)
            _write_audit(claims["username"], "Logout", role=claims["role"].upper(), request=request,
                          payload={"username": claims["username"]})
        except Exception:
            pass
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

    _write_audit(username, "Password changed", role=current_user["role"].upper(),
                  payload={"username": username})
    return {"status": "ok"}


@router.post("/forgot-password")
def forgot_password(request: Request, payload: dict = Body(...)):
    """
    Request a password reset. Always returns a generic success message
    (never reveals whether the username exists). If the account is
    real, a one-time token valid for 30 minutes is created and either
    emailed (if SMTP is configured) or logged server-side only -- see
    mailer.send_email()'s call below. The token is NEVER returned in
    this response; that was fixed in patch 0001/0002 of the 2026-09-12
    audit after it was found to be a full account-takeover vector.
    """
    username = (payload.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")

    # SECURITY: unlimited calls here previously meant free-form DB
    # lookups (and, when SMTP is configured, free email sends) at
    # whatever rate an attacker chose. Checked after the basic
    # "username present" validation, before any DB work.
    enforce_forgot_password_rate_limit(request)

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, email, role FROM users WHERE username = %s AND active = 1",
        (username,),
    )
    user = cursor.fetchone()

    if not user:
        cursor.close()
        conn.close()
        # Same response either way so usernames can't be enumerated.
        return {
            "status":  "ok",
            "message": "If that account exists, a password reset has been initiated. "
                        "Check your email, or contact an administrator if you don't "
                        "receive it shortly.",
            "expires_in_minutes": RESET_TOKEN_TTL_MINUTES,
        }

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

    _write_audit(username, "Password reset requested", role=user["role"].upper(), request=request,
                  payload={"username": username})

    # SECURITY: the reset token must NEVER be returned in this API
    # response. This endpoint is intentionally unauthenticated (anyone
    # can call it for any username, so the not-found path can't be used
    # to enumerate accounts) -- if the token itself came back in the
    # JSON, that same unauthenticated caller could reset ANY account's
    # password without ever proving ownership of the account's inbox,
    # i.e. a one-request full account takeover for every username in
    # the system. The previous "no SMTP configured -> return the token
    # directly" fallback made that the default behavior of this
    # deployment (SMTP_HOST is blank in .env.production.example).
    #
    # If mail is configured and an address is on file, email the link.
    # Otherwise the token is written ONLY to the server-side
    # application log (readable by someone with shell/log access to
    # the box, not by an anonymous HTTP caller) and an admin can always
    # set a user's password directly via the admin users API/UI
    # regardless of SMTP configuration.
    if user.get("email") and mailer.is_configured():
        reset_link = f"{mailer.get_public_app_url()}/reset-password?token={token}"
        sent = mailer.send_email(
            to_addr=user["email"],
            subject="CloudOps password reset",
            body_text=(
                f"A password reset was requested for the account '{username}'.\n\n"
                f"Reset your password (link valid {RESET_TOKEN_TTL_MINUTES} minutes):\n{reset_link}\n\n"
                f"If you didn't request this, you can ignore this email.\n"
            ),
        )
        if not sent:
            logger.warning(
                f"Password reset for '{username}' could not be emailed "
                f"(send failed); token={token} (server log only, never returned via API)"
            )
    else:
        logger.warning(
            f"Password reset requested for '{username}' but SMTP is not configured "
            f"and/or no email is on file; token={token} (server log only, never "
            f"returned via API). Configure SMTP_HOST or have an admin reset the "
            f"password directly via the admin users API."
        )

    return {
        "status":  "ok",
        "message": "If that account exists, a password reset has been initiated. "
                    "Check your email, or contact an administrator if you don't "
                    "receive it shortly.",
        "expires_in_minutes": RESET_TOKEN_TTL_MINUTES,
    }


@router.post("/reset-password")
def reset_password(request: Request, payload: dict = Body(...)):
    """Complete a reset using the token from /forgot-password."""
    token  = (payload.get("token") or "").strip()
    new_pw = (payload.get("new_password") or "").strip()

    if not token or not new_pw:
        raise HTTPException(status_code=400, detail="token and new_password are required")

    # SECURITY: defense-in-depth against brute-forcing the token itself.
    enforce_reset_password_rate_limit(request)
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT prt.id AS token_id, prt.user_id, prt.expires_at, u.username, u.role
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

    _write_audit(row["username"], "Password reset completed", role=row["role"].upper(), request=request,
                  payload={"username": row["username"]})
    return {"status": "ok"}
