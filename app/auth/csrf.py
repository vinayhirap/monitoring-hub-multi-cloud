# app/auth/csrf.py
"""
Origin check for state-changing requests (audit E3: CSRF posture unverified).

Session auth is a cookie (mh_session: HttpOnly, SameSite=Lax). SameSite=Lax already
stops browsers sending the cookie on cross-site POST/PUT/PATCH/DELETE; this is the
second layer. A browser always attaches `Origin` to those requests, so a request whose
Origin is present and not ours is rejected with 403 before it reaches any route.

Allowed when ANY of:
  * method is safe (GET/HEAD/OPTIONS);
  * no Origin header (curl, server-to-server, same-origin GET-style navigations) --
    a browser cannot omit it on a cross-site unsafe request;
  * Origin's host name equals the request's Host header (the normal same-origin case;
    scheme and port are ignored so the http -> https migration cannot lock anyone out);
  * Origin is listed in CORS_ALLOWED_ORIGINS or equals PUBLIC_APP_URL;
  * the path is an exempt prefix: the SAML ACS callback is a legitimate cross-site POST
    from the IdP, and webhooks authenticate with their own bearer token.

Set CSRF_ORIGIN_CHECK=false to disable (escape hatch, not recommended).
"""
import os
from urllib.parse import urlsplit

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
EXEMPT_PREFIXES = ("/api/auth/sso/", "/api/webhooks/")


def _hostport(value: str) -> str:
    """'http://Host:8080/x' -> 'host:8080'; scheme/path ignored, lower-cased."""
    parts = urlsplit(value if "://" in value else f"//{value}")
    return (parts.netloc or "").lower()


def _hostname(value: str) -> str:
    """Host part without port: 'host:8080' / 'http://host:8080' -> 'host' ([v6] kept)."""
    hp = _hostport(value)
    if hp.startswith("["):
        return hp.split("]", 1)[0] + "]"
    return hp.rsplit(":", 1)[0] if ":" in hp else hp


def enabled() -> bool:
    return os.getenv("CSRF_ORIGIN_CHECK", "true").strip().lower() != "false"


def allowed_origin_hosts() -> set:
    hosts = set()
    for raw in (os.getenv("CORS_ALLOWED_ORIGINS", "") or "").split(","):
        if raw.strip():
            hosts.add(_hostport(raw.strip()))
    public = (os.getenv("PUBLIC_APP_URL") or "").strip()
    if public:
        hosts.add(_hostport(public))
    hosts.discard("")
    return hosts


def origin_allowed(method: str, path: str, origin, host, extra_hosts=None) -> bool:
    if method.upper() in SAFE_METHODS:
        return True
    if any(path.startswith(p) for p in EXEMPT_PREFIXES):
        return True
    if not origin:
        return True
    if origin.strip().lower() == "null":
        return False                      # sandboxed iframe / file:// / data: -- never ours
    origin_host = _hostport(origin)
    # nginx forwards `Host $host`, which carries NO port, while the browser's Origin does
    # for non-default ports -- so compare host names, not host:port. (A different port on
    # the same host is not a cross-site attacker position worth locking users out over.)
    if host and _hostname(origin) == _hostname(host):
        return True
    return origin_host in (extra_hosts if extra_hosts is not None else allowed_origin_hosts())
