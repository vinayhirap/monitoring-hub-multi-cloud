# tests/test_host_agent_ingest.py
"""Phase C: POST /api/host-agents/ingest, metric mapping, instance_id attach, seq idempotency, heartbeat cost, and the
HOST_AGENT_PREFER_OVER_CWAGENT dedup hooks in the collector. No database: a recording fake connection."""
import gzip
import importlib
import json
import os
import sys
import time
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests.conftest import REPO_ROOT, install_stub, install_real_utils_package, install_polling_modules, load_module

TOKEN = "cah_" + "a" * 40
AGENT = {"host_agent_id": 5, "aws_account_id": 3, "hostname": "web-1", "resource_id": None, "config_version": 1}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    def _is_app(n):
        return n == "app" or n.startswith("app.")
    saved = {n: m for n, m in sys.modules.items() if _is_app(n) and m is not None}
    dicts = {n: dict(m.__dict__) for n, m in saved.items()}
    for k in ("HOST_AGENT_ENABLED", "HOST_AGENT_PREFER_OVER_CWAGENT", "HOST_AGENT_MAX_BODY_BYTES",
              "HOST_AGENT_STALE_SECONDS", "HOST_AGENT_INGEST_RATE_PER_MIN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SHARED_CACHE_BACKEND", "memory")
    yield
    for n in [n for n in list(sys.modules) if _is_app(n) and n not in saved]:
        del sys.modules[n]
    for n, m in saved.items():
        sys.modules[n] = m
        m.__dict__.clear()
        m.__dict__.update(dicts[n])


# ── fakes ────────────────────────────────────────────────────────────────

class FakeDB:
    def __init__(self):
        self.connections = 0
        self.commits = 0
        self.executed = []            # (normalized sql, params)
        self.many = []                # (normalized sql, rows)
        self.duplicate = False        # claim UPDATE matches no row
        self.ec2 = {}                 # instance_id -> resources.id
        self.host_rid = 900
        self.hijacked = False         # another active agent already holds the instance
        self.config_row = {"config_json": None, "config_version": 2}
        self.fail_many_once = None

    def get_connection(self):
        self.connections += 1
        return FakeConn(self)


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self, dictionary=True):
        return FakeCur(self.db)

    def commit(self):
        self.db.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


class FakeCur:
    def __init__(self, db):
        self.db, self.rowcount, self._rows = db, 1, []

    def execute(self, sql, params=None):
        n = " ".join(sql.split())
        self.db.executed.append((n, params))
        self.rowcount = 0 if (self.db.duplicate and n.startswith("UPDATE host_agents SET last_seq = %s,")) else 1
        self._rows = []
        if "FROM resources" in n and "resource_type = 'ec2'" in n:
            iid = params[1]
            self._rows = [{"id": self.db.ec2[iid]}] if iid in self.db.ec2 else []
        elif "SELECT id FROM resources" in n:
            self._rows = [{"id": self.db.host_rid}]
        elif n.startswith("SELECT 1 AS x FROM host_agents"):
            self._rows = [{"x": 1}] if self.db.hijacked else []
        elif n.startswith("SELECT config_json"):
            self._rows = [self.db.config_row]

    def executemany(self, sql, rows):
        if self.db.fail_many_once is not None:
            exc, self.db.fail_many_once = self.db.fail_many_once, None
            raise exc
        self.db.many.append((" ".join(sql.split()), list(rows)))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class Deadlock(Exception):
    errno = 1213


def _payload(metrics=None, services=None, seq=10, config_version=1, instance_id=None, now=None):
    now = int(now or time.time())
    return {"schema": 1, "agent_version": "0.1.0", "config_version": config_version, "seq": seq, "sent_at": now,
            "host": {"hostname": "web-1", "instance_id": instance_id, "os": "linux", "arch": "amd64", "boot_time": now - 99},
            "metrics": metrics if metrics is not None else [], "services": services or []}


def _m(name, value, labels=None, ts=None):
    return {"name": name, "value": value, "ts": int(ts or time.time()), "labels": labels or {}}


class Env:
    pass


def _env(auth=lambda token: dict(AGENT), rate=None, enabled=True, monkeypatch=None):
    if enabled:
        os.environ["HOST_AGENT_ENABLED"] = "true"
    install_real_utils_package()
    e = Env()
    e.db = FakeDB()
    e.audit = []
    e.auth_calls = []
    e.rate_calls = []

    def _auth(token):
        e.auth_calls.append(token)
        return auth(token)

    def _rate(key, mx, window):
        e.rate_calls.append((key, mx, window))
        if rate:
            raise rate

    install_stub("app.db", get_connection=e.db.get_connection)
    install_stub("app.auth.rate_limit", check_rate_limit=_rate)
    install_stub("app.audit", write_audit=lambda *a, **k: e.audit.append((a, k)))
    install_stub("app.collector.disk_mounts", ensure_disk_mount_metric_registered=lambda *a, **k: e.audit.append(("mount", a)))
    install_stub("app.hostagents.auth", authenticate_agent=_auth)
    pkg = sys.modules["app.hostagents"] if "app.hostagents" in sys.modules else install_stub("app.hostagents")
    pkg.__path__ = [os.path.join(REPO_ROOT, "app", "hostagents")]
    for n in [n for n in sys.modules if n.startswith("app.hostagents.") and n != "app.hostagents.auth"]:
        del sys.modules[n]
    # Catalog registration opens its own connection after the commit; recorded here (tests/test_host_agent_catalog.py covers it).
    e.catalog_calls = []
    install_stub("app.hostagents.catalog",
                 ensure_host_metrics_registered=lambda *a, **k: e.catalog_calls.append(a) or True)
    e.ingest = importlib.import_module("app.hostagents.ingest")
    e.ingest._REGISTERED.clear()
    app = FastAPI()
    app.include_router(e.ingest.router)
    e.client = TestClient(app)
    return e


def _post(e, body, token=TOKEN, headers=None, raw=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    h.update(headers or {})
    if raw is not None:
        return e.client.post("/api/host-agents/ingest", content=raw, headers=h)
    return e.client.post("/api/host-agents/ingest", json=body, headers=h)


# ── gating / auth / limits ───────────────────────────────────────────────

def test_flag_off_is_404_and_touches_nothing():
    e = _env(enabled=False)
    r = _post(e, _payload())
    assert r.status_code == 404
    assert e.db.connections == 0 and e.auth_calls == []


@pytest.mark.parametrize("hdr", [None, "Basic abc", "Bearer short", "Bearer cah_" + "!" * 40, "Bearer " + "x" * 44])
def test_bad_token_shape_is_401_without_any_lookup(hdr):
    e = _env()
    r = e.client.post("/api/host-agents/ingest", json=_payload(), headers={"Authorization": hdr} if hdr else {})
    assert r.status_code == 401 and e.auth_calls == [] and e.db.connections == 0
    assert r.headers["www-authenticate"] == "Bearer"


def test_unknown_or_revoked_token_is_401():
    e = _env(auth=lambda t: None)
    assert _post(e, _payload()).status_code == 401 and e.db.connections == 0 and e.rate_calls == []


def test_auth_backend_error_is_503_not_a_crash():
    def boom(t):
        raise RuntimeError("db down")
    e = _env(auth=boom)
    r = _post(e, _payload())
    assert r.status_code == 503 and e.db.connections == 0


def test_rate_limit_429_before_any_db_work_and_keyed_by_agent_id():
    e = _env(rate=HTTPException(429, "slow down"))
    r = _post(e, _payload())
    assert r.status_code == 429 and e.db.connections == 0
    assert e.rate_calls == [("hostagent:ingest:5", 30, 60)]


def test_rate_limit_uses_env_value(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_INGEST_RATE_PER_MIN", "7")
    e = _env()
    _post(e, _payload())
    assert e.rate_calls[0][1] == 7


def test_oversize_declared_length_is_413_before_auth(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_MAX_BODY_BYTES", "2048")
    e = _env()
    r = _post(e, None, raw=b"x" * 5000)
    assert r.status_code == 413 and e.auth_calls == [] and e.db.connections == 0


def test_oversize_streamed_body_without_content_length_is_413(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_MAX_BODY_BYTES", "2048")
    e = _env()
    r = e.client.post("/api/host-agents/ingest", content=(b"y" * 1000 for _ in range(10)),
                      headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 413 and e.db.connections == 0


def _gz(obj_or_bytes):
    raw = obj_or_bytes if isinstance(obj_or_bytes, bytes) else json.dumps(obj_or_bytes).encode()
    return gzip.compress(raw)


GZ = {"Content-Encoding": "gzip", "Content-Type": "application/json"}


def test_gzip_body_is_decoded_exactly_like_the_agent_sends_it():
    """Regression: the Go agent gzips by default; phase C first shipped without decoding it and every real payload was 400."""
    e = _env()
    r = _post(e, None, raw=_gz(_payload(metrics=[_m("mem_used_percent", 41.5)], seq=12)), headers=GZ)
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 1 and r.json()["rejected"] == 0
    assert e.db.connections == 1 and len(e.db.many) >= 1


def test_gzip_and_plain_bodies_give_identical_results():
    a, b = _env(), _env()
    p = _payload(metrics=[_m("cpu_percent", 12.5), _m("disk_used_percent", 71, {"mount": "/"})], seq=3)
    ra = _post(a, None, raw=_gz(p), headers=GZ)
    rb = _post(b, p)
    assert ra.status_code == rb.status_code == 200 and ra.json() == rb.json()

    def strip(rows):   # the stored timestamp is the server's "now", which differs by microseconds between two requests
        return [(sql, [tuple(v for v in r if not hasattr(v, "year")) for r in rs]) for sql, rs in rows]
    assert [x[0] for x in a.db.executed] == [x[0] for x in b.db.executed]
    assert strip(a.db.many) == strip(b.db.many)


def test_identity_content_encoding_is_plain_json():
    e = _env()
    r = _post(e, None, raw=json.dumps(_payload(seq=4)).encode(), headers={"Content-Encoding": "identity"})
    assert r.status_code == 200


def test_gzip_bomb_is_413_and_never_reaches_the_database(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_MAX_BODY_BYTES", "8192")
    e = _env()
    bomb = _gz(b" " * 6_000_000)               # ~6 KB on the wire (< cap), 6 MB inflated (>> cap)
    assert len(bomb) < 8192
    r = _post(e, None, raw=bomb, headers=GZ)
    assert r.status_code == 413 and e.db.connections == 0


def test_corrupt_and_truncated_gzip_are_400():
    e = _env()
    assert _post(e, None, raw=b"this is not gzip", headers=GZ).status_code == 400
    good = _gz(_payload(metrics=[_m("mem_used_percent", 41.5)]))
    assert _post(e, None, raw=good[: len(good) // 2], headers=GZ).status_code == 400
    assert e.db.connections == 0


def test_gzip_of_non_json_is_400_and_unknown_encoding_is_415():
    e = _env()
    assert _post(e, None, raw=_gz(b"{not json"), headers=GZ).status_code == 400
    assert _post(e, None, raw=b"whatever", headers={"Content-Encoding": "br"}).status_code == 415
    assert _post(e, None, raw=b"whatever", headers={"Content-Encoding": "deflate"}).status_code == 415


def test_gzip_is_only_decoded_after_authentication():
    e = _env(auth=lambda t: None)
    r = _post(e, None, raw=_gz(_payload()), headers=GZ)
    assert r.status_code == 401 and e.db.connections == 0


def test_invalid_json_and_unknown_schema_are_400():
    e = _env()
    assert _post(e, None, raw=b"{not json").status_code == 400
    bad = _payload(); bad["schema"] = 2
    assert _post(e, bad).status_code == 400
    assert e.db.connections == 0


def test_too_many_metrics_is_413():
    e = _env()
    big = _payload([_m("cpu_percent", 1.0)] * 501)
    assert _post(e, big).status_code == 413 and e.db.connections == 0


# ── heartbeat cost ───────────────────────────────────────────────────────

def test_heartbeat_is_one_connection_one_update():
    e = _env()
    r = _post(e, _payload(seq=77))
    assert r.status_code == 200
    assert r.json() == {"accepted": 0, "rejected": 0, "config_version": 1}
    assert e.db.connections == 1 and e.db.commits == 1
    assert len(e.db.executed) == 1 and e.db.many == []
    sql, params = e.db.executed[0]
    assert sql.startswith("UPDATE host_agents SET") and params == (77, 77, "0.1.0", 5)


def test_heartbeat_sql_assigns_last_seq_before_last_seen_at():
    e = _env()
    sql = e.ingest.SQL_HEARTBEAT
    assert sql.index("last_seq =") < sql.index("last_seen_at = UTC_TIMESTAMP()")


def test_payload_without_services_stays_a_single_update():
    e = _env()
    r = _post(e, _payload(services=[]))
    assert r.status_code == 200 and len(e.db.executed) == 1 and e.db.many == []


def test_services_only_payload_stores_states_and_prunes_the_rest():
    e = _env()
    r = _post(e, _payload(services=[{"name": "sshd", "state": "running"}, {"name": "nginx", "state": "failed"}]))
    assert r.status_code == 200 and r.json()["accepted"] == 0
    assert e.db.executed[0][0].startswith("UPDATE host_agents SET")
    sql, rows = e.db.many[0]
    assert sql == e.ingest.SQL_UPSERT_SERVICES
    assert [(r[0], r[1], r[2]) for r in rows] == [(5, "nginx", "failed"), (5, "sshd", "running")]      # name order, agent id from the token
    assert all(r[3] == r[4] for r in rows)
    prune_sql, prune_params = e.db.executed[1]
    assert prune_sql.startswith("DELETE FROM host_agent_services WHERE host_agent_id = %s AND service_name NOT IN (%s, %s)")
    assert prune_params == (5, "nginx", "sshd")
    assert e.db.commits == 1


def test_services_with_samples_are_written_in_the_same_transaction():
    e = _env()
    r = _post(e, _payload(metrics=[_m("cpu_percent", 10)], services=[{"name": "nginx", "state": "running"}]))
    assert r.status_code == 200 and r.json()["accepted"] == 1
    assert e.db.connections == 1 and e.db.commits == 1
    assert [s for s, _ in e.db.many][-1] == e.ingest.SQL_UPSERT_SERVICES


def test_services_are_ignored_while_the_agent_runs_an_older_config():
    # The admin removed a service (config_version 1 -> 2). The agent still reports the OLD list with version 1 and must not
    # bring the removed service back.
    e = _env(auth=lambda t: dict(AGENT, config_version=2))
    e.db.config_row = {"config_json": None, "config_version": 2}
    r = _post(e, _payload(config_version=1, services=[{"name": "removed-svc", "state": "running"}]))
    assert r.status_code == 200
    assert not any("host_agent_services" in sql for sql, _ in e.db.executed) and e.db.many == []
    assert r.json()["config_version"] == 2                           # and it is told to resync


def test_duplicate_seq_writes_no_services():
    e = _env()
    e.db.duplicate = True
    r = _post(e, _payload(metrics=[_m("cpu_percent", 10)], services=[{"name": "nginx", "state": "running"}]))
    assert r.status_code == 200
    assert not any("host_agent_services" in sql for sql, _ in e.db.executed) and e.db.many == []


def test_a_name_sent_twice_keeps_the_last_state_and_one_row():
    e = _env()
    _post(e, _payload(services=[{"name": "nginx", "state": "running"}, {"name": "nginx", "state": "failed"}]))
    _sql, rows = e.db.many[0]
    assert len(rows) == 1 and rows[0][2] == "failed"


def test_service_observed_time_is_clamped_to_a_sane_window():
    e = _env()
    now = datetime(2026, 10, 24, 12, 0, 0)
    f = e.ingest._service_observed_at
    ok = int((now - timedelta(minutes=3)).timestamp())
    assert f(ok, now) == datetime.utcfromtimestamp(ok)
    for bad in (int((now + timedelta(hours=5)).timestamp()), int((now - timedelta(days=3)).timestamp()), "x", None, 10**20):
        assert f(bad, now) == now                                      # a wrong agent clock cannot pin a state in the future


def test_service_upsert_sql_is_newest_wins_and_assigns_columns_in_a_safe_order():
    e = _env()
    sql = e.ingest.SQL_UPSERT_SERVICES
    assert sql.index("state_since =") < sql.index(" state = IF") < sql.index("observed_at = GREATEST")
    assert "VALUES(observed_at) >= observed_at" in sql


# ── config ───────────────────────────────────────────────────────────────

def test_config_returned_only_when_version_differs():
    e = _env(auth=lambda t: dict(AGENT, config_version=2))
    assert "config" not in _post(e, _payload(config_version=2)).json()
    assert len(e.db.executed) == 1                                   # versions equal: no config SELECT either
    e.db.executed.clear()
    e.db.config_row = {"config_json": json.dumps({"interval_seconds": 30, "watch_services": ["nginx"], "evil": 1}),
                       "config_version": 2}
    body = _post(e, _payload(config_version=1)).json()
    assert body["config_version"] == 2
    assert body["config"]["interval_seconds"] == 30 and body["config"]["watch_services"] == ["nginx"]
    assert "evil" not in body["config"] and body["config"]["max_batch_metrics"] == 500


def test_config_with_null_config_json_is_the_contract_defaults():
    e = _env(auth=lambda t: dict(AGENT, config_version=1))
    e.db.config_row = {"config_json": None, "config_version": 1}
    assert _post(e, _payload(config_version=0)).json()["config"] == e.ingest.DEFAULT_CONFIG


def test_config_returned_when_agent_claims_a_different_version_than_the_server():
    e = _env()
    e.db.config_row = {"config_json": None, "config_version": 1}
    body = _post(e, _payload(config_version=4)).json()          # agent AHEAD of the server still differs: resync it
    assert body["config_version"] == 1 and body["config"] == e.ingest.DEFAULT_CONFIG


# ── samples: write path ──────────────────────────────────────────────────

def test_samples_create_host_resource_and_batch_write():
    e = _env()
    now = int(time.time())
    ms = [_m("cpu_percent", 12.5, ts=now), _m("mem_used_percent", 40, ts=now),
          _m("disk_used_percent", 71, {"mount": "/"}, ts=now), _m("disk_used_percent", 55, {"mount": "/data"}, ts=now),
          _m("net_rx_bytes_per_s", 10, {"iface": "eth0"}, ts=now), _m("cpu_percent", 99, ts=now - 30)]
    r = _post(e, _payload(ms))
    assert r.status_code == 200 and r.json()["accepted"] == 6 and r.json()["rejected"] == 0
    assert e.db.connections == 1 and e.db.commits == 1
    assert len(e.db.many) == 2                                       # one executemany per table, no per-metric statements
    (_, last), (_, hist) = e.db.many
    assert {row[1] for row in last} == {"host_cpu_percent", "mem_used_percent", "disk_used_percent",
                                        "disk_used_percent__data", "host_net_rx_bytes_per_s__eth0"}
    assert {row[0] for row in last} == {900}
    assert len(hist) == 6
    cpu = next(row for row in last if row[1] == "host_cpu_percent")
    assert cpu[2] == 12.5                                             # newest sample wins the last-value row
    assert any("INSERT INTO resources" in s and p[1] == "host" and p[2] == "host:web-1" for s, p in e.db.executed)
    assert any(s.startswith("UPDATE host_agents SET resource_id") and p == ("host:web-1", 5) for s, p in e.db.executed)
    assert [a[0][1] for a in e.audit if a[0] and a[0][0] != "mount"] == ["Host agent resource created"]


def test_instance_id_attaches_to_existing_ec2_resource():
    e = _env()
    e.db.ec2["i-0abc"] = 77
    r = _post(e, _payload([_m("mem_used_percent", 40)], instance_id="i-0abc"))
    assert r.status_code == 200
    assert not any("INSERT INTO resources" in s for s, _ in e.db.executed)
    assert any(s.startswith("UPDATE host_agents SET resource_id") and p[0] == "i-0abc" for s, p in e.db.executed)
    assert {row[0] for row in e.db.many[0][1]} == {77}
    assert e.audit[0][0][1] == "Host agent attached to EC2 resource"


def test_already_attached_agent_reuses_ec2_resource_without_extra_writes():
    e = _env(auth=lambda t: dict(AGENT, resource_id="i-0abc"))
    e.db.ec2["i-0abc"] = 77
    _post(e, _payload([_m("mem_used_percent", 40)], instance_id="i-0abc"))
    assert not any(s.startswith("UPDATE host_agents SET resource_id") or "INSERT INTO resources" in s
                   for s, _ in e.db.executed)
    assert e.audit == []


def test_attach_refused_when_another_agent_holds_the_instance():
    e = _env()
    e.db.ec2["i-0abc"] = 77
    e.db.hijacked = True
    _post(e, _payload([_m("mem_used_percent", 40)], instance_id="i-0abc"))
    assert {row[0] for row in e.db.many[0][1]} == {900}              # fell back to the host resource, not row 77


def test_unknown_instance_id_falls_back_to_host_resource():
    e = _env()
    _post(e, _payload([_m("mem_used_percent", 40)], instance_id="i-nothere"))
    assert any("INSERT INTO resources" in s for s, _ in e.db.executed)


def test_attached_ec2_non_root_mount_is_registered_once_per_process():
    e = _env(auth=lambda t: dict(AGENT, resource_id="i-0abc"))
    e.db.ec2["i-0abc"] = 77
    body = _payload([_m("disk_used_percent", 55, {"mount": "/data"})])
    _post(e, body)
    _post(e, dict(body, seq=11))
    assert [a for a in e.audit if a[0] == "mount"] == [("mount", (3, "ec2", "disk_used_percent__data", "/data"))]


def test_host_resource_never_registers_mount_thresholds():
    e = _env()
    _post(e, _payload([_m("disk_used_percent", 55, {"mount": "/data"})]))
    assert [a for a in e.audit if a[0] == "mount"] == []


# ── idempotency ──────────────────────────────────────────────────────────

def test_duplicate_seq_is_accepted_but_not_rewritten():
    e = _env()
    e.db.duplicate = True
    r = _post(e, _payload([_m("cpu_percent", 5.0), _m("load1", 0.2)], seq=3))
    assert r.status_code == 200 and r.json()["accepted"] == 2          # same answer the first delivery got
    assert e.db.many == []
    assert any(s.startswith("UPDATE host_agents SET last_seen_at") for s, _ in e.db.executed)
    assert not any("INSERT INTO resources" in s for s, _ in e.db.executed)


def test_claim_sql_encodes_the_ten_minute_duplicate_window():
    e = _env()
    assert "last_seq < %s" in e.ingest.SQL_CLAIM_SEQ and "INTERVAL 600 SECOND" in e.ingest.SQL_CLAIM_SEQ


def test_deadlock_is_retried_on_a_fresh_connection():
    e = _env()
    e.db.fail_many_once = Deadlock("deadlock")
    r = _post(e, _payload([_m("cpu_percent", 5.0)]))
    assert r.status_code == 200 and e.db.connections == 2 and len(e.db.many) == 2


def test_non_retryable_db_error_is_500_and_never_committed():
    e = _env()
    e.db.fail_many_once = RuntimeError("boom")
    r = _post(e, _payload([_m("cpu_percent", 5.0)]))
    assert r.status_code == 500 and e.db.commits == 0 and "boom" not in r.text


# ── validation matrix (pure) ─────────────────────────────────────────────

def _mm():
    return load_module("app/hostagents/metric_map.py")


def _validate(mm, metrics, services=None, **kw):
    return mm.validate_payload(_payload(metrics, services), max_batch_metrics=500, **kw)


@pytest.mark.parametrize("name,value,labels,ok", [
    ("cpu_percent", 0, {}, True), ("cpu_percent", 100, {}, True), ("cpu_percent", 100.1, {}, False),
    ("cpu_percent", -0.1, {}, False), ("mem_used_percent", 101, {}, False), ("swap_used_percent", 50, {}, True),
    ("load1", 0, {}, True), ("load1", -1, {}, False), ("proc_count", 1e12, {}, True), ("uptime_seconds", -5, {}, False),
    ("disk_used_percent", 50, {"mount": "/"}, True), ("disk_used_percent", 50, {}, False),
    ("disk_used_percent", 50, {"mount": ""}, False), ("disk_used_percent", 50, {"mount": 5}, False),
    ("disk_used_percent", 50, {"mount": "/proc"}, False), ("disk_used_percent", 50, {"mount": "/snap/core/1"}, False),
    ("disk_used_percent", 50, {"mount": "C:"}, True), ("net_rx_bytes_per_s", 1, {"iface": "eth0"}, True),
    ("net_rx_bytes_per_s", 1, {"mount": "/"}, False), ("no_such_metric", 1, {}, False),
    ("cpu_percent", True, {}, False), ("cpu_percent", "12", {}, False), ("cpu_percent", None, {}, False),
])
def test_validation_matrix(name, value, labels, ok):
    mm = _mm()
    clean, rejected = _validate(mm, [_m(name, value, labels)])
    assert (len(clean["samples"]), rejected) == ((1, 0) if ok else (0, 1))


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nan_and_inf_values_are_rejected_per_sample(token):
    e = _env()
    now = int(time.time())
    raw = ('{"schema":1,"agent_version":"0.1.0","config_version":1,"seq":1,"sent_at":%d,'
           '"host":{"hostname":"web-1","instance_id":null},"services":[],"metrics":['
           '{"name":"cpu_percent","value":%s,"ts":%d,"labels":{}},'
           '{"name":"load1","value":0.5,"ts":%d,"labels":{}}]}') % (now, token, now, now)
    r = _post(e, None, raw=raw.encode())
    assert r.status_code == 200 and r.json()["accepted"] == 1 and r.json()["rejected"] == 1


def test_timestamp_window_future_and_past():
    mm = _mm()
    now = 1_760_000_000.0
    cases = {"+11min": (now + 660, False), "+9min": (now + 540, True), "-25h": (now - 25 * 3600, False),
             "-23h": (now - 23 * 3600, True), "now": (now, True)}
    for label, (ts, ok) in cases.items():
        clean, rejected = mm.validate_payload(_payload([_m("cpu_percent", 1.0, ts=ts)], now=now),
                                              max_batch_metrics=500, now=now)
        assert (len(clean["samples"]), rejected) == ((1, 0) if ok else (0, 1)), label
    clean, rejected = mm.validate_payload(_payload([{"name": "cpu_percent", "value": 1, "ts": "x"}], now=now),
                                          max_batch_metrics=500, now=now)
    assert rejected == 1


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(schema=0), lambda b: b.update(schema="1"), lambda b: b.update(schema=True),
    lambda b: b.pop("schema"), lambda b: b.update(seq=-1), lambda b: b.update(seq=1.5), lambda b: b.update(seq=True),
    lambda b: b.update(config_version="1"), lambda b: b.update(sent_at=0), lambda b: b.update(agent_version=""),
    lambda b: b.update(agent_version="x" * 41), lambda b: b.update(host=[]), lambda b: b["host"].update(hostname=""),
    lambda b: b["host"].update(instance_id=7), lambda b: b.update(metrics={}), lambda b: b.update(services="x"),
])
def test_whole_payload_errors_are_400(mutate):
    mm = _mm()
    b = _payload()
    mutate(b)
    with pytest.raises(mm.PayloadError) as ei:
        mm.validate_payload(b, max_batch_metrics=500)
    assert ei.value.status == 400
    with pytest.raises(mm.PayloadError):
        mm.validate_payload([], max_batch_metrics=500)


def test_series_cardinality_cap_and_services_validation():
    mm = _mm()
    ms = [_m("net_rx_bytes_per_s", 1, {"iface": f"veth{i}"}) for i in range(40)]
    clean, rejected = _validate(mm, ms, [{"name": "nginx", "state": "running"}, {"name": "x", "state": "weird"},
                                         {"name": "", "state": "running"}, "junk"])
    assert len(clean["samples"]) == mm.MAX_SERIES_PER_METRIC and rejected == (40 - mm.MAX_SERIES_PER_METRIC) + 3
    assert clean["services"] == [("nginx", "running")]


# ── mapping ──────────────────────────────────────────────────────────────

def test_mapping_table_is_exact():
    mm = _mm()
    assert {k: v[0] for k, v in mm.METRICS.items()} == {
        "cpu_percent": "host_cpu_percent", "load1": "host_load1", "load5": "host_load5", "load15": "host_load15",
        "mem_used_percent": "mem_used_percent", "mem_available_bytes": "host_mem_available_bytes",
        "swap_used_percent": "host_swap_used_percent", "disk_used_percent": "disk_used_percent",
        "disk_inodes_used_percent": "host_disk_inodes_used_percent",
        "disk_read_bytes_per_s": "host_disk_read_bytes_per_s", "disk_write_bytes_per_s": "host_disk_write_bytes_per_s",
        "net_rx_bytes_per_s": "host_net_rx_bytes_per_s", "net_tx_bytes_per_s": "host_net_tx_bytes_per_s",
        "proc_count": "host_proc_count", "uptime_seconds": "host_uptime_seconds"}
    assert set(mm.METRICS) == {"cpu_percent", "load1", "load5", "load15", "mem_used_percent", "mem_available_bytes",
                               "swap_used_percent", "disk_used_percent", "disk_inodes_used_percent",
                               "disk_read_bytes_per_s", "disk_write_bytes_per_s", "net_rx_bytes_per_s",
                               "net_tx_bytes_per_s", "proc_count", "uptime_seconds"}     # the contract's exact list


def test_disk_names_agree_with_the_cwagent_collector_helpers():
    mm = _mm()
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.threshold_defaults", DEFAULT_THRESHOLDS={})
    dm = load_module("app/collector/disk_mounts.py")
    for path in ["/", "C:", "/data", "/var/lib/mysql", "D:", "/mnt/a b", "/x" * 60]:
        expected = dm.metric_name_for_mount(path)
        if len(path) < 40:
            assert mm.stored_name("disk_used_percent", path) == expected
    assert mm.ROOT_PATHS == dm.ROOT_PATHS and mm.BASE_DISK_METRIC == dm.BASE_METRIC_NAME
    for p in ["/proc", "/run/user/1", "/snap/x/1", "/dev/shm", "/boot/efi", "/data", "/"]:
        assert mm.is_real_mount(p) == dm._is_real_mount(p, {}) or p in dm.ROOT_PATHS


def test_stored_names_fit_the_metric_name_column():
    mm = _mm()
    assert len(mm.stored_name("net_rx_bytes_per_s", "e" * 255)) <= 100
    assert len(mm.stored_name("disk_inodes_used_percent", "/" + "d" * 250)) <= 100


# ── HOST_AGENT_PREFER_OVER_CWAGENT ───────────────────────────────────────

def _prefer(monkeypatch=None):
    install_real_utils_package()
    pkg = install_stub("app.hostagents")
    pkg.__path__ = [os.path.join(REPO_ROOT, "app", "hostagents")]
    for n in [n for n in sys.modules if n.startswith("app.hostagents.")]:
        del sys.modules[n]
    return importlib.import_module("app.hostagents.prefer")


def test_prefer_flag_off_returns_input_and_never_reads_the_cache(monkeypatch):
    p = _prefer()
    monkeypatch.setattr(p.shared_cache, "peek", lambda k: (_ for _ in ()).throw(AssertionError("cache read")))
    rs = [{"id": 1}, {"id": 2}]
    kept, skipped = p.drop_resources_with_fed_metric(rs)
    assert kept is rs and skipped == 0 and p.covered_disk_names(1) == frozenset()
    assert p.mark_feed(1, ["mem_used_percent"]) is False


def test_prefer_needs_both_flags(monkeypatch):
    p = _prefer()
    monkeypatch.setenv("HOST_AGENT_PREFER_OVER_CWAGENT", "true")
    assert p.settings.prefer_over_cwagent() is False                 # master flag still off
    monkeypatch.setenv("HOST_AGENT_ENABLED", "true")
    assert p.settings.prefer_over_cwagent() is True


def test_prefer_marker_lifecycle_and_expiry(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_ENABLED", "true")
    monkeypatch.setenv("HOST_AGENT_PREFER_OVER_CWAGENT", "true")
    p = _prefer()
    clock = [1000.0]
    monkeypatch.setattr(p.shared_cache, "_now", lambda: clock[0])
    p.shared_cache.reset_for_tests()
    assert p.mark_feed(7, ["host_cpu_percent"]) is False             # nothing CWAgent-comparable: no marker
    assert p.mark_feed(7, ["host_cpu_percent", "mem_used_percent", "disk_used_percent", "disk_used_percent__data"])
    assert p.fed_names(7) == {"mem_used_percent", "disk_used_percent", "disk_used_percent__data"}
    kept, skipped = p.drop_resources_with_fed_metric([{"id": 7}, {"id": 8}])
    assert [r["id"] for r in kept] == [8] and skipped == 1
    assert p.covered_disk_names(7) == {"disk_used_percent", "disk_used_percent__data"}
    clock[0] += 181                                                  # past HOST_AGENT_STALE_SECONDS: CWAgent resumes
    assert p.fed_names(7) == frozenset()
    assert p.drop_resources_with_fed_metric([{"id": 7}])[0] == [{"id": 7}]


def test_ingest_marks_feed_for_samples_but_not_for_heartbeats(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_PREFER_OVER_CWAGENT", "true")
    e = _env()
    from app.hostagents import prefer
    prefer.shared_cache.reset_for_tests()
    _post(e, _payload())                                              # heartbeat
    assert prefer.fed_names(900) == frozenset()
    _post(e, _payload([_m("mem_used_percent", 40)], seq=11))
    assert prefer.fed_names(900) == {"mem_used_percent"}


def test_ingest_without_prefer_flag_writes_no_marker():
    e = _env()
    from app.hostagents import prefer
    prefer.shared_cache.reset_for_tests()
    _post(e, _payload([_m("mem_used_percent", 40)]))
    assert prefer.fed_names(900) == frozenset()


def test_estimated_saving_math():
    p = _prefer()
    # 20 instances, mem 288/day + one root mount 96/day each = 7,680 metric requests/day = $0.0768/day
    assert round(p.estimated_saving_usd(20 * 288, 20 * 96) * 1, 4) == 0.0768


# ── collector hooks (runner.py) ──────────────────────────────────────────

def _runner():
    install_real_utils_package()
    install_polling_modules()
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.aws.sts", get_boto3_session=lambda *a, **k: None)
    install_stub("app.aws.boto_config", STANDARD_RETRY={})
    install_stub("app.collector.metrics_writer", write_metrics_batch=lambda rows: None,
                 write_metric_history_batch=lambda rows, **kw: None)
    install_stub("app.collector.disk_mounts", all_cwagent_disk_dims=lambda cw, iid, **k: [],
                 ensure_disk_mount_metric_registered=lambda *a, **k: None)
    pm = load_module("app/collector/polling_model.py")
    ep = load_module("app/collector/env_polling.py")
    for name, m in (("polling_model", pm), ("env_polling", ep)):
        sys.modules[f"app.collector.{name}"] = m
        setattr(sys.modules["app.collector"], name, m)
    pkg = install_stub("app.hostagents")
    pkg.__path__ = [os.path.join(REPO_ROOT, "app", "hostagents")]
    for n in [n for n in sys.modules if n.startswith("app.hostagents.")]:
        del sys.modules[n]
    return load_module("app/collector/metrics/runner.py")


def test_runner_mem_unchanged_with_flag_off(monkeypatch):
    runner = _runner()
    seen = []
    monkeypatch.setattr(runner, "_ec2_instances_with_cwagent_mem_dims", lambda cw, rs: seen.append(list(rs)) or {})
    rs = [{"id": 1, "resource_id": "i-1"}, {"id": 2, "resource_id": "i-2"}]
    runner._collect_ec2_cwagent_mem(object(), rs)
    assert seen == [rs]


def test_runner_mem_skips_fed_instances_and_whole_call_when_all_fed(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_ENABLED", "true")
    monkeypatch.setenv("HOST_AGENT_PREFER_OVER_CWAGENT", "true")
    runner = _runner()
    from app.hostagents import prefer
    prefer.shared_cache.reset_for_tests()
    seen = []
    monkeypatch.setattr(runner, "_ec2_instances_with_cwagent_mem_dims", lambda cw, rs: seen.append([r["id"] for r in rs]) or {})
    rs = [{"id": 1, "resource_id": "i-1"}, {"id": 2, "resource_id": "i-2"}]
    prefer.mark_feed(1, ["mem_used_percent"])
    runner._collect_ec2_cwagent_mem(object(), rs)
    assert seen == [[2]]                                              # i-1 is fed by the agent: no paid ListMetrics/GMD
    prefer.mark_feed(2, ["mem_used_percent"])
    runner._collect_ec2_cwagent_mem(object(), rs)
    assert seen == [[2]]                                              # everything fed: no CWAgent work at all


def test_runner_disk_skips_only_the_mounts_the_agent_supplies(monkeypatch):
    runner = _runner()
    mounts = [([{"Name": "path", "Value": "/"}], "/", "disk_used_percent", "disk_used_percent", False),
              ([{"Name": "path", "Value": "/data"}], "/data", "disk_used_percent__data", "disk_used_percent", False)]
    monkeypatch.setattr(runner, "_cached_disk_dims", lambda cw, r: mounts)
    got = []
    monkeypatch.setattr(runner, "_execute_gmd", lambda cw, q, idm, minutes=5, align_period=None: got.append(sorted(v[1] for v in idm.values())) or 0)
    rs = [{"id": 1, "resource_id": "i-1"}]
    runner._collect_ec2_cwagent_disk(object(), rs, 3)
    assert got == [["disk_used_percent", "disk_used_percent__data"]]    # flag off: unchanged
    monkeypatch.setenv("HOST_AGENT_ENABLED", "true")
    monkeypatch.setenv("HOST_AGENT_PREFER_OVER_CWAGENT", "true")
    from app.hostagents import prefer
    prefer.shared_cache.reset_for_tests()
    prefer.mark_feed(1, ["disk_used_percent"])                          # agent reports only "/"
    got.clear()
    runner._collect_ec2_cwagent_disk(object(), rs, 3)
    assert got == [["disk_used_percent__data"]]                         # /data still comes from CWAgent


def test_runner_preference_errors_fall_back_to_the_paid_fetch(monkeypatch):
    runner = _runner()
    monkeypatch.setenv("HOST_AGENT_ENABLED", "true")
    monkeypatch.setenv("HOST_AGENT_PREFER_OVER_CWAGENT", "true")
    from app.hostagents import prefer
    monkeypatch.setattr(prefer, "drop_resources_with_fed_metric", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    rs = [{"id": 1, "resource_id": "i-1"}]
    assert runner._host_agent_skip_mem(rs) is rs


# ── service inventory (migration 131) ────────────────────────────────────

def _inv_item(name="nginx", state="running", startup="enabled", description="web"):
    return {"name": name, "state": state, "startup": startup, "description": description}


def test_inventory_is_written_in_the_same_transaction_and_replaces_older_scans():
    e = _env()
    body = _payload(metrics=[_m("cpu_percent", 10)])
    body["service_inventory"] = [_inv_item("nginx"), _inv_item("cron", "stopped", "disabled")]
    r = _post(e, body)
    assert r.status_code == 200 and r.json()["accepted"] == 1 and r.json()["rejected"] == 0
    assert e.db.connections == 1 and e.db.commits == 1
    sql, rows = [x for x in e.db.many if x[0] == e.ingest.SQL_UPSERT_INVENTORY][0]
    assert [r_[1] for r_ in rows] == ["cron", "nginx"]                         # lock order = name order
    deletes = [x for x in e.db.executed if x[0].startswith("DELETE FROM host_agent_service_inventory")]
    assert len(deletes) == 1 and "scanned_at < %s" in deletes[0][0]            # complete snapshot: older rows go


def test_heartbeat_with_an_inventory_stores_it_too():
    e = _env()
    body = _payload()
    body["service_inventory"] = [_inv_item()]
    assert _post(e, body).status_code == 200
    assert any(s == e.ingest.SQL_UPSERT_INVENTORY for s, _ in e.db.many)


def test_report_without_inventory_touches_no_inventory_table():
    e = _env()
    assert _post(e, _payload(metrics=[_m("cpu_percent", 10)])).status_code == 200
    assert not any("host_agent_service_inventory" in s for s, _ in e.db.many)
    assert not any("host_agent_service_inventory" in s for s, _ in e.db.executed)


def test_an_empty_inventory_means_no_news_and_deletes_nothing():
    e = _env()
    body = _payload(metrics=[_m("cpu_percent", 10)])
    body["service_inventory"] = []
    assert _post(e, body).status_code == 200
    assert not any("host_agent_service_inventory" in s for s, _ in e.db.executed)


def test_a_duplicate_seq_writes_no_inventory():
    e = _env()
    e.db.duplicate = True
    body = _payload(metrics=[_m("cpu_percent", 10)])
    body["service_inventory"] = [_inv_item()]
    assert _post(e, body).status_code == 200
    assert not any(s == e.ingest.SQL_UPSERT_INVENTORY for s, _ in e.db.many)


def test_inventory_upsert_sql_is_newest_scan_wins():
    sql = _env().ingest.SQL_UPSERT_INVENTORY
    for col in ("state", "startup", "description"):
        assert f"{col} = IF(VALUES(scanned_at) >= scanned_at, VALUES({col}), {col})" in sql
    assert "scanned_at = GREATEST(scanned_at, VALUES(scanned_at))" in sql
    # scanned_at is assigned LAST (assignments run left to right), so the comparisons above read the OLD value
    assert sql.index("scanned_at = GREATEST") > sql.index("description = IF")


def test_catalog_registration_runs_after_commit_with_the_resource_type_and_series_names():
    e = _env()
    e.db.ec2["i-abc"] = 55
    body = _payload(metrics=[_m("cpu_percent", 10), _m("net_rx_bytes_per_s", 5, {"iface": "ens5"})], instance_id="i-abc")
    assert _post(e, body).status_code == 200
    (acct, rtype, names), = e.catalog_calls
    assert rtype == "ec2" and "host_net_rx_bytes_per_s__ens5" in names and "host_cpu_percent" in names


def test_a_catalog_failure_never_fails_the_ingest(monkeypatch):
    e = _env()
    def boom(*a, **k):
        raise RuntimeError("catalog exploded")
    sys.modules["app.hostagents.catalog"].ensure_host_metrics_registered = boom
    r = _post(e, _payload(metrics=[_m("cpu_percent", 10)]))
    assert r.status_code == 200 and r.json()["accepted"] == 1
