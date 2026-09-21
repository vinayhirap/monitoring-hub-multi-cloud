"""
app/auth/deps.py

FastAPI dependencies for authenticated routes. Reads the session from
the httpOnly `mh_session` cookie set by POST /api/auth/login — never
trusts any client-supplied identity field (body params, headers, etc.).

The JWT alone is NOT enough to authenticate a request (audit B01): it is
a 12 h bearer token, so get_current_user() also re-checks the user's
live state in MySQL — account still active, role current, token_version
unchanged (bumped on password change/reset), and this token's `jti` not
in revoked_sessions (written by POST /api/auth/logout). The lookup is
cached per (user, jti) for SESSION_STATE_CACHE_SECONDS (default 15) per
worker, so the cost is ~1 indexed query per session per 15 s, and a
deactivation/logout takes effect within that window on every worker.

require_account_access() from the previous (dead, unused) version of
this file is intentionally NOT carried forward here — cloud/account/
region SCOPE enforcement is a separate, later authorization layer
(Phase 1+ of the RBAC plan), not something to half-implement now.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
import jwt

from app.auth.security import decode_token
from app.db import get_connection

try:  # kept optional so unit tests can stub app.db without the connector
    from mysql.connector import errors as _mysql_errors
except Exception:  # pragma: no cover
    _mysql_errors = None

logger = logging.getLogger(__name__)

COOKIE_NAME = "mh_session"

# COOKIE_SECURE must be "true" once the app is served over HTTPS (see the
# Security Checklist in the deployment guide). Defaults to False because
# production currently serves plain HTTP on port 80 — a Secure cookie
# would silently never be sent by the browser over HTTP, breaking login.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").strip().lower() == "true"
COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60  # 12 hours, matches token expiry

_STATE_TTL_SECONDS = float(os.getenv("SESSION_STATE_CACHE_SECONDS", "15"))
_STATE_CACHE_MAX = 2000
_state_cache: dict = {}          # (user_id, jti) -> (monotonic_ts, row_or_None)
_state_lock = threading.Lock()
_LEGACY = object()               # sentinel: auth-hardening schema not migrated yet
_warned_schema = False


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True, secure=COOKIE_SECURE,
        samesite="lax", max_age=COOKIE_MAX_AGE_SECONDS, path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", secure=COOKIE_SECURE,
                           httponly=True, samesite="lax")


def _schema_missing(exc: Exception) -> bool:
    # 1054 = unknown column, 1146 = table doesn't exist (migration 052 not applied)
    return (_mysql_errors is not None
            and isinstance(exc, _mysql_errors.ProgrammingError)
            and getattr(exc, "errno", None) in (1054, 1146))


def _fetch_state(user_id: int, jti):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT u.role, u.active, u.token_version, "
                "       EXISTS(SELECT 1 FROM revoked_sessions r WHERE r.jti = %s) AS revoked "
                "FROM users u WHERE u.id = %s",
                (jti or "", user_id),
            )
            return cursor.fetchone()
        finally:
            cursor.close()
    finally:
        conn.close()


def forget_user_sessions(user_id: int) -> None:
    """Drop this worker's cached state for a user (other worker: <= TTL)."""
    with _state_lock:
        for key in [k for k in _state_cache if k[0] == user_id]:
            _state_cache.pop(key, None)


def revoke_session(user_id: int, jti, exp_ts: int) -> None:
    """Server-side logout: blacklist one token's jti until it would expire anyway."""
    if not jti:
        return  # pre-B01 token: no jti to revoke; it simply expires
    expires_at = datetime.fromtimestamp(exp_ts, timezone.utc).replace(tzinfo=None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT IGNORE INTO revoked_sessions (jti, user_id, expires_at) VALUES (%s, %s, %s)",
                (jti, user_id, expires_at),
            )
            cursor.execute("DELETE FROM revoked_sessions WHERE expires_at < %s", (now,))
            conn.commit()
        finally:
            cursor.close()
    finally:
        conn.close()
    forget_user_sessions(user_id)


def validate_session_claims(claims: dict) -> str:
    """
    Server-side session check for already-decoded, signature-valid claims.
    Returns the user's CURRENT role; raises HTTPException 401 if the
    session is no longer valid, 503 if the state lookup itself fails.
    Also usable by non-HTTP entry points (e.g. the /ws endpoint).
    """
    global _warned_schema
    key = (claims["id"], claims.get("jti"))
    now = time.monotonic()
    with _state_lock:
        hit = _state_cache.get(key)
    if hit and now - hit[0] < _STATE_TTL_SECONDS:
        state = hit[1]
    else:
        try:
            state = _fetch_state(claims["id"], claims.get("jti"))
        except Exception as e:
            if _schema_missing(e):
                if not _warned_schema:
                    _warned_schema = True
                    logger.error("Session revocation disabled: run migrate.py apply "
                                 "--all-pending (migration 052) — %s", e)
                return claims["role"]
            logger.exception("Session state lookup failed")
            raise HTTPException(status_code=503, detail="Session check temporarily unavailable")
        with _state_lock:
            if len(_state_cache) >= _STATE_CACHE_MAX:
                cutoff = now - _STATE_TTL_SECONDS
                for k in [k for k, v in _state_cache.items() if v[0] < cutoff]:
                    _state_cache.pop(k, None)
                if len(_state_cache) >= _STATE_CACHE_MAX:
                    _state_cache.clear()
            _state_cache[key] = (now, state)

    if (not state or not state["active"] or state["revoked"]
            or int(state["token_version"] or 0) != int(claims.get("tv", 0))):
        raise HTTPException(status_code=401, detail="Session is no longer valid, please log in again")
    return str(state["role"])


def get_current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        claims = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired, please log in again")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid session")
    role = validate_session_claims(claims)
    return {"id": claims["id"], "username": claims["username"], "role": role}


def require_role(*roles: str):
    """
    Depends(require_role('admin')) — 403s if the authenticated user's
    role isn't in `roles`. Role-only for now; scope checks (which
    cloud/account/region a user may act on) are a later authorization
    layer, not implemented in this phase.
    """
    def _check(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return _check
