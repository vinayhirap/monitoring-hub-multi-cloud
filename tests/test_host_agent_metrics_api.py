# tests/test_host_agent_metrics_api.py
"""
Host agent patch 0008: GET /api/host-agents/{id}/metrics (latest stored reading per agent metric, DB read only) and the
pure metric_map.describe_stored_name() it relies on. MiniDb (tests/hostagent_support.py) fails loudly on any SQL it does
not know, so "no write, no cloud call" is also enforced by the statement log below.
"""
import re
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


def _host(env, account=1, hostname="web-1", resource_id=None):
    k = env.make_key(account=account, max_uses=5)
    hid = env.enroll(k["key"], hostname).json()["host_agent_id"]
    env.db.hosts[hid]["resource_id"] = resource_id
    return hid


def _resource(env, rid, account, rtype, resource_id):
    env.db.resources[rid] = dict(id=rid, aws_account_id=account, resource_type=rtype, resource_id=resource_id)


def _m(env, rid, name, value, age=30, table="metrics"):
    getattr(env.db, table).append(dict(resource_id=rid, metric_name=name, metric_value=value,
                                       metric_timestamp=utcnow() - timedelta(seconds=age)))


def _get(env, hid):
    env.as_user("viewer", {"hostagents.view"})
    return env.client.get(f"{BASE}/{hid}/metrics")


# ── describe_stored_name (pure) ──────────────────────────────────────────────

def test_describe_covers_every_contract_metric_and_inverts_stored_name(env):
    mm = env.metric_map
    assert set(mm.DISPLAY) == set(mm.METRICS)                       # a new contract metric must get a unit and a title
    for contract, (_base, label_key, _kind) in mm.METRICS.items():
        label = {"mount": "/data", "iface": "eth0"}.get(label_key)
        info = mm.describe_stored_name(mm.stored_name(contract, label))
        assert info and info["contract"] == contract, contract
        assert info["unit"] == mm.DISPLAY[contract][0]
    root = mm.describe_stored_name("disk_used_percent")
    assert root["label"] == "/" and root["label_key"] == "mount"
    assert mm.describe_stored_name("disk_used_percent__var_log")["label"] == "var_log"
    assert mm.describe_stored_name("host_net_rx_bytes_per_s__eth0")["label"] == "eth0"


@pytest.mark.parametrize("name", ["CPUUtilization", "NetworkIn", "disk_used_percent__", "host_net_rx_bytes_per_s",
                                  "host_cpu_percent__x", "host_unknown", "", None, "mem_used_percent__x"])
def test_describe_rejects_names_an_agent_cannot_feed(env, name):
    assert env.metric_map.describe_stored_name(name) is None


# ── endpoint ─────────────────────────────────────────────────────────────────

def test_returns_latest_value_per_metric_with_units_ordered_and_only_agent_names(env):
    hid = _host(env, resource_id="host:web-1")
    _resource(env, 10, 1, "host", "host:web-1")
    _m(env, 10, "host_uptime_seconds", 86400.0)
    _m(env, 10, "mem_used_percent", 41.5, age=45)
    _m(env, 10, "host_cpu_percent", 12.25, age=5)
    _m(env, 10, "disk_used_percent", 63.0)
    _m(env, 10, "disk_used_percent__var_log", 20.0)
    _m(env, 10, "host_net_rx_bytes_per_s__eth0", 1024.0)
    _m(env, 10, "CPUUtilization", 99.0)                              # not an agent series: must not appear
    _m(env, 99, "host_cpu_percent", 77.0)                            # another resource: must not appear
    r = _get(env, hid)
    assert r.status_code == 200
    body = r.json()
    assert body["hostname"] == "web-1" and body["resource_type"] == "host" and body["shared_series"] is False
    assert body["source"] == "metrics"
    assert [m["name"] for m in body["metrics"]] == [
        "host_cpu_percent", "mem_used_percent", "disk_used_percent", "disk_used_percent__var_log",
        "host_net_rx_bytes_per_s__eth0", "host_uptime_seconds"]
    cpu = body["metrics"][0]
    assert cpu["value"] == 12.25 and cpu["unit"] == "%" and cpu["metric"] == "cpu_percent" and cpu["label"] is None
    assert cpu["timestamp"].endswith("Z") and 4 <= cpu["age_seconds"] <= 15
    assert body["metrics"][2]["label"] == "/" and body["metrics"][3]["label"] == "var_log"
    assert body["metrics"][4]["unit"] == "bytes/s"
    assert set(cpu) == {"name", "metric", "title", "label", "label_key", "unit", "value", "timestamp", "age_seconds"}


def test_never_written_values_are_not_invented(env):
    hid = _host(env)                                                 # enrolled, never ingested: resource_id is NULL
    body = _get(env, hid).json()
    assert body["metrics"] == [] and body["resource_type"] is None and body["source"] is None
    hid2 = _host(env, hostname="web-2", resource_id="host:web-2")    # resource_id set but no resources row
    assert _get(env, hid2).json()["metrics"] == []
    _resource(env, 11, 1, "host", "host:web-2")                      # resource but no readings
    assert _get(env, hid2).json()["metrics"] == []


def test_null_value_or_timestamp_rows_are_skipped(env):
    hid = _host(env, resource_id="host:web-1")
    _resource(env, 10, 1, "host", "host:web-1")
    env.db.metrics.append(dict(resource_id=10, metric_name="host_load1", metric_value=None, metric_timestamp=utcnow()))
    env.db.metrics.append(dict(resource_id=10, metric_name="host_load5", metric_value=0.5, metric_timestamp=None))
    _m(env, 10, "host_load15", 0.25)
    assert [m["name"] for m in _get(env, hid).json()["metrics"]] == ["host_load15"]


def test_attached_ec2_resource_is_flagged_as_shared_and_cloudwatch_series_are_excluded(env):
    hid = _host(env, resource_id="i-0abc")
    _resource(env, 20, 1, "ec2", "i-0abc")
    _resource(env, 21, 1, "host", "i-0abc")                          # same id under another type must not be picked
    _m(env, 20, "mem_used_percent", 55.0)
    _m(env, 20, "CPUUtilization", 3.0)
    _m(env, 20, "NetworkIn", 10.0)
    _m(env, 21, "host_cpu_percent", 1.0)
    body = _get(env, hid).json()
    assert body["resource_type"] == "ec2" and body["shared_series"] is True
    assert [m["name"] for m in body["metrics"]] == ["mem_used_percent"]


def test_history_fallback_only_when_metrics_has_no_agent_rows_and_only_last_24h(env):
    hid = _host(env, resource_id="host:web-1")
    _resource(env, 10, 1, "host", "host:web-1")
    _m(env, 10, "host_cpu_percent", 10.0, age=7200, table="history")
    _m(env, 10, "host_cpu_percent", 20.0, age=600, table="history")           # newest wins
    _m(env, 10, "host_load1", 1.0, age=25 * 3600, table="history")             # older than 24 h: not shown
    body = _get(env, hid).json()
    assert body["source"] == "metric_history"
    assert [(m["name"], m["value"]) for m in body["metrics"]] == [("host_cpu_percent", 20.0)]
    _m(env, 10, "host_load5", 2.0)                                              # metrics now has an agent row
    body = _get(env, hid).json()
    assert body["source"] == "metrics" and [m["name"] for m in body["metrics"]] == ["host_load5"]


def test_revoked_host_still_shows_its_last_readings(env):
    hid = _host(env, resource_id="host:web-1")
    _resource(env, 10, 1, "host", "host:web-1")
    _m(env, 10, "host_load1", 0.1)
    env.as_user("admin")
    assert env.client.post(f"{BASE}/{hid}/revoke").status_code == 200
    assert _get(env, hid).json()["metrics"][0]["name"] == "host_load1"


# ── permissions, scope, flag, read-only ──────────────────────────────────────

def test_permission_scope_unknown_id_and_flag(env, monkeypatch):
    h1 = _host(env, account=1, hostname="one", resource_id="host:one")
    h2 = _host(env, account=2, hostname="two", resource_id="host:two")
    _resource(env, 10, 1, "host", "host:one")
    _resource(env, 11, 2, "host", "host:two")
    _m(env, 10, "host_load1", 1.0)
    _m(env, 11, "host_load1", 2.0)
    env.user = None
    assert env.client.get(f"{BASE}/{h1}/metrics").status_code == 401
    env.as_user("viewer", {"help.view"})
    assert env.client.get(f"{BASE}/{h1}/metrics").status_code == 403
    env.as_user("viewer", {"hostagents.view"}, accessible={1})
    assert env.client.get(f"{BASE}/{h1}/metrics").status_code == 200
    assert env.client.get(f"{BASE}/{h2}/metrics").status_code == 404           # other account: same answer as unknown
    assert env.client.get(f"{BASE}/9999/metrics").status_code == 404
    env.as_user("viewer", {"hostagents.view"}, accessible=set())
    assert env.client.get(f"{BASE}/{h1}/metrics").status_code == 404
    env.as_user("viewer", {"hostagents.view"})                                  # None = full access
    assert env.client.get(f"{BASE}/{h2}/metrics").json()["metrics"][0]["value"] == 2.0
    monkeypatch.setenv("HOST_AGENT_ENABLED", "false")
    assert env.client.get(f"{BASE}/{h1}/metrics").status_code == 404


def test_endpoint_is_read_only(env):
    hid = _host(env, resource_id="host:web-1")
    _resource(env, 10, 1, "host", "host:web-1")
    _m(env, 10, "host_load1", 1.0)
    before_hosts, before_audit = dict(env.db.hosts[hid]), len(env.audit)
    env.db.sql_log.clear()
    assert _get(env, hid).status_code == 200
    assert env.db.sql_log and all(q.startswith("SELECT") for q, _ in env.db.sql_log)
    assert env.db.hosts[hid] == before_hosts and len(env.audit) == before_audit


def test_source_has_no_cloud_call_no_migration_and_leaves_the_ai_agent_alone():
    src = (ROOT / "app/api/host_agents.py").read_text()
    block = src[src.index("# ── current metrics of one host"):src.index("# ── enrollment keys")]
    code = re.sub(r'"""(.|\n)*?"""', "", block)                       # docstrings may mention what the route does NOT do
    assert not re.search(r"boto3|cloudwatch|get_metric_data|requests\.|httpx|azure|google\.cloud", code, re.I)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE|ALTER|DROP)\b", code)
    assert not re.search(r"^(import|from) .*(boto3|botocore|requests|httpx|azure|google)", src, re.M)
    assert "app.agent" not in src and "/api/agent" not in src
    nums = sorted(p.name[:3] for p in (ROOT / "db/migrations").glob("1[0-9][0-9]_*.sql"))
    assert "122" in nums and "123" in nums                                      # 123 belongs to patch 0009 (services), not to this route


def test_help_and_docs_describe_the_current_metrics_panel():
    help_src = (ROOT / "app/help_content/host_agents.py").read_text()
    assert "Metric history" in help_src and "estimated" in help_src
    assert "/api/host-agents/{id}/metrics" in (ROOT / "docs/HOST_AGENT.md").read_text()
    jsx = (ROOT / "frontend/src/pages/HostAgents.jsx").read_text()
    assert "getHostAgentMetrics" in jsx and "No API serves metrics" not in jsx
