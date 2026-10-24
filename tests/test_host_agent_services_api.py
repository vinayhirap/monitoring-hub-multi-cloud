# tests/test_host_agent_services_api.py
"""
Host agent patch 0009: GET /api/host-agents/{id}/services (watched services with their latest stored state plus the host's
current config), pruning when PATCH config shrinks the watch list, and the pure _service_view(). MiniDb fails loudly on any
SQL it does not know, so "the read writes nothing" is enforced by the statement log.
"""
from datetime import timedelta
from pathlib import Path

import pytest

from tests.hostagent_support import Env, utcnow

ROOT = Path(__file__).resolve().parent.parent
BASE = "/api/host-agents"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_ENABLED", "true")
    return Env()


def _host(env, account=1, hostname="web-1"):
    k = env.make_key(account=account, max_uses=5)
    return env.enroll(k["key"], hostname).json()["host_agent_id"]


def _svc(env, hid, name, state="running", since_s=600, seen_s=30):
    now = utcnow()
    env.db.services[(hid, name)] = dict(state=state, state_since=now - timedelta(seconds=since_s),
                                        observed_at=now - timedelta(seconds=seen_s))


def _patch(env, hid, body):
    env.as_user("admin")
    return env.client.patch(f"{BASE}/{hid}/config", json=body)


def _get(env, hid, perms=("hostagents.view",), role="viewer", accessible=None):
    env.as_user(role, set(perms), accessible=accessible)
    return env.client.get(f"{BASE}/{hid}/services")


def test_returns_current_config_states_and_summary(env):
    hid = _host(env)
    assert _patch(env, hid, {"interval_seconds": 30, "watch_services": ["nginx", "sshd", "cron", "mysql"]}).status_code == 200
    _svc(env, hid, "nginx", "running", since_s=7200, seen_s=20)
    _svc(env, hid, "sshd", "failed", since_s=90, seen_s=20)
    _svc(env, hid, "cron", "unknown", since_s=90, seen_s=20)
    body = _get(env, hid).json()
    assert body["hostname"] == "web-1" and body["interval_seconds"] == 30 and body["config_version"] == 2
    assert body["watch_services"] == ["nginx", "sshd", "cron", "mysql"]
    assert [s["name"] for s in body["services"]] == ["nginx", "sshd", "cron"]            # the admin's order
    assert body["pending"] == ["mysql"]                                                  # configured, nothing reported yet
    assert body["summary"] == {"total": 4, "running": 1, "stopped": 0, "failed": 1, "unknown": 1, "pending": 1}
    nginx = body["services"][0]
    assert nginx["state"] == "running" and 7190 <= nginx["since_seconds"] <= 7260 and 15 <= nginx["age_seconds"] <= 60
    assert nginx["since"].endswith("Z") and nginx["observed_at"].endswith("Z")


def test_defaults_when_nothing_was_configured(env):
    hid = _host(env)
    body = _get(env, hid).json()
    assert body["watch_services"] == [] and body["services"] == [] and body["pending"] == []
    assert body["interval_seconds"] == 60 and body["summary"]["total"] == 0


def test_a_leftover_row_for_an_unconfigured_name_is_never_shown(env):
    hid = _host(env)
    _patch(env, hid, {"watch_services": ["nginx"]})
    _svc(env, hid, "nginx")
    _svc(env, hid, "ghost")                                      # e.g. a row written before the list was changed by hand
    assert [s["name"] for s in _get(env, hid).json()["services"]] == ["nginx"]


def test_the_read_is_select_only(env):
    hid = _host(env)
    _patch(env, hid, {"watch_services": ["nginx"]})
    before = len(env.db.sql_log)
    _get(env, hid)
    sql = [q for q, _ in env.db.sql_log[before:]]
    assert sql and all(q.startswith("SELECT") for q in sql)


def test_needs_view_permission_and_account_scope(env):
    hid = _host(env, account=2)
    env.user = None
    assert env.client.get(f"{BASE}/{hid}/services").status_code == 401
    assert _get(env, hid, perms=()).status_code == 403
    assert _get(env, hid, accessible={1}).status_code == 404       # exists, but outside the caller's accounts: same as missing
    assert _get(env, hid, accessible={2}).status_code == 200
    assert _get(env, 999).status_code == 404


def test_feature_flag_off_is_404(env, monkeypatch):
    hid = _host(env)
    monkeypatch.setenv("HOST_AGENT_ENABLED", "false")
    assert _get(env, hid).status_code == 404


def test_patch_removing_a_service_deletes_its_state_immediately(env):
    hid = _host(env)
    _patch(env, hid, {"watch_services": ["nginx", "sshd"]})
    _svc(env, hid, "nginx")
    _svc(env, hid, "sshd")
    assert _patch(env, hid, {"watch_services": ["nginx"]}).status_code == 200
    assert set(n for (h, n) in env.db.services if h == hid) == {"nginx"}


def test_patch_with_an_empty_list_clears_every_state_and_other_hosts_are_untouched(env):
    a, b = _host(env, hostname="a"), _host(env, hostname="b")
    for h in (a, b):
        _patch(env, h, {"watch_services": ["nginx"]})
        _svc(env, h, "nginx")
    assert _patch(env, a, {"watch_services": []}).status_code == 200
    assert (a, "nginx") not in env.db.services and (b, "nginx") in env.db.services


def test_patch_that_only_changes_the_interval_keeps_the_states(env):
    hid = _host(env)
    _patch(env, hid, {"watch_services": ["nginx"]})
    _svc(env, hid, "nginx")
    assert _patch(env, hid, {"interval_seconds": 120}).status_code == 200
    assert (hid, "nginx") in env.db.services


def test_service_view_is_pure_and_never_invents_a_state(env):
    now = utcnow()
    rows = [dict(service_name="a", state="stopped", state_since=now - timedelta(seconds=5), observed_at=now)]
    out = env.mod._service_view(rows, ["a", "b"], now)
    assert out["pending"] == ["b"] and out["summary"]["pending"] == 1 and out["summary"]["stopped"] == 1
    assert env.mod._service_view([], None, now) == {"services": [], "pending": [],
                                                    "summary": {"total": 0, "running": 0, "stopped": 0, "failed": 0, "unknown": 0, "pending": 0}}
