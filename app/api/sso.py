# app/api/sso.py
"""
SAML 2.0 SSO endpoints (2026-09-14). See app/auth/saml.py's module
docstring for the full design. Every endpoint here calls
_check_enabled() first and returns 503 until SSO_SAML_ENABLED=true AND
the required IdP env vars are set -- see app/auth/saml.py's
_require_config(). No _auth_dep on this router (see main.py) --
these ARE the pre-authentication login flow, same as
app/api/auth.py's POST /login, which also has none.
"""
import logging
import os
import secrets

import bcrypt
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from app.db import get_connection
from app.auth.security import create_access_token
from app.auth.deps import COOKIE_NAME
from app.auth import saml
from app.audit import write_audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth/sso", tags=["SSO"])

COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").strip().lower() == "true"
COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60  # matches app/api/auth.py's local-login session length


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
    """Returns (id, username, role) for an existing user matching this
    email, or auto-provisions one if SSO_AUTO_PROVISION=true. Returns
    None if no match and auto-provisioning is off -- caller rejects the
    login with a clear message rather than silently creating accounts
    an admin didn't opt into."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, username, role FROM users WHERE email = %s AND active = 1",
            (email,),
        )
        user = cursor.fetchone()
        if user:
            return user["id"], user["username"], user["role"]

        if os.getenv("SSO_AUTO_PROVISION", "false").strip().lower() != "true":
            return None

        default_role = os.getenv("SSO_DEFAULT_ROLE", "viewer")
        if default_role not in ("viewer", "editor", "admin"):
            logger.warning(f"[sso] SSO_DEFAULT_ROLE={default_role!r} is invalid, defaulting to 'viewer'")
            default_role = "viewer"

        # SSO-provisioned accounts have no usable local password -- a
        # random 32-byte secret, bcrypt-hashed like any real password,
        # so local POST /api/auth/login can never authenticate as this
        # user (the random value was never returned to anyone and isn't
        # stored anywhere else) while the users.password_hash NOT NULL
        # constraint is still satisfied without a schema change.
        unusable_password_hash = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt()).decode()

        cursor.execute("""
            INSERT INTO users (username, email, password_hash, role, auth_source, active)
            VALUES (%s, %s, %s, %s, 'sso', 1)
        """, (email, email, unusable_password_hash, default_role))
        conn.commit()
        logger.info(f"[sso] auto-provisioned new user {email!r} with role={default_role!r}")
        return cursor.lastrowid, email, default_role
    finally:
        cursor.close()
        conn.close()


@router.get("/login")
async def sso_login(request: Request):
    """SP-initiated login -- redirects the browser to the IdP's SSO
    URL. The frontend's login page should link/redirect here (behind a
    'Log in with SSO' button) when SSO is enabled -- see /metadata for
    how the IdP itself gets configured to trust this app."""
    _check_enabled()
    request_data = await saml.build_request_data(request)
    auth = _saml_auth(request_data)
    return RedirectResponse(url=auth.login(), status_code=302)


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
    request_data = await saml.build_request_data(request)
    auth = _saml_auth(request_data)
    auth.process_response()

    errors = auth.get_errors()
    if errors:
        reason = auth.get_last_error_reason()
        write_audit("sso", "SSO login failed", request=request,
                     payload={"errors": errors, "reason": reason})
        raise HTTPException(status_code=401, detail=f"SAML authentication failed: {reason}")

    if not auth.is_authenticated():
        raise HTTPException(status_code=401, detail="SAML authentication was not confirmed by the IdP")

    email_attribute = os.getenv("SSO_EMAIL_ATTRIBUTE", "")
    if email_attribute:
        values = auth.get_attribute(email_attribute) or []
        email = values[0] if values else None
    else:
        email = auth.get_nameid()  # NameIDFormat is emailAddress, see build_saml_settings()

    if not email:
        raise HTTPException(status_code=401, detail="SAML response did not include an email address")

    match = _find_or_provision_user(email)
    if match is None:
        write_audit("sso", "SSO login rejected -- no matching local user", request=request,
                     payload={"email": email})
        raise HTTPException(
            status_code=403,
            detail=f"No account exists for {email} and SSO_AUTO_PROVISION is not enabled -- "
                   f"ask an admin to create your account first",
        )

    user_id, username, role = match
    token = create_access_token(user_id, username, role)
    response = RedirectResponse(url=os.getenv("PUBLIC_APP_URL", "/"), status_code=302)
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True, secure=COOKIE_SECURE,
        samesite="lax", max_age=COOKIE_MAX_AGE_SECONDS, path="/",
    )
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
        raise HTTPException(status_code=500, detail=f"Invalid SP metadata configuration: {errors}")
    return Response(content=metadata, media_type="application/xml")
