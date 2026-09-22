# tests/test_main_ws_security.py
"""
Audit B01 follow-up: app/main.py's /ws/{channel} endpoint used to accept any
signature-valid, unexpired JWT (decode_token() only) with no check that the
session behind it was still valid -- unlike every REST route, which goes
through get_current_user() -> validate_session_claims() (active/token_
version/revoked-jti). A logged-out or deactivated user's cookie could still
open (or keep open) a live WebSocket feed. These tests call the real
websocket_endpoint coroutine directly (the @app.websocket(...) decorator
returns the undecorated function -- see FastAPI/Starlette source) against a
fake WebSocket object, so no ASGI transport or ORM/router stubs beyond what
main.py imports at module load time are needed.
"""
import os
import sys
import types

import pytest
from fastapi import HTTPException, APIRouter

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_module, install_stub

os.environ.setdefault("JWT_SECRET", "unit-test-secret-" + "x" * 32)

_ROUTER_MODULES = [
    "app.api.alerts", "app.api.admin.accounts", "app.api.admin.groups",
    "app.api.admin.roles", "app.api.admin.rbac_scopes", "app.api.admin.bindings",
    "app.api.permissions", "app.api.settings", "app.api.live_data",
    "app.api.audit_logs", "app.api.metric_catalog", "app.api.topology",
    "app.api.op_events", "app.api.escalation", "app.api.incidents",
    "app.api.reports", "app.api.nlquery", "app.api.synthetic",
    "app.api.webhooks", "app.api.deploy_risk", "app.api.sso", "app.api.slo",
    "app.api.security", "app.api.maintenance",
]
# app.api.auth and app.api.admin.users have real create_access_token/
# decode_token side effects we don't want here -- give them empty routers too;
# this test only exercises websocket_endpoint(), not those routers.
_SIMPLE_ROUTER_MODULES = _ROUTER_MODULES + ["app.api.auth", "app.api.admin.users"]


class _FakeWebSocket:
    def __init__(self, cookies):
        self.cookies = cookies
        self.closed_code = None
        self._messages = list()  # queue of incoming client messages
        self.sent = []
        self.accepted = False

    def queue(self, *msgs):
        self._messages.extend(msgs)
        return self

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed_code = code

    async def receive_text(self):
        from starlette.websockets import WebSocketDisconnect
        if not self._messages:
            raise WebSocketDisconnect()
        return self._messages.pop(0)

    async def send_text(self, text):
        self.sent.append(text)


def _load_main(decode_token, validate_session_claims, get_accessible_account_ids=None):
    install_stub("dotenv", load_dotenv=lambda *a, **k: None)
    for m in _SIMPLE_ROUTER_MODULES:
        install_stub(m, router=APIRouter())
    install_stub("app.api.status_page", admin_router=APIRouter(), public_router=APIRouter())

    connects, disconnects = [], []

    class _WSManager:
        async def connect(self, ws, channel, accessible):
            connects.append((ws, channel, accessible))
        def disconnect(self, ws, channel):
            disconnects.append((ws, channel))
        def connection_count(self):
            return len(connects) - len(disconnects)

    ws_manager = _WSManager()
    install_stub("app.ws.manager", ws_manager=ws_manager,
                 KNOWN_CHANNELS=("overview", "alerts", "metrics"))

    async def _noop():
        return None
    install_stub("app.ws.pusher", redis_listener=_noop, stop_listener=_noop)

    install_stub("app.auth.deps", get_current_user=lambda: None, COOKIE_NAME="mh_session",
                 validate_session_claims=validate_session_claims)
    install_stub("app.auth.security", decode_token=decode_token,
                 create_access_token=lambda *a, **k: "tok")
    install_stub("app.auth.authorization",
                 get_accessible_account_ids=get_accessible_account_ids or (lambda claims: {1, 2}))

    mod = load_module("app/main.py")
    return mod, ws_manager, connects, disconnects


def test_missing_cookie_is_closed_before_any_db_or_manager_work():
    calls = []
    mod, _, connects, _ = _load_main(
        decode_token=lambda t: calls.append(t) or {"id": 1},
        validate_session_claims=lambda c: "viewer",
    )
    import asyncio
    ws = _FakeWebSocket(cookies={})
    asyncio.run(mod.websocket_endpoint(ws, "alerts"))
    assert ws.closed_code == 4401 and calls == [] and connects == []


def test_garbage_token_is_closed():
    mod, _, connects, _ = _load_main(
        decode_token=lambda t: (_ for _ in ()).throw(Exception("bad sig")),
        validate_session_claims=lambda c: "viewer",
    )
    import asyncio
    ws = _FakeWebSocket(cookies={"mh_session": "garbage"})
    asyncio.run(mod.websocket_endpoint(ws, "alerts"))
    assert ws.closed_code == 4401 and connects == []


def test_signature_valid_but_revoked_session_is_closed_not_connected():
    """The exact gap this fix closes: decode_token() alone would have let
    this through (it only checks signature/expiry)."""
    claims = {"id": 5, "username": "amy", "role": "viewer", "jti": "j1", "tv": 0}
    mod, _, connects, _ = _load_main(
        decode_token=lambda t: claims,
        validate_session_claims=lambda c: (_ for _ in ()).throw(HTTPException(401, "revoked")),
    )
    import asyncio
    ws = _FakeWebSocket(cookies={"mh_session": "valid-signature-but-revoked"})
    asyncio.run(mod.websocket_endpoint(ws, "alerts"))
    assert ws.closed_code == 4401 and connects == []


def test_valid_session_connects_with_resolved_scope():
    claims = {"id": 5, "username": "amy", "role": "viewer", "jti": "j1", "tv": 0}
    mod, ws_manager, connects, _ = _load_main(
        decode_token=lambda t: claims,
        validate_session_claims=lambda c: "viewer",
        get_accessible_account_ids=lambda c: {7, 9},
    )
    import asyncio
    ws = _FakeWebSocket(cookies={"mh_session": "good"})
    asyncio.run(mod.websocket_endpoint(ws, "alerts"))
    assert len(connects) == 1
    _, channel, accessible = connects[0]
    assert channel == "alerts" and accessible == {7, 9}


def test_scope_lookup_failure_fails_closed_to_empty_scope_but_still_connects():
    claims = {"id": 5, "username": "amy", "role": "viewer", "jti": "j1", "tv": 0}
    mod, ws_manager, connects, _ = _load_main(
        decode_token=lambda t: claims,
        validate_session_claims=lambda c: "viewer",
        get_accessible_account_ids=lambda c: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    import asyncio
    ws = _FakeWebSocket(cookies={"mh_session": "good"})
    asyncio.run(mod.websocket_endpoint(ws, "alerts"))
    assert len(connects) == 1 and connects[0][2] == set()   # fails CLOSED (empty), not open


def test_revoked_mid_connection_is_disconnected_on_next_ping():
    """The frontend pings every ~10s (see useWebSocket.js). A session
    revoked mid-connection (logout from another tab, admin deactivates the
    user) must be dropped on the next ping rather than staying open for up
    to 12h (the JWT's own expiry)."""
    claims = {"id": 5, "username": "amy", "role": "viewer", "jti": "j1", "tv": 0}
    call_count = {"n": 0}

    def revalidate(c):
        call_count["n"] += 1
        # call 1 = the connect-time check (must pass so the socket opens at all);
        # call 2 = the first ping-triggered re-check, revoked from here on.
        if call_count["n"] >= 2:
            raise HTTPException(401, "revoked")
        return "viewer"

    mod, ws_manager, connects, disconnects = _load_main(
        decode_token=lambda t: claims, validate_session_claims=revalidate,
    )
    import asyncio
    ws = _FakeWebSocket(cookies={"mh_session": "good"}).queue("ping", "ping")
    asyncio.run(mod.websocket_endpoint(ws, "alerts"))
    assert len(connects) == 1                       # connect-time check passed
    assert ws.sent == []                             # 1st ping's re-check already fails -> no pong
    assert ws.closed_code == 4401                   # closed instead
    assert disconnects == [(ws, "alerts")]           # and unregistered from the broadcaster


def test_still_valid_session_keeps_getting_pongs():
    claims = {"id": 5, "username": "amy", "role": "viewer", "jti": "j1", "tv": 0}
    mod, ws_manager, connects, disconnects = _load_main(
        decode_token=lambda t: claims, validate_session_claims=lambda c: "viewer",
    )
    import asyncio
    ws = _FakeWebSocket(cookies={"mh_session": "good"}).queue("ping", "ping", "ping")
    asyncio.run(mod.websocket_endpoint(ws, "alerts"))
    assert ws.sent == ['{"type":"pong"}'] * 3 and ws.closed_code is None
    assert disconnects == [(ws, "alerts")]          # normal disconnect (queue exhausted) still cleans up
