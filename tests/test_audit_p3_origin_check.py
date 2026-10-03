# tests/test_audit_p3_origin_check.py
"""Audit E3: state-changing requests from a foreign Origin are rejected."""
import sys
sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app          # noqa: F401
import app.auth     # noqa: F401
from app.auth import csrf  # noqa: E402

H = "35.154.149.94"


def ok(method, path, origin, host=H, extra=frozenset()):
    return csrf.origin_allowed(method, path, origin, host, extra)


def test_safe_methods_always_allowed():
    for m in ("GET", "HEAD", "OPTIONS"):
        assert ok(m, "/api/alerts", "https://evil.example")


def test_same_origin_post_allowed_any_scheme_and_port():
    assert ok("POST", "/api/auth/login", "http://35.154.149.94")
    assert ok("POST", "/api/auth/login", "https://35.154.149.94")            # after TLS migration
    assert ok("PATCH", "/api/alerts/1/ack", "http://35.154.149.94:8080")     # nginx strips port from Host


def test_cross_origin_unsafe_methods_blocked():
    for m in ("POST", "PUT", "PATCH", "DELETE"):
        assert not ok(m, "/api/alerts/clear", "https://evil.example")
    assert not ok("POST", "/api/alerts/clear", "http://35.154.149.94.evil.example")   # suffix trick
    assert not ok("POST", "/api/alerts/clear", "http://evil.example/35.154.149.94")


def test_opaque_null_origin_blocked():
    assert not ok("POST", "/api/auth/login", "null")


def test_missing_origin_allowed_for_non_browser_clients():
    assert ok("POST", "/api/webhooks/deploy", None)
    assert ok("POST", "/api/auth/login", "")


def test_configured_origins_allowed():
    assert ok("POST", "/api/x", "https://hub.example.com", extra={"hub.example.com"})
    assert not ok("POST", "/api/x", "https://other.example.com", extra={"hub.example.com"})


def test_sso_acs_and_webhooks_exempt():
    assert ok("POST", "/api/auth/sso/acs", "https://idp.example.com")
    assert ok("POST", "/api/webhooks/deploy", "https://ci.example.com")
    assert not ok("POST", "/api/auth/change-password", "https://idp.example.com")


def test_env_toggle_and_allow_list(monkeypatch):
    monkeypatch.setenv("CSRF_ORIGIN_CHECK", "false")
    assert csrf.enabled() is False
    monkeypatch.setenv("CSRF_ORIGIN_CHECK", "true")
    assert csrf.enabled() is True
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173, https://hub.example.com")
    monkeypatch.setenv("PUBLIC_APP_URL", "https://portal.example.com/")
    assert csrf.allowed_origin_hosts() == {"localhost:5173", "hub.example.com", "portal.example.com"}


def test_middleware_is_wired_before_security_headers():
    src = open(__file__.rsplit("/tests/", 1)[0] + "/app/main.py").read()
    assert src.index("async def _origin_check") < src.index("async def _security_headers")
    assert "Cross-origin request blocked" in src
