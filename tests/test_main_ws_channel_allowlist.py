# tests/test_main_ws_channel_allowlist.py
"""
Audit b05: app/main.py's /ws/{channel} accepted any client-supplied
channel string, which app/ws/manager.py's connect() then silently
turned into a new, permanent entry in active_connections -- never
cleaned up even after every connection on it disconnected (unbounded
memory growth from an authenticated-but-otherwise-unrestricted client
repeatedly opening /ws/<random-string>). Fixed by checking {channel}
against ws/manager.KNOWN_CHANNELS before any cookie/DB work.

Reuses the same load-in-isolation harness as
tests/test_main_ws_security.py (see that file/conftest.py's docstrings
for why) rather than duplicating its stub setup wholesale.
"""
import os
import sys

from fastapi import APIRouter

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_module, install_stub

os.environ.setdefault("JWT_SECRET", "unit-test-secret-" + "x" * 32)

_SIMPLE_ROUTER_MODULES = [
    "app.api.alerts", "app.api.admin.accounts", "app.api.admin.groups",
    "app.api.admin.roles", "app.api.admin.rbac_scopes", "app.api.admin.bindings",
    "app.api.permissions", "app.api.settings", "app.api.live_data",
    "app.api.audit_logs", "app.api.metric_catalog", "app.api.topology",
    "app.api.op_events", "app.api.escalation", "app.api.incidents",
    "app.api.reports", "app.api.nlquery", "app.api.synthetic",
    "app.api.webhooks", "app.api.deploy_risk", "app.api.sso", "app.api.slo",
    "app.api.security", "app.api.maintenance", "app.api.auth", "app.api.admin.users",
]


class _FakeWebSocket:
    def __init__(self):
        self.closed_code = None
        self.cookies = {}

    async def accept(self):
        raise AssertionError("accept() must not be called for a rejected channel")

    async def close(self, code=1000):
        self.closed_code = code

    async def receive_text(self):
        raise AssertionError("receive_text() must not be called for a rejected channel")


def _load_main():
    install_stub("dotenv", load_dotenv=lambda *a, **k: None)
    for m in _SIMPLE_ROUTER_MODULES:
        install_stub(m, router=APIRouter())
    install_stub("app.api.status_page", admin_router=APIRouter(), public_router=APIRouter())

    connect_calls = []

    class _WSManager:
        async def connect(self, ws, channel, accessible):
            connect_calls.append((ws, channel, accessible))
        def disconnect(self, ws, channel):
            pass
        def connection_count(self):
            return len(connect_calls)

    install_stub("app.ws.manager", ws_manager=_WSManager(), KNOWN_CHANNELS=("overview", "alerts", "metrics"))

    async def _noop():
        return None
    install_stub("app.ws.pusher", redis_listener=_noop, stop_listener=_noop)

    # decode_token/validate_session_claims must NOT be reached at all for a
    # rejected channel -- make them blow up loudly if they ever are.
    def _must_not_be_called(*a, **k):
        raise AssertionError("auth must not run before the channel is validated")

    install_stub("app.auth.deps", get_current_user=lambda: None, COOKIE_NAME="mh_session",
                 validate_session_claims=_must_not_be_called)
    install_stub("app.auth.security", decode_token=_must_not_be_called,
                 create_access_token=lambda *a, **k: "tok")
    install_stub("app.auth.authorization", get_accessible_account_ids=_must_not_be_called)

    mod = load_module("app/main.py")
    return mod, connect_calls


def test_unknown_channel_is_rejected_before_any_auth_work():
    import asyncio
    mod, connect_calls = _load_main()
    ws = _FakeWebSocket()
    asyncio.run(mod.websocket_endpoint(ws, "not-a-real-channel-" + "x" * 50))
    assert ws.closed_code == 4404
    assert connect_calls == []


def test_empty_channel_is_rejected():
    import asyncio
    mod, connect_calls = _load_main()
    ws = _FakeWebSocket()
    asyncio.run(mod.websocket_endpoint(ws, ""))
    assert ws.closed_code == 4404
    assert connect_calls == []


def test_known_channels_constant_matches_manager_dict_keys():
    """Cheap guard against KNOWN_CHANNELS and ws/manager.py's
    active_connections dict silently drifting apart in the future."""
    mgr_mod = load_module("app/ws/manager.py")
    assert set(mgr_mod.KNOWN_CHANNELS) == set(mgr_mod.ConnectionManager().active_connections.keys())
