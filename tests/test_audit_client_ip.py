# tests/test_audit_client_ip.py
"""
Tests app/audit.py's _client_ip() (audit b05 fix). deploy/nginx.conf
sets X-Forwarded-For via nginx's $proxy_add_x_forwarded_for, which
APPENDS to any client-supplied value rather than replacing it, and
uvicorn is run with --proxy-headers --forwarded-allow-ips='127.0.0.1'
so it already resolves request.client.host correctly from that same
trusted header before app code ever runs. _client_ip() must use
request.client.host rather than re-parsing X-Forwarded-For itself
(which previously returned the attacker-controlled first hop).
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_module, install_stub


def _fake_request(client_host, xff_header=None):
    req = types.SimpleNamespace()
    req.client = types.SimpleNamespace(host=client_host) if client_host else None
    headers = {}
    if xff_header is not None:
        headers["x-forwarded-for"] = xff_header
    req.headers = headers
    return req


def _load_audit():
    install_stub("app.db", get_connection=lambda: None)
    return load_module("app/audit.py")


def test_uses_request_client_host_as_the_real_ip():
    mod = _load_audit()
    req = _fake_request("203.0.113.9")
    assert mod._client_ip(req) == "203.0.113.9"


def test_ignores_a_spoofed_x_forwarded_for_header():
    """
    SECURITY: previously this returned the FIRST entry of a raw
    X-Forwarded-For header, which is exactly the attacker-controlled
    value a client can prepend before nginx appends the real IP
    (nginx uses $proxy_add_x_forwarded_for, which appends rather than
    replaces). request.client.host -- already correctly resolved by
    uvicorn's trusted-proxy handling -- must win regardless of what
    the raw header says.
    """
    mod = _load_audit()
    # Simulates nginx having appended the real client IP after an
    # attacker-supplied leading value.
    req = _fake_request("198.51.100.7", xff_header="1.2.3.4, 198.51.100.7")
    assert mod._client_ip(req) == "198.51.100.7"
    assert mod._client_ip(req) != "1.2.3.4"


def test_returns_none_when_request_has_no_client():
    mod = _load_audit()
    req = _fake_request(None)
    assert mod._client_ip(req) is None


def test_returns_none_when_request_itself_is_none():
    mod = _load_audit()
    assert mod._client_ip(None) is None


def test_never_raises_on_a_malformed_request_object():
    mod = _load_audit()
    req = object()  # has neither .client nor .headers
    assert mod._client_ip(req) is None
