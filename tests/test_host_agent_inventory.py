# tests/test_host_agent_inventory.py
"""Host agent service inventory (migration 131): payload validation (pure), the ingest write (newest scan wins, complete
snapshot replaces, empty means no news), and GET /api/host-agents/{id}/service-inventory (scoping, plumbing flag, watched
flag, typo detection)."""
import time
from datetime import timedelta
from pathlib import Path

import pytest

from tests.conftest import load_module
from tests.hostagent_support import Env, utcnow

BASE = "/api/host-agents"
ROOT = Path(__file__).resolve().parent.parent
mm = load_module("app/hostagents/metric_map.py")


def _item(name="nginx", state="running", startup="enabled", description="web server"):
    return {"name": name, "state": state, "startup": startup, "description": description}


# ── pure validation ──────────────────────────────────────────────────────

def test_absent_or_malformed_inventory_is_empty_never_an_error():
    for raw in (None, "x", 5, {}, [None, 3, "a", []]):
        assert mm.clean_inventory(raw) == []


def test_valid_entries_are_normalised_and_deduplicated_first_wins():
    got = mm.clean_inventory([_item("nginx", description="  web \n server  "), _item("nginx", "stopped"), _item("cron", "stopped", "")])
    assert got == [("nginx", "running", "enabled", "web server"), ("cron", "stopped", "", "web server")]


@pytest.mark.parametrize("bad", [
    {"name": "-rf", "state": "running"}, {"name": "", "state": "running"}, {"name": "a b", "state": "running"},
    {"name": "x" * 101, "state": "running"}, {"name": "nginx", "state": "active"}, {"name": "nginx"}, {"name": 7, "state": "running"},
    {"name": "a/b", "state": "running"},
])
def test_names_the_watch_list_would_refuse_are_dropped(bad):
    assert mm.clean_inventory([bad, _item("ok")]) == [("ok", "running", "enabled", "web server")]


def test_inventory_is_capped_and_long_text_is_trimmed():
    many = [_item(f"svc{i}") for i in range(mm.MAX_INVENTORY + 50)]
    assert len(mm.clean_inventory(many)) == mm.MAX_INVENTORY
    got = mm.clean_inventory([_item("a", startup="s" * 99, description="d" * 999)])[0]
    assert len(got[2]) == mm.MAX_INVENTORY_STARTUP_LEN and len(got[3]) == mm.MAX_INVENTORY_DESC_LEN


def test_every_inventory_name_satisfies_the_api_watch_list_rule():
    src = (ROOT / "app/api/host_agents.py").read_text()
    assert '_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:\\-]*$")' in src
    assert mm.INVENTORY_NAME_RE.pattern == r"^[A-Za-z0-9][A-Za-z0-9_.@:\-]*$"


def test_payload_with_inventory_validates_and_without_it_is_unchanged():
    now = int(time.time())
    body = {"schema": 1, "agent_version": "0.2.2", "config_version": 1, "seq": 1, "sent_at": now,
            "host": {"hostname": "h", "instance_id": None, "os": "linux", "arch": "amd64", "boot_time": now - 9},
            "metrics": [], "services": []}
    clean, rejected = mm.validate_payload(body, max_batch_metrics=500)
    assert clean["inventory"] == [] and rejected == 0
    clean, rejected = mm.validate_payload({**body, "service_inventory": [_item(), {"name": "bad name", "state": "running"}]},
                                          max_batch_metrics=500)
    assert [i[0] for i in clean["inventory"]] == ["nginx"] and rejected == 0   # picker data never counts as rejected samples


# ── API ──────────────────────────────────────────────────────────────────

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_ENABLED", "true")
    return Env()


def _host(env, account=1):
    k = env.make_key(account=account, max_uses=5)
    return env.enroll(k["key"], "web-1").json()["host_agent_id"]


def _inv(env, hid, name, state="running", startup="enabled", description="", age_s=120):
    env.db.inventory[(hid, name)] = dict(state=state, startup=startup, description=description,
                                         scanned_at=utcnow() - timedelta(seconds=age_s))


def _get(env, hid, perms=("hostagents.view",), role="viewer", accessible=None):
    env.as_user(role, set(perms), accessible=accessible)
    return env.client.get(f"{BASE}/{hid}/service-inventory")


def test_inventory_lists_services_with_major_and_watched_flags(env):
    hid = _host(env)
    env.as_user("admin")
    assert env.client.patch(f"{BASE}/{hid}/config", json={"watch_services": ["nginx"]}).status_code == 200
    _inv(env, hid, "nginx", "running", "enabled", "A high performance web server")
    _inv(env, hid, "cron", "running", "enabled")
    _inv(env, hid, "systemd-journald", "running", "static")
    _inv(env, hid, "mysql", "stopped", "disabled")
    body = _get(env, hid).json()
    assert body["hostname"] == "web-1" and body["total"] == 4
    by = {i["name"]: i for i in body["items"]}
    assert by["nginx"]["watched"] is True and by["cron"]["watched"] is False
    assert by["systemd-journald"]["major"] is False and by["mysql"]["major"] is True    # disabled is still "installed by a person"
    assert by["nginx"]["description"] == "A high performance web server"
    assert body["scanned_at"].endswith("Z") and 100 <= body["scan_age_seconds"] <= 200
    assert body["missing_watched"] == []


def test_watched_name_not_found_on_the_host_is_reported_not_hidden(env):
    hid = _host(env)
    env.as_user("admin")
    env.client.patch(f"{BASE}/{hid}/config", json={"watch_services": ["nginx", "nginxS"]})
    _inv(env, hid, "nginx")
    assert _get(env, hid).json()["missing_watched"] == ["nginxS"]


def test_no_scan_yet_is_an_empty_list_and_never_a_false_typo_warning(env):
    hid = _host(env)
    env.as_user("admin")
    env.client.patch(f"{BASE}/{hid}/config", json={"watch_services": ["nginx"]})
    body = _get(env, hid).json()
    assert body["items"] == [] and body["scanned_at"] is None and body["missing_watched"] == []


def test_inventory_is_scoped_and_permissioned_like_the_other_reads(env):
    hid = _host(env, account=1)
    assert _get(env, hid, accessible={2}).status_code == 404            # other account's host looks nonexistent
    assert _get(env, hid, perms=()).status_code == 403
    assert _get(env, 9999).status_code == 404


def test_the_read_writes_nothing(env):
    hid = _host(env)
    _inv(env, hid, "nginx")
    env.db.sql_log.clear()
    assert _get(env, hid).status_code == 200
    assert all(q.startswith("SELECT") for q, _ in env.db.sql_log)
