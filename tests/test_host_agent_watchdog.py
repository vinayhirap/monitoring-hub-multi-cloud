# tests/test_host_agent_watchdog.py
"""Phase D: host agent heartbeat watchdog (app/collector/host_agent_watchdog.py).

Uses a small STATEFUL fake database (an in-memory alerts / resources / watchdog-state store behind the cursor) rather than a
static FakeCursor script, because the behaviours under test only exist across cycles: no duplicate alert on cycle 2,
auto-resolve when beats return, escalation in place."""
import sys
import types

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401

from tests.conftest import load_module

ACCT = 7


class FakeDB:
    def __init__(self):
        self.hosts = []                 # dicts: id, hostname, instance_id, resource_id, age
        self.resources = []             # dicts: db_id, resource_id, resource_type, name, tags, instance_state
        self.metrics = {}               # resource db_id -> statuscheckfailed value
        self.alerts = []
        self.states = {}                # host_agent_id -> dict
        self.departed = []              # rows returned by the revoked/deleted lookup
        self.queries = []
        self._next_alert = 100
        self._next_res = 500

    def add_resource(self, rid, rtype="ec2", state="running", tags="{}"):
        r = {"db_id": self._next_res, "resource_id": rid, "resource_type": rtype, "name": rid, "tags": tags,
             "instance_state": state}
        self._next_res += 1
        self.resources.append(r)
        return r

    def open_alerts(self):
        return [a for a in self.alerts if a["status"] in ("active", "acknowledged")]


class Cur:
    rowcount = 0

    def __init__(self, db):
        self.db = db
        self._rows = []
        self.lastrowid = None

    def close(self):
        pass

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def _res(self, rid, rtype=None):
        for r in self.db.resources:
            if r["resource_id"] == rid and (rtype is None or r["resource_type"] == rtype):
                return r
        return None

    def execute(self, sql, params=None):
        db, p = self.db, params or ()
        q = " ".join(sql.split())
        db.queries.append(q)
        self._rows, self.rowcount = [], 0
        if "FROM host_agent_watchdog_state s LEFT JOIN host_agents" in q:
            self._rows = list(db.departed)
        elif q.startswith("DELETE FROM host_agent_watchdog_state"):
            db.states.pop(p[0], None)
        elif "FROM host_agents h JOIN aws_accounts" in q:
            self._rows = [{"id": h["id"], "aws_account_id": ACCT, "hostname": h["hostname"],
                           "instance_id": h.get("instance_id"), "resource_id": h.get("resource_id"),
                           "age_seconds": h["age"]} for h in db.hosts]
        elif "r.resource_type = 'host_agent'" in q:
            r = self._res(p[1], "host_agent")
            self._rows = [r] if r else []
        elif q.startswith("SELECT r.id AS db_id") and "r.resource_id = %s" in q:
            r = self._res(p[1])
            self._rows = [r] if r else []
        elif q.startswith("SELECT id AS db_id, instance_state FROM resources"):
            r = self._res(p[1], "ec2")
            self._rows = [r] if r else []
        elif "FROM metrics" in q:
            v = db.metrics.get(p[0])
            self._rows = [{"metric_value": v}] if v is not None else []
        elif q.startswith("INSERT INTO resources"):
            db.add_resource(p[1], "host_agent", None)
        elif q.startswith("SELECT id, severity, status, silenced"):
            self._rows = [dict(a, muted=0) for a in db.open_alerts()
                          if a["resource_id"] == p[1] and a["metric_name"] == "host_agent_heartbeat"][:1]
        elif q.startswith("INSERT INTO alerts"):
            db._next_alert += 1
            self.lastrowid = db._next_alert
            db.alerts.append({"id": db._next_alert, "aws_account_id": p[0], "resource_id": p[1], "metric_name": p[2],
                              "severity": p[3], "environment": p[4], "group_key": p[5], "status": "active",
                              "current_value": p[6], "threshold": p[7], "silenced": p[10], "silenced_reason": p[11]})
        elif q.startswith("UPDATE alerts SET status = 'resolved'"):
            for a in db.alerts:
                if a["id"] == p[1] and a["status"] in ("active", "acknowledged"):
                    a["status"], a["resolution_reason"] = "resolved", p[0]
                    self.rowcount = 1
        elif q.startswith("UPDATE alerts SET current_value"):
            a = next(x for x in db.alerts if x["id"] == p[-1])
            a["current_value"], a["threshold"] = p[0], p[1]
            if "severity = %s" in q:
                a["severity"] = p[2]
            if "status = 'active'" in q:
                a["status"] = "active"
            self.rowcount = 1
        elif q.startswith("INSERT INTO host_agent_watchdog_state"):
            db.states[p[0]] = {"state": p[2], "offline_reason": p[3], "alert_id": p[4], "detail": p[6]}
        elif q.startswith("SELECT account_name FROM aws_accounts"):
            self._rows = [{"account_name": "acme"}]
        else:
            raise AssertionError(f"unexpected query: {q!r} params={p!r}")


class Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self, dictionary=True):
        return Cur(self.db)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _stub(monkeypatch, name, **attrs):
    """Fresh fake module in sys.modules for this test only. (conftest.install_stub would MUTATE the real module if
    another test already imported it, leaking fakes into later test files.)"""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _load(db, silenced=None, monkeypatch=None, enabled=True, stale="180"):
    calls = {"publish": [], "resolved": [], "notify": []}
    _stub(monkeypatch, "app.db", get_connection=lambda: Conn(db))
    _stub(monkeypatch, "app.environment_classifier", flag_enabled=lambda: False, IGNORE="ignore", UNKNOWN="unknown",
          resolve_environment=lambda **kw: None)
    _stub(monkeypatch, "app.priority", enabled=lambda: False, compute_priority=lambda *a, **k: None)
    _stub(monkeypatch, "app.ws.publisher",
          publish_alert=lambda **kw: calls["publish"].append(kw),
          publish_alert_resolved=lambda **kw: calls["resolved"].append(kw))
    _stub(monkeypatch, "app.notifications.sender",
          notify_alert_event=lambda event, alert: calls["notify"].append((event, alert)))
    _stub(monkeypatch, "app.collector.maintenance", active_silenced_map=lambda cur: dict(silenced or {}))
    monkeypatch.setenv("HOST_AGENT_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("HOST_AGENT_STALE_SECONDS", stale)
    return load_module("app/collector/host_agent_watchdog.py"), calls


def _host(db, age, hid=1, instance_id=None, resource_id=None, hostname="web-1"):
    db.hosts = [h for h in db.hosts if h["id"] != hid]
    db.hosts.append({"id": hid, "hostname": hostname, "instance_id": instance_id, "resource_id": resource_id,
                     "age": age})


# ── pure state function: threshold edges ─────────────────────────────────

def test_classify_threshold_edges(monkeypatch):
    mod, _ = _load(FakeDB(), monkeypatch=monkeypatch)
    s = 180
    assert mod.classify(0, s) == "online"
    assert mod.classify(60, s) == "online"            # one missed 60 s beat: still online (flap damping)
    assert mod.classify(s, s) == "online"             # exactly at the limit: not exceeded
    assert mod.classify(s + 1, s) == "stale"
    assert mod.classify(s * 5, s) == "stale"          # exactly 5x: still stale
    assert mod.classify(s * 5 + 1, s) == "offline"
    assert mod.classify(-30, s) == "online"           # heartbeat stamped slightly in the future
    assert mod.classify(None, s) is None


def test_stale_seconds_env_parsing(monkeypatch):
    mod, _ = _load(FakeDB(), monkeypatch=monkeypatch, stale="90")
    assert mod.stale_seconds() == 90
    monkeypatch.setenv("HOST_AGENT_STALE_SECONDS", "junk")
    assert mod.stale_seconds() == 180
    monkeypatch.setenv("HOST_AGENT_STALE_SECONDS", "0")
    assert mod.stale_seconds() == 180


def test_inert_when_flag_off(monkeypatch):
    def boom():
        raise AssertionError("database must not be touched when HOST_AGENT_ENABLED is not true")
    db = FakeDB()
    mod, _ = _load(db, monkeypatch=monkeypatch, enabled=False)
    mod.get_connection = boom
    assert mod.run_watchdog() == {"skipped": "disabled"}
    monkeypatch.delenv("HOST_AGENT_ENABLED")
    assert mod.run_watchdog() == {"skipped": "disabled"}


# ── alerts through the pipeline ──────────────────────────────────────────

def test_single_missed_beat_raises_nothing(monkeypatch):
    db = FakeDB()
    ec2 = db.add_resource("i-0abc", "ec2", "running")
    _host(db, age=170, instance_id="i-0abc")
    mod, calls = _load(db, monkeypatch=monkeypatch)
    stats = mod.run_watchdog()
    assert db.alerts == [] and stats["opened"] == 0
    assert db.states[1]["state"] == "online"
    assert calls["publish"] == [] and calls["notify"] == []


def test_stale_is_warning_then_offline_escalates_same_row(monkeypatch):
    db = FakeDB()
    db.add_resource("i-0abc", "ec2", "running")
    _host(db, age=200, instance_id="i-0abc")
    mod, calls = _load(db, monkeypatch=monkeypatch)
    mod.run_watchdog()
    assert len(db.alerts) == 1
    a = db.alerts[0]
    assert (a["severity"], a["metric_name"], a["resource_id"], a["silenced"]) == ("WARNING", "host_agent_heartbeat", "i-0abc", 0)
    assert a["group_key"] == f"{ACCT}:ec2:host_agent_heartbeat"
    assert calls["notify"][0][0] == "opened" and calls["publish"][0]["severity"] == "WARNING"

    _host(db, age=181 * 5, instance_id="i-0abc")           # now beyond 5x
    mod.run_watchdog()
    assert len(db.alerts) == 1                              # still ONE alert
    assert db.alerts[0]["severity"] == "CRITICAL"
    assert calls["notify"][-1][0] == "escalated"
    assert db.states[1]["state"] == "offline"


def test_no_duplicate_alerts_across_cycles(monkeypatch):
    db = FakeDB()
    db.add_resource("i-0abc", "ec2", "running")
    _host(db, age=300, instance_id="i-0abc")
    mod, calls = _load(db, monkeypatch=monkeypatch)
    for age in (300, 360, 420, 480):
        _host(db, age=age, instance_id="i-0abc")
        mod.run_watchdog()
    assert len(db.alerts) == 1
    assert len(calls["notify"]) == 1                        # opened once, never re-sent
    assert db.alerts[0]["current_value"] == 480             # but kept fresh every cycle


def test_auto_resolve_when_heartbeats_return(monkeypatch):
    db = FakeDB()
    db.add_resource("i-0abc", "ec2", "running")
    _host(db, age=400, instance_id="i-0abc")
    mod, calls = _load(db, monkeypatch=monkeypatch)
    mod.run_watchdog()
    assert len(db.open_alerts()) == 1
    _host(db, age=5, instance_id="i-0abc")
    stats = mod.run_watchdog()
    assert db.open_alerts() == [] and stats["resolved"] == 1
    assert db.alerts[0]["resolution_reason"] == "recovered"
    assert calls["resolved"][0]["alert_id"] == db.alerts[0]["id"]
    assert db.states[1]["state"] == "online" and db.states[1]["alert_id"] is None
    mod.run_watchdog()                                      # and stays quiet afterwards
    assert len(db.alerts) == 1


# ── agent down vs host down ──────────────────────────────────────────────

def test_stopped_instance_is_quiet_offline_and_clears_open_alert(monkeypatch):
    db = FakeDB()
    ec2 = db.add_resource("i-0abc", "ec2", "stopped")
    _host(db, age=900, instance_id="i-0abc")
    mod, calls = _load(db, monkeypatch=monkeypatch)
    stats = mod.run_watchdog()
    assert db.alerts == [] and stats["quiet_offline"] == 1
    assert db.states[1]["state"] == "offline" and db.states[1]["offline_reason"] == "instance_stopped"
    assert calls["notify"] == [] and calls["publish"] == []

    # an alert opened while the instance was still running is closed once it is seen stopped
    ec2["instance_state"] = "running"
    mod.run_watchdog()
    assert len(db.open_alerts()) == 1
    ec2["instance_state"] = "terminated"
    mod.run_watchdog()
    assert db.open_alerts() == []
    assert db.alerts[0]["resolution_reason"] == "instance_stopped"


def test_running_instance_text_says_agent_not_host(monkeypatch):
    db = FakeDB()
    db.add_resource("i-0abc", "ec2", "running")
    _host(db, age=400, instance_id="i-0abc", hostname="web-1")
    mod, calls = _load(db, monkeypatch=monkeypatch)
    mod.run_watchdog()
    text = db.states[1]["detail"]
    assert "web-1" in text and "instance is running" in text and "not the instance" in text
    assert calls["notify"][0][1]["detail"] == text


def test_running_instance_with_failing_status_checks_says_host_impaired(monkeypatch):
    db = FakeDB()
    ec2 = db.add_resource("i-0abc", "ec2", "running")
    db.metrics[ec2["db_id"]] = 1.0
    _host(db, age=400, instance_id="i-0abc")
    mod, _ = _load(db, monkeypatch=monkeypatch)
    mod.run_watchdog()
    assert "status checks are failing" in db.states[1]["detail"]


def test_unknown_instance_text_admits_it_cannot_tell(monkeypatch):
    db = FakeDB()
    _host(db, age=400, instance_id=None)
    mod, _ = _load(db, monkeypatch=monkeypatch)
    mod.run_watchdog()
    assert "cannot be told apart" in db.states[1]["detail"]
    # a host with no resources row gets its own 'host_agent' resource so the alert is visible everywhere
    assert [r["resource_id"] for r in db.resources] == ["host-agent-1"]
    assert db.alerts[0]["resource_id"] == "host-agent-1"
    assert db.alerts[0]["group_key"] == f"{ACCT}:host_agent:host_agent_heartbeat"


def test_alert_hangs_on_existing_ec2_resource_for_correlation(monkeypatch):
    db = FakeDB()
    db.add_resource("i-0abc", "ec2", "running", tags='{"Environment": "Dev"}')
    _host(db, age=400, instance_id="i-0abc")
    mod, _ = _load(db, monkeypatch=monkeypatch)
    mod.run_watchdog()
    assert len(db.resources) == 1                           # no extra resources row created
    assert db.alerts[0]["resource_id"] == "i-0abc"
    assert db.alerts[0]["environment"] == "dev"             # environment comes from the resource, like the evaluator


# ── maintenance / silences ───────────────────────────────────────────────

def test_maintenance_window_births_alert_silenced_and_mutes_notifications(monkeypatch):
    db = FakeDB()
    db.add_resource("i-0abc", "ec2", "running")
    _host(db, age=400, instance_id="i-0abc")
    mod, calls = _load(db, silenced={(ACCT, "i-0abc"): "Maintenance window: patching"}, monkeypatch=monkeypatch)
    stats = mod.run_watchdog()
    assert len(db.alerts) == 1 and db.alerts[0]["silenced"] == 1
    assert db.alerts[0]["silenced_reason"] == "Maintenance window: patching"
    assert stats["born_silenced"] == 1
    assert calls["notify"] == [] and calls["publish"] == []


def test_silenced_alert_does_not_notify_on_escalation(monkeypatch):
    db = FakeDB()
    db.add_resource("i-0abc", "ec2", "running")
    _host(db, age=200, instance_id="i-0abc")
    mod, calls = _load(db, monkeypatch=monkeypatch)
    mod.run_watchdog()
    n = len(calls["notify"])
    db.alerts[0]["silenced"] = 1                            # sync_maintenance_silencing() flipped it
    _host(db, age=2000, instance_id="i-0abc")
    mod.run_watchdog()
    assert db.alerts[0]["severity"] == "CRITICAL"
    assert len(calls["notify"]) == n


def test_other_accounts_window_does_not_silence(monkeypatch):
    db = FakeDB()
    db.add_resource("i-0abc", "ec2", "running")
    _host(db, age=400, instance_id="i-0abc")
    mod, calls = _load(db, silenced={(ACCT + 1, "i-0abc"): "other tenant"}, monkeypatch=monkeypatch)
    mod.run_watchdog()
    assert db.alerts[0]["silenced"] == 0 and len(calls["notify"]) == 1


# ── lifecycle edges ──────────────────────────────────────────────────────

def test_revoked_or_deleted_host_closes_its_alert_and_state(monkeypatch):
    db = FakeDB()
    db.add_resource("i-0abc", "ec2", "running")
    _host(db, age=400, instance_id="i-0abc")
    mod, calls = _load(db, monkeypatch=monkeypatch)
    mod.run_watchdog()
    alert_id = db.alerts[0]["id"]
    db.hosts = []
    db.departed = [{"host_agent_id": 1, "aws_account_id": ACCT, "alert_id": alert_id}]
    stats = mod.run_watchdog()
    assert db.open_alerts() == [] and db.alerts[0]["resolution_reason"] == "agent_revoked"
    assert 1 not in db.states and stats["departed"] == 1


def test_twin_hosts_on_one_instance_freshest_wins(monkeypatch):
    db = FakeDB()
    db.add_resource("i-0abc", "ec2", "running")
    _host(db, age=5000, hid=1, instance_id="i-0abc", hostname="old-name")
    _host(db, age=10, hid=2, instance_id="i-0abc", hostname="new-name")
    db.hosts.sort(key=lambda h: h["id"])
    mod, _ = _load(db, monkeypatch=monkeypatch)
    mod.run_watchdog()
    assert db.alerts == []                                  # the re-enrolled agent is healthy: no alert for the dead twin
    assert db.states[2]["state"] == "online"
    assert db.states[1]["offline_reason"] == "superseded"


def test_watchdog_failure_rolls_back_and_raises(monkeypatch):
    db = FakeDB()
    _host(db, age=400)
    mod, _ = _load(db, monkeypatch=monkeypatch)
    rolled = []

    class BadConn(Conn):
        def cursor(self, dictionary=True):
            raise RuntimeError("db down")

        def rollback(self):
            rolled.append(1)
    mod.get_connection = lambda: BadConn(db)
    try:
        mod.run_watchdog()
        assert False, "should raise"
    except RuntimeError:
        pass


# ── shared-file edits ────────────────────────────────────────────────────

def test_heartbeat_metric_is_a_system_metric_so_threshold_sweep_leaves_it():
    import re
    src = open("app/alert_rules.py").read()
    assert re.search(r'SYSTEM_METRICS = \([^)]*"host_agent_heartbeat"', src)


def test_notification_message_carries_detail_line(monkeypatch):
    _stub(monkeypatch, "app.db", get_connection=lambda: None)
    sender = load_module("app/notifications/sender.py")
    msg = sender.build_message("opened", {"id": 1, "severity": "WARNING", "metric_name": "host_agent_heartbeat",
                                          "resource_id": "i-0abc", "detail": "agent silent for 5m"})
    assert "agent silent for 5m" in msg["text"]
    plain = sender.build_message("opened", {"id": 1, "severity": "WARNING", "metric_name": "x", "resource_id": "r"})
    assert "agent silent" not in plain["text"]


def test_scheduler_wires_watchdog_in_critical_tier_after_maintenance():
    src = open("app/collector/scheduler.py").read()
    i_maint = src.index("sync_maintenance_silencing()")
    i_dog = src.index("run_watchdog()")
    assert i_dog > i_maint
    assert "from app.collector.host_agent_watchdog import run_watchdog" in src


def test_migration_121_is_idempotent_and_touches_no_contract_table():
    sql = open("db/migrations/121_host_agent_watchdog_state.sql").read()
    body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    assert "CREATE TABLE IF NOT EXISTS host_agent_watchdog_state" in body
    for contract in ("host_agents", "host_agent_tokens", "host_agent_enrollment_keys"):
        assert f"ALTER TABLE {contract}" not in body and f"INSERT INTO {contract}" not in body
    assert "FOREIGN KEY" not in body.upper()
