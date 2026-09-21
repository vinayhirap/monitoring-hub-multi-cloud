# app/api/sso.py
"""
SAML 2.0 SSO endpoints (2026-09-14). See app/auth/saml.py's module
docstring for the full design. Every endpoint here calls
_check_enabled() first and returns 503 until SSO_SAML_ENABLED=true AND
the required IdP env vars are set -- see app/auth/saml.py's
_require_config(). No _auth_dep on this router (see main.py) --
these ARE the pre-authentication login flow, same as
app/api/auth.py's POST /login, which also has none.

Audit B01: only SP-initiated logins are accepted. /login records the
AuthnRequest id in Redis; /acs consumes it exactly once and passes it to
python3-saml as request_id, so InResponseTo is verified and a captured
assertion cannot be replayed or pushed at us unsolicited. If Redis is
down SSO fails closed (local login is unaffected).
"""
import logging
import os
import secrets

import bcrypt
from mysql.connector import errors as mysql_errors
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from app.db import get_connection
from app.auth.security import create_access_token
from app.auth.deps import set_session_cookie
from app.auth.rate_limit import enforce_sso_rate_limit
from app.auth import saml
from app.audit import write_audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth/sso", tags=["SSO"])

_LOGIN_FAILED = "SSO login failed"
_USERNAME_MAX = 100   # users.username is VARCHAR(100)


class _SsoDenied(Exception):
    """Login refused; str(e) is the audit reason (never shown to the client)."""


def _check_enabled():
    if not saml.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="SSO is not enabled -- set SSO_SAML_ENABLED=true and the SSO_* IdP "
                   "settings in .env to activate it",
        )


def _saml_auth(request_data: dict):
    from onelogin.saml2.auth import OneLogin_Saml2_Auth
    return OneLogin_Saml2_Auth(request_data, saml.build_saml_settings())


def _find_or_provision_user(email: str):
    """Returns (id, username, role, token_version) for the single ACTIVE user
    matching this email, or auto-provisions one if SSO_AUTO_PROVISION=true.
    Returns None if no match and auto-provisioning is off. Raises _SsoDenied
    for a deactivated match, an ambiguous match, or an unprovisionable email
    -- a deactivated account must never be silently re-created by SSO."""
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id, username, role, active, token_version FROM users WHERE email = %s",
                (email,),
            )
            rows = cursor.fetchall()
            if rows:
                active = [r for r in rows if r["active"]]
                if not active:
                    raise _SsoDenied("matching local account is deactivated")
                if len(active) > 1:
                    raise _SsoDenied("email matches more than one active account")
                u = active[0]
                return u["id"], u["username"], u["role"], int(u["token_version"] or 0)

            if os.getenv("SSO_AUTO_PROVISION", "false").strip().lower() != "true":
                return None

            if len(email) > _USERNAME_MAX:
                raise _SsoDenied(f"email longer than {_USERNAME_MAX} chars cannot be used as username")

            # Auto-provisioning may only create least-privilege roles; an
            # IdP-authenticated principal must never be minted as admin here.
            default_role = os.getenv("SSO_DEFAULT_ROLE", "viewer")
            if default_role not in ("viewer", "editor"):
                logger.warning(f"[sso] SSO_DEFAULT_ROLE={default_role!r} is not allowed for "
                               f"auto-provisioning (viewer/editor only), using 'viewer'")
                default_role = "viewer"

            # No usable local password: random 32-byte secret, bcrypt-hashed,
            # never stored or returned, so POST /api/auth/login can't succeed.
            unusable_password_hash = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt()).decode()

            try:
                cursor.execute(
                    "INSERT INTO users (username, email, password, role, auth_source, active) "
                    "VALUES (%s, %s, %s, %s, 'sso', 1)",
                    (email, email, unusable_password_hash, default_role),
                )
                conn.commit()
            except mysql_errors.IntegrityError:
                conn.rollback()
                raise _SsoDenied("username already taken by a different account")
            logger.info(f"[sso] auto-provisioned new user {email!r} with role={default_role!r}")
            return cursor.lastrowid, email, default_role, 0
        finally:
            cursor.close()
    finally:
        conn.close()


@router.get("/login")
async def sso_login(request: Request):
    """SP-initiated login -- redirects the browser to the IdP's SSO
    URL. The frontend's login page should link/redirect here (behind a
    'Log in with SSO' button) when SSO is enabled -- see /metadata for
    how the IdP itself gets configured to trust this app."""
    _check_enabled()
    enforce_sso_rate_limit(request)
    request_data = await saml.build_request_data(request)
    auth = _saml_auth(request_data)
    url = auth.login()
    try:
        saml.remember_request_id(auth.get_last_request_id())
    except saml.SamlStateUnavailable as e:
        logger.error(f"[sso] request-id store unavailable: {e}")
        raise HTTPException(status_code=503, detail="SSO temporarily unavailable")
    return RedirectResponse(url=url, status_code=302)


@router.post("/acs")
async def sso_acs(request: Request):
    """
    Assertion Consumer Service -- the URL the IdP POSTs the signed SAML
    response back to after the user authenticates there. Verifies the
    signature/conditions (python3-saml, not a hand-rolled check -- see
    app/auth/saml.py's module docstring), extracts the user's email,
    finds or auto-provisions the matching local user, and issues the
    EXACT SAME session cookie app/api/auth.py's local-password login
    issues -- from this point on, an SSO-authenticated session is
    indistinguishable from a local one to the rest of the app (every
    existing require_permission()/get_current_user() check keeps
    working unchanged).
    """
    _check_enabled()
    enforce_sso_rate_limit(request)
    request_data = await saml.build_request_data(request)

    in_response_to = saml.extract_in_response_to(request_data["post_data"].get("SAMLResponse", ""))
    if not in_response_to:
        write_audit("sso", "SSO login failed", request=request,
                     payload={"reason": "missing/invalid InResponseTo (IdP-initiated not supported)"})
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED)
    try:
        known = saml.consume_request_id(in_response_to)
    except saml.SamlStateUnavailable as e:
        logger.error(f"[sso] request-id store unavailable: {e}")
        raise HTTPException(status_code=503, detail="SSO temporarily unavailable")
    if not known:
        write_audit("sso", "SSO login failed", request=request,
                     payload={"reason": "unknown, expired or already-used request id"})
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED)

    auth = _saml_auth(request_data)
    auth.process_response(request_id=in_response_to)

    errors = auth.get_errors()
    if errors:
        reason = auth.get_last_error_reason()
        logger.warning(f"[sso] SAML response rejected: {errors} {reason}")
        write_audit("sso", "SSO login failed", request=request,
                     payload={"errors": errors, "reason": reason})
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED)

    if not auth.is_authenticated():
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED)

    email_attribute = os.getenv("SSO_EMAIL_ATTRIBUTE", "")
    if email_attribute:
        values = auth.get_attribute(email_attribute) or []
        email = values[0] if values else None
    else:
        email = auth.get_nameid()  # NameIDFormat is emailAddress, see build_saml_settings()

    email = (email or "").strip()
    if not email:
        raise HTTPException(status_code=401, detail="SAML response did not include an email address")

    try:
        match = _find_or_provision_user(email)
    except _SsoDenied as e:
        write_audit("sso", "SSO login rejected", request=request,
                     payload={"email": email, "reason": str(e)})
        raise HTTPException(status_code=403, detail="SSO login is not permitted for this account -- contact an administrator")
    if match is None:
        write_audit("sso", "SSO login rejected -- no matching local user", request=request,
                     payload={"email": email})
        raise HTTPException(
            status_code=403,
            detail="No account exists for this identity and SSO_AUTO_PROVISION is not enabled -- "
                   "ask an admin to create your account first",
        )

    user_id, username, role, token_version = match
    token = create_access_token(user_id, username, role, token_version=token_version)
    response = RedirectResponse(url=os.getenv("PUBLIC_APP_URL", "/"), status_code=302)
    set_session_cookie(response, token)
    write_audit(username, "Login successful (SSO)", role=role.upper(), request=request,
                 payload={"username": username, "auth_source": "sso"})
    return response


@router.get("/metadata")
def sso_metadata():
    """SP metadata XML -- give this URL (or its downloaded XML) to
    whoever administers your IdP (Okta/Azure AD/OneLogin admin console)
    to configure this app as a trusted Service Provider. Read-only,
    contains no secrets -- entity ID and ACS URL only, safe to expose
    without authentication (an IdP admin needs to fetch this before any
    login has ever succeeded)."""
    _check_enabled()
    from onelogin.saml2.settings import OneLogin_Saml2_Settings
    settings = OneLogin_Saml2_Settings(saml.build_saml_settings(), sp_validation_only=True)
    metadata = settings.get_sp_metadata()
    errors = settings.validate_metadata(metadata)
    if errors:
        logger.error(f"[sso] invalid SP metadata configuration: {errors}")
        raise HTTPException(status_code=500, detail="Invalid SP metadata configuration (see server log)")
    return Response(content=metadata, media_type="application/xml")
