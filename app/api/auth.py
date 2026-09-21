# app/api/auth.py
import os

from fastapi import APIRouter, BackgroundTasks, HTTPException, Body, Response, Request, Depends
from app.db import get_connection
from app.auth.security import (
    create_access_token, decode_token, hash_password, verify_password,
)
from app.auth.deps import (
    get_current_user, COOKIE_NAME, COOKIE_SECURE, COOKIE_MAX_AGE_SECONDS,
    set_session_cookie, clear_session_cookie, revoke_session, forget_user_sessions,
)
from app.auth.rate_limit import (
    enforce_login_rate_limit,
    enforce_forgot_password_rate_limit,
    enforce_reset_password_rate_limit,
)
from app.email import mailer
import bcrypt
import hashlib
import jwt
import logging
import re
import secrets
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["Auth"])

RESET_TOKEN_TTL_MINUTES = 30
# Minimum gap between reset emails for the SAME account (per-IP limiting
# alone doesn't stop a distributed mail-bomb of one victim).
RESET_REQUEST_COOLDOWN_SECONDS = 60

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_dummy_hash_cache = None

_FORGOT_RESPONSE = {
    "status":  "ok",
    "message": "If that account exists, a password reset has been initiated. "
               "Check your email, or contact an administrator if you don't "
               "receive it shortly.",
    "expires_in_minutes": RESET_TOKEN_TTL_MINUTES,
}


# COOKIE_SECURE / COOKIE_MAX_AGE_SECONDS live in app/auth/deps.py now (shared
# with app/api/sso.py); re-exported here for anything importing them from this module.


def _verify_password(plain: str, stored: str) -> bool:
    return verify_password(plain, stored)


def _hash_password(plain: str) -> str:
    return hash_password(plain)


def _dummy_hash() -> str:
    """Hash to burn a bcrypt verify on when the username doesn't exist, so
    unknown-user and wrong-password logins take the same time."""
    global _dummy_hash_cache
    if _dummy_hash_cache is None:
        _dummy_hash_cache = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt()).decode()
    return _dummy_hash_cache


def _text_field(payload: dict, name: str) -> str:
    value = payload.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{name} must be a string")
    return value.strip()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
    username = _text_field(payload, "username")
    password = _text_field(payload, "password")

    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")

    # SECURITY: this endpoint had no rate limiting at all -- unlimited
    # password guesses against any account. Checked AFTER validating
    # username/password are present (no point spending a rate-limit
    # slot on an obviously-malformed request) but BEFORE the DB lookup
    # (no point querying the DB for a request we're about to reject).
    enforce_login_rate_limit(request, username)

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id, username, role, token_version, password AS pw "
                "FROM users WHERE username = %s AND active = 1",
                (username,)
            )
            user = cursor.fetchone()
        finally:
            cursor.close()
    finally:
        conn.close()

    if not user:
        _verify_password(password, _dummy_hash())   # constant-ish time vs. a real user
        _write_audit(username, "Login failed", request=request,
                      payload={"username": username, "reason": "unknown username"})
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not _verify_password(password, user["pw"]):
        _write_audit(username, "Login failed", request=request,
                      payload={"username": username, "reason": "incorrect password"})
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(user["id"], user["username"], user["role"],
                                 token_version=user["token_version"])
    set_session_cookie(response, token)

    _write_audit(user["username"], "Login successful", role=user["role"].upper(), request=request,
                  payload={"username": user["username"]})

    return {
        "id":       user["id"],
        "username": user["username"],
        "role":     user["role"],
    }


@router.post("/logout")
def logout(request: Request, response: Response):
    # Best-effort actor lookup + server-side revocation. Logout must still
    # succeed (200 + cookie cleared) even with no/expired/invalid session
    # or a DB hiccup, so nothing in here ever raises.
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            claims = decode_token(token)
        except jwt.PyJWTError:
            claims = None   # expired/invalid: nothing to revoke
        except Exception as e:
            logger.warning(f"Logout: could not decode session token: {type(e).__name__}")
            claims = None
        if claims:
            try:
                revoke_session(claims["id"], claims.get("jti"), claims["exp"])
            except Exception as e:
                logger.warning(f"Logout: could not revoke session server-side: {e}")
            _write_audit(claims["username"], "Logout", role=claims["role"].upper(), request=request,
                          payload={"username": claims["username"]})
    clear_session_cookie(response)
    return {"status": "ok"}


@router.get("/me")
def me(current_user: dict = Depends(get_current_user)):
    from app.auth import authorization as authz
    return {**current_user, "scope": authz.serialize_scope(current_user)}


@router.post("/change-password")
def change_password(response: Response, payload: dict = Body(...),
                    current_user: dict = Depends(get_current_user)):
    """
    Self-service change password — requires the current password AND a
    valid session. Always acts on the SESSION's identity, never a
    client-supplied username, so a logged-in user can never target
    another account's password by passing a different username field.
    Every OTHER session of this user (and any outstanding reset link) is
    invalidated; the caller's own session is re-issued so they stay in.
    """
    username     = current_user["username"]
    current_pw   = _text_field(payload, "current_password")
    new_pw       = _text_field(payload, "new_password")

    if not current_pw or not new_pw:
        raise HTTPException(status_code=400, detail="current_password and new_password are required")
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id, password AS pw FROM users WHERE username = %s AND active = 1",
                (username,),
            )
            user = cursor.fetchone()

            if not user or not _verify_password(current_pw, user["pw"]):
                raise HTTPException(status_code=401, detail="Current password is incorrect")

            cursor.execute(
                "UPDATE users SET password = %s, token_version = token_version + 1 WHERE id = %s",
                (_hash_password(new_pw), user["id"]),
            )
            cursor.execute("DELETE FROM password_reset_tokens WHERE user_id = %s", (user["id"],))
            cursor.execute("SELECT token_version FROM users WHERE id = %s", (user["id"],))
            new_tv = int(cursor.fetchone()["token_version"] or 0)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
    finally:
        conn.close()

    forget_user_sessions(user["id"])
    set_session_cookie(response, create_access_token(
        user["id"], username, current_user["role"], token_version=new_tv))

    _write_audit(username, "Password changed", role=current_user["role"].upper(),
                  payload={"username": username})
    return {"status": "ok"}


def _log_undelivered_token(username: str, token: str, why: str) -> None:
    """A reset token that couldn't be emailed. The token itself is only
    written to the log when RESET_TOKEN_LOG_FALLBACK=true (old behaviour);
    by default the log gets a fingerprint and admins reset via the users API."""
    if os.getenv("RESET_TOKEN_LOG_FALLBACK", "false").strip().lower() == "true":
        logger.warning(f"Password reset for '{username}': {why}; token={token} "
                        f"(server log only, never returned via API)")
    else:
        logger.warning(f"Password reset for '{username}': {why}; token not logged "
                        f"(fingerprint {_token_hash(token)[:8]}). Have an admin reset the "
                        f"password via the admin users API, or set RESET_TOKEN_LOG_FALLBACK=true.")


def _send_reset_email(username: str, to_addr: str, token: str) -> None:
    """Runs as a BackgroundTask: SMTP can block up to 30 s and must not sit
    in the request path (it also made 'account exists' observable by timing)."""
    reset_link = f"{mailer.get_public_app_url()}/reset-password?token={token}"
    sent = mailer.send_email(
        to_addr=to_addr,
        subject="CloudOps password reset",
        body_text=(
            f"A password reset was requested for the account '{username}'.\n\n"
            f"Reset your password (link valid {RESET_TOKEN_TTL_MINUTES} minutes):\n{reset_link}\n\n"
            f"If you didn't request this, you can ignore this email.\n"
        ),
    )
    if not sent:
        _log_undelivered_token(username, token, "could not be emailed (send failed)")


@router.post("/forgot-password")
def forgot_password(request: Request, background_tasks: BackgroundTasks,
                    payload: dict = Body(...)):
    """
    Request a password reset. Always returns a generic success message
    (never reveals whether the username exists). If the account is
    real, a one-time token valid for 30 minutes is created (stored only
    as a SHA-256 hash) and either emailed (if SMTP is configured) or
    reported in the server log per _log_undelivered_token(). The token
    is NEVER returned in this response; that was fixed in patch 0001/0002
    of the 2026-09-12 audit after it was found to be a full
    account-takeover vector.
    """
    username = _text_field(payload, "username")
    if not username:
        raise HTTPException(status_code=400, detail="username is required")

    # SECURITY: unlimited calls here previously meant free-form DB
    # lookups (and, when SMTP is configured, free email sends) at
    # whatever rate an attacker chose. Checked after the basic
    # "username present" validation, before any DB work.
    enforce_forgot_password_rate_limit(request)

    token = None
    user = None
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id, email, role FROM users WHERE username = %s AND active = 1",
                (username,),
            )
            user = cursor.fetchone()

            if user:
                # Per-account cooldown: at most one reset mail per minute per user.
                cursor.execute(
                    "SELECT 1 FROM password_reset_tokens WHERE user_id = %s "
                    "AND created_at > (NOW() - INTERVAL %s SECOND) LIMIT 1",
                    (user["id"], RESET_REQUEST_COOLDOWN_SECONDS),
                )
                if cursor.fetchone() is None:
                    token      = secrets.token_urlsafe(32)
                    expires_at = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)
                    cursor.execute("DELETE FROM password_reset_tokens WHERE user_id = %s", (user["id"],))
                    cursor.execute(
                        "INSERT INTO password_reset_tokens (user_id, token, expires_at) "
                        "VALUES (%s, %s, %s)",
                        (user["id"], _token_hash(token), expires_at),   # hashed at rest
                    )
                    conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
    finally:
        conn.close()

    # Same response either way so usernames can't be enumerated.
    if not user or token is None:
        return dict(_FORGOT_RESPONSE)

    _write_audit(username, "Password reset requested", role=user["role"].upper(), request=request,
                  payload={"username": username})

    # SECURITY: the reset token must NEVER be returned in this API
    # response (unauthenticated endpoint => one-request account takeover).
    # If mail is configured and an address is on file, email the link
    # (in the background). Otherwise an admin sets the password directly
    # via the admin users API.
    if user.get("email") and mailer.is_configured():
        background_tasks.add_task(_send_reset_email, username, user["email"], token)
    else:
        _log_undelivered_token(username, token, "SMTP is not configured and/or no email is on file")

    return dict(_FORGOT_RESPONSE)


@router.post("/reset-password")
def reset_password(request: Request, payload: dict = Body(...)):
    """Complete a reset using the token from /forgot-password."""
    token  = _text_field(payload, "token")
    new_pw = _text_field(payload, "new_password")

    if not token or not new_pw:
        raise HTTPException(status_code=400, detail="token and new_password are required")

    # SECURITY: defense-in-depth against brute-forcing the token itself.
    enforce_reset_password_rate_limit(request)
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    # Tokens made by /forgot-password are stored as SHA-256(token). Tokens
    # inserted raw by app/api/admin/users.py (welcome emails) are still matched
    # by their raw value -- but never when the submitted value looks like a
    # stored hash, or a DB reader could replay a hash as a token.
    candidates = [_token_hash(token)]
    if not _HEX64.match(token):
        candidates.append(token)
    placeholders = ",".join(["%s"] * len(candidates))

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT prt.id AS token_id, prt.user_id, prt.expires_at, u.username, u.role
                FROM password_reset_tokens prt
                JOIN users u ON u.id = prt.user_id
                WHERE prt.token IN ({placeholders}) AND u.active = 1
                """,
                tuple(candidates),
            )
            row = cursor.fetchone()

            if not row or row["expires_at"] < datetime.utcnow():
                raise HTTPException(status_code=400, detail="Reset token is invalid or has expired")

            # Single use, race-safe: only the request whose DELETE removes the
            # row may proceed; a concurrent replay sees rowcount 0.
            cursor.execute("DELETE FROM password_reset_tokens WHERE id = %s", (row["token_id"],))
            if cursor.rowcount != 1:
                conn.rollback()
                raise HTTPException(status_code=400, detail="Reset token is invalid or has expired")

            cursor.execute(
                "UPDATE users SET password = %s, token_version = token_version + 1 WHERE id = %s",
                (_hash_password(new_pw), row["user_id"]),
            )
            cursor.execute("DELETE FROM password_reset_tokens WHERE user_id = %s", (row["user_id"],))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
    finally:
        conn.close()

    forget_user_sessions(row["user_id"])
    _write_audit(row["username"], "Password reset completed", role=row["role"].upper(), request=request,
                  payload={"username": row["username"]})
    return {"status": "ok"}
