# app/auth/saml.py
"""
SAML 2.0 SSO scaffolding (2026-09-14). Built in full, OFF BY DEFAULT --
every endpoint in app/api/sso.py refuses with 503 until SSO_SAML_ENABLED
is explicitly set to "true" AND the IdP settings below are filled in.
Same "wire it, ship it inert, flip one flag to activate" pattern this
session already used for LLM_SUMMARY_ENABLED and DEPLOY_WEBHOOK_TOKEN.

WHY THIS EXISTS: enterprise buyers' IT/security teams routinely require
SSO (so access is centrally provisioned/revoked through the company's
existing identity provider -- Okta, Azure AD, OneLogin, Google
Workspace -- instead of a separate username/password this app would
have to manage) before they'll approve rolling a tool out past a pilot.
Without it, this app simply can't be adopted at that tier regardless of
how good the AIOps features are.

USES python3-saml (OneLogin's SAML toolkit) -- the standard, actively
maintained Python SAML library, not a bespoke XML-signature
implementation (rolling your own SAML assertion verification is a
well-known way to introduce an authentication bypass -- CVE history for
homegrown SAML parsers is long). Its `xmlsec` dependency ships
pre-built manylinux wheels for linux x86_64 (this app's dev/prod OS,
Ubuntu 24) as of the pinned version below, so `pip install` alone
should work with NO system package needed. If it doesn't (a wheel
mismatch for your exact environment), the fallback is:
    sudo apt install -y libxmlsec1-dev pkg-config
    pip install python3-saml --break-system-packages
-- flagged here rather than assumed silently, since this is the one
dependency in this whole session's work that MIGHT need a step beyond
the normal pip install.

SINGLE-IDP DESIGN: one IdP config via .env, not a database table with
a UI to manage multiple IdPs. A company the size this app targets has
exactly one identity provider (their whole company's Okta/Azure AD
tenant) -- multi-IdP (SSO per customer org, for a true multi-tenant
SaaS) is a real feature but a different, bigger one than what's
being asked for here.
"""
import os


def is_enabled() -> bool:
    return os.getenv("SSO_SAML_ENABLED", "false").strip().lower() == "true"


def _require_config():
    required = ["SSO_SP_ENTITY_ID", "SSO_SP_ACS_URL", "SSO_IDP_ENTITY_ID",
                "SSO_IDP_SSO_URL", "SSO_IDP_X509_CERT"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            f"SSO_SAML_ENABLED=true but missing required env var(s): {', '.join(missing)}"
        )


def build_saml_settings() -> dict:
    """
    python3-saml settings dict -- see
    https://github.com/SAML-Toolkits/python3-saml#settings for the full
    shape. wantAssertionsSigned/wantMessagesSigned both True -- this app
    never trusts an unsigned assertion, full stop; a misconfigured IdP
    that only signs the response envelope (not the assertion itself) is
    a known SAML spoofing vector this deliberately does not tolerate.
    """
    _require_config()
    return {
        "strict": True,
        "debug": os.getenv("APP_ENV", "production") != "production",
        "sp": {
            "entityId": os.getenv("SSO_SP_ENTITY_ID"),
            "assertionConsumerService": {
                "url": os.getenv("SSO_SP_ACS_URL"),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        },
        "idp": {
            "entityId": os.getenv("SSO_IDP_ENTITY_ID"),
            "singleSignOnService": {
                "url": os.getenv("SSO_IDP_SSO_URL"),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": os.getenv("SSO_IDP_X509_CERT"),
        },
        "security": {
            "wantAssertionsSigned": True,
            "wantMessagesSigned": True,
            "wantNameIdEncrypted": False,
            "requestedAuthnContext": False,
        },
    }


async def build_request_data(request) -> dict:
    """Converts a FastAPI Request into the plain dict python3-saml's
    OneLogin_Saml2_Auth expects (it's framework-agnostic and doesn't
    know about FastAPI/Starlette). https flag reads X-Forwarded-Proto
    first -- this app sits behind a reverse proxy in production (see
    COOKIE_SECURE's own reasoning in app/api/auth.py), so
    request.url.scheme alone would report 'http' even when the
    actual public-facing connection is HTTPS."""
    form = await request.form()
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return {
        "https": "on" if forwarded_proto == "https" else "off",
        "http_host": request.headers.get("host", request.url.hostname),
        "script_name": request.url.path,
        "get_data": dict(request.query_params),
        "post_data": dict(form),
    }
