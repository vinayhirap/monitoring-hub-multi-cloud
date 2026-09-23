# tests/test_audit_b07_alert_evaluation.py
"""
Regression coverage for audit B07 (alert evaluation engine, thresholds,
auto-tuning):
  - evaluator: tag parsing never raises, renamed AWS metrics join,
    ALB/NLB threshold disambiguation, per-row isolation with publish
    only after commit, account-scoped stopped-instance sweep.
  - threshold_tuning: account-scoped lookups, collector metric names,
    ALB/NLB baseline filtering.
  - settings: upsert / dynamic validation, /check permission.
"""
import math
import sys
from contextlib import contextmanager

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app          # noqa: F401
import app.auth     # noqa: F401
import app.alert_rules  # noqa: F401 -- real module, imported by alert_evaluator
import app.threshold_defaults  # noqa: F401

import pytest
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn


# ── helpers ─────────────────────────────────────────────────────────

def _fake_get_db_cursor(get_connection):
    @contextmanager
    def get_db_cursor(dictionary=False, commit=True):
        conn = get_connection()
        cur = conn.cursor(dictionary=dictionary)
        try:
            yield conn, cur
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()
    return get_db_cursor


def _load_evaluator(conn_factory=None, publish_log=None):
    publish_log = publish_log if publish_log is not None else []
    install_stub("app.db", get_connection=conn_factory or (lambda: FakeConn([])))
    install_stub("app.ws.publisher",
                 publish_alert=lambda **kw: publish_log.append(("new", kw)),
                 publish_alert_resolved=lambda **kw: publish_log.append(("resolved", kw)))
    install_stub("app.api.live_data", invalidate_accounts_cache=lambda: None)
    install_stub("app.api.alerts", _invalidate_cache=lambda: None)
    return load_module("app/collector/alert_evaluator.py")


# ── evaluator: pure helpers ─────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (None, "prod"),
    ("null", "prod"),                       # json.dumps(None)
    ("[1, 2]", "prod"),                     # non-object JSON
    ("not json", "prod"),
    ('{"Environment": "UAT"}', "uat"),
    ('{"environment": null}', "prod"),
    ('{"environment": 5}', "5"),            # non-string value
    ('{"Environment": "development"}', "development"),
])
def test_environment_from_tags_never_raises(raw, expected):
    mod = _load_evaluator()
    assert mod._environment_from_tags(raw) == expected


def test_environment_truncated_to_column_width():
    mod = _load_evaluator()
    env = mod._environment_from_tags('{"environment": "' + "x" * 200 + '"}')
    assert len(env) == mod.ENVIRONMENT_MAX_LEN == 50


def test_threshold_disabled_sweep_uses_collector_metric_names():
    """The orphan sweep's NOT EXISTS must match renamed AWS metrics
    (errors5xx etc.) -- a plain mc.metric_name = a.metric_name never matched
    them, so every such alert looked 'threshold_disabled'."""
    conn = _ScriptedConn([])
    mod = _load_evaluator(lambda: conn)
    mod.evaluate_alerts()
    sweep = [n for n, _ in conn.executed if "AND NOT EXISTS" in n][0]
    assert "THEN 'errors5xx'" in sweep
    assert "mc.metric_name = a.metric_name" not in sweep


def _row(**kw):
    base = {"aws_account_id": 7, "aws_resource_id": "i-1", "metric_name": "cpuutilization",
            "resource_type": "ec2", "service": "ec2", "threshold_id": 10}
    base.update(kw)
    return base


def test_select_threshold_rows_picks_matching_lb_type():
    mod = _load_evaluator()
    alb = "arn:aws:elasticloadbalancing:ap-south-1:1:loadbalancer/app/web/abc"
    nlb = "arn:aws:elasticloadbalancing:ap-south-1:1:loadbalancer/net/tcp/def"
    rows = [
        _row(aws_resource_id=alb, resource_type="elb", service="alb", threshold_id=20,
             metric_name="healthyhosts_describe"),
        _row(aws_resource_id=alb, resource_type="elb", service="nlb", threshold_id=21,
             metric_name="healthyhosts_describe"),
        _row(aws_resource_id=nlb, resource_type="elb", service="alb", threshold_id=20,
             metric_name="healthyhosts_describe"),
        _row(aws_resource_id=nlb, resource_type="elb", service="nlb", threshold_id=21,
             metric_name="healthyhosts_describe"),
    ]
    kept = {(r["aws_resource_id"], r["threshold_id"]) for r in mod._select_threshold_rows(rows)}
    assert kept == {(alb, 20), (nlb, 21)}


def test_select_threshold_rows_dedupes_on_lowest_threshold_id():
    mod = _load_evaluator()
    kept = mod._select_threshold_rows([_row(threshold_id=12), _row(threshold_id=11)])
    assert [r["threshold_id"] for r in kept] == [11]


# ── evaluator: cycle behaviour ──────────────────────────────────────

class _ScriptedConn:
    """Records commits/rollbacks; cursor answers by SQL prefix."""

    def __init__(self, rows, fail_insert_for=None):
        self.rows = rows
        self.fail_insert_for = fail_insert_for
        self.commits = 0
        self.rollbacks = 0
        self.events = []
        self.executed = []
        conn = self

        class _Cur(FakeCursor):
            lastrowid = 555

            def execute(self, sql, params=None):
                n = " ".join(sql.split())
                conn.executed.append((n, params))
                if n.startswith("SELECT a.id FROM alerts a"):
                    self._pending = []
                elif n.startswith("DELETE FROM alert_pending WHERE last_seen_at"):
                    self._pending = []
                elif "FROM maintenance_windows" in n:
                    self._pending = []
                elif n.startswith("SELECT m.resource_id AS db_resource_id"):
                    self._pending = conn.rows
                elif n.startswith("SELECT id, severity, status FROM alerts"):
                    self._pending = []
                elif n.startswith("INSERT INTO alert_pending"):
                    if conn.fail_insert_for and params[1] == conn.fail_insert_for:
                        raise RuntimeError("1406 Data too long for column 'environment'")
                    self._pending = []
                elif n.startswith("SELECT breach_cycles"):
                    self._pending = [{"breach_cycles": 1, "severity": "CRITICAL",
                                      "first_breach_at": "2026-09-23 00:00:00"}]
                elif n.startswith("INSERT INTO alerts"):
                    conn.events.append(("insert", params[1]))
                    self._pending = []
                elif n.startswith("DELETE FROM alert_pending"):
                    self._pending = []
                else:
                    raise AssertionError(f"unexpected query: {n!r}")

        self._cur = _Cur([])

    def cursor(self, dictionary=True):
        return self._cur

    def commit(self):
        self.commits += 1
        self.events.append(("commit", None))

    def rollback(self):
        self.rollbacks += 1
        self.events.append(("rollback", None))

    def close(self):
        pass


def _metric_row(resource_id, value=99.0):
    return {"db_resource_id": 1, "aws_resource_id": resource_id, "resource_type": "ec2",
            "aws_account_id": 7, "tags": '{"Environment": "prod"}', "region": "ap-south-1",
            "account_name": "acct", "default_region": "ap-south-1",
            "metric_name": "cpuutilization", "metric_value": value,
            "metric_timestamp": None, "service": "ec2", "threshold_id": 1,
            "cadence": "core", "unit": "Percent",
            "warning_value": 70.0, "critical_value": 90.0, "comparison": ">",
            "evaluation_period": 5, "use_dynamic": 0, "dynamic_k": 3.0}


def test_one_failing_row_does_not_abort_cycle_and_publish_follows_commit():
    publishes = []
    conn = _ScriptedConn([_metric_row("i-bad"), _metric_row("i-good")], fail_insert_for="i-bad")
    mod = _load_evaluator(lambda: conn, publishes)
    # publish log is appended by the stub; record ordering against commits
    orig = mod.publish_alert

    def _pub(**kw):
        conn.events.append(("publish", kw["alert_id"]))
        orig(**kw)
    mod.publish_alert = _pub

    mod.evaluate_alerts()  # must not raise

    assert ("insert", "i-good") in conn.events
    assert ("insert", "i-bad") not in conn.events
    assert conn.rollbacks >= 1
    assert len(publishes) == 1
    ins = conn.events.index(("insert", "i-good"))
    pub = conn.events.index(("publish", 555))
    assert any(e == ("commit", None) for e in conn.events[ins:pub])


def test_promoted_critical_uses_critical_threshold():
    # pending says CRITICAL (earlier cycle) but this reading only crosses warning
    conn = _ScriptedConn([_metric_row("i-1", value=75.0)])
    mod = _load_evaluator(lambda: conn)
    mod.evaluate_alerts()
    insert = [p for n, p in conn.executed if n.startswith("INSERT INTO alerts")][0]
    assert insert[3] == "CRITICAL"
    assert insert[8] == 90.0   # threshold column


def test_stopped_instance_sweep_is_account_scoped():
    conn = _ScriptedConn([])
    mod = _load_evaluator(lambda: conn)
    mod.evaluate_alerts()
    stopped = [n for n, _ in conn.executed if "instance_state IN" in n][0]
    assert "r.aws_account_id = a.aws_account_id" in stopped


# ── threshold_tuning ────────────────────────────────────────────────

def _tuning_stub(threshold, baselines, chronic_for=None):
    calls = []

    class _Cur(FakeCursor):
        def execute(self, sql, params=None):
            n = " ".join(sql.split())
            calls.append((n, params))
            if n.startswith("SELECT t.id, t.aws_account_id"):
                self._pending = [threshold]
            elif n.startswith("SELECT b.resource_id, AVG"):
                self._pending = baselines
            elif n.startswith("SELECT COUNT(*) AS cnt FROM alerts"):
                self._pending = [{"cnt": 0}]
            elif n.startswith("SELECT id FROM alerts"):
                self._pending = [{"id": 1}] if params[1] == chronic_for else []
            elif n.startswith("UPDATE thresholds SET use_dynamic"):
                self._pending = []
            else:
                raise AssertionError(n)

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cur([])

    audits = []
    install_stub("app.db", get_connection=lambda: _Conn([]))
    install_stub("app.audit", write_audit=lambda **kw: audits.append(kw))
    install_stub("app.collector.op_log", log_event=lambda *a, **k: None)
    return load_module("app/collector/threshold_tuning.py"), calls, audits


def _th(**kw):
    row = {"id": 1, "aws_account_id": 7, "resource_type": "rds", "metric_id": 9,
           "warning_value": 100.0, "critical_value": 200.0, "comparison": ">",
           "dynamic_k": None, "metric_name": "DatabaseConnections", "service": "rds",
           "account_name": "acct"}
    row.update(kw)
    return row


def test_tuning_lookups_are_account_scoped_and_use_collector_names():
    mod, calls, audits = _tuning_stub(
        _th(), [{"resource_id": "db-1", "typical_value": 150.0,
                 "typical_stddev": 1.0, "total_samples": 40}], chronic_for="db-1")
    assert mod.auto_tune_static_thresholds() == 1
    baseline_q = [p for n, p in calls if n.startswith("SELECT b.resource_id")][0]
    assert baseline_q[2] == "dbconnections"
    for n, p in calls:
        if n.startswith("SELECT id FROM alerts") or n.startswith("SELECT COUNT(*) AS cnt FROM alerts"):
            assert "aws_account_id = %s" in n
            assert p[0] == 7
            assert p[2] == "dbconnections"
    assert len(audits) == 1


def test_nlb_threshold_ignores_alb_baselines():
    alb = "arn:aws:elasticloadbalancing:x:1:loadbalancer/app/web/abc"
    mod, calls, audits = _tuning_stub(
        _th(resource_type="elb", service="nlb", metric_name="HealthyHostCount",
            comparison="<", warning_value=1.0, critical_value=0.0),
        [{"resource_id": alb, "typical_value": 0.0, "typical_stddev": 0.0, "total_samples": 40},
         {"resource_id": alb + "2", "typical_value": 0.0, "typical_stddev": 0.0, "total_samples": 40}],
    )
    assert mod.auto_tune_static_thresholds() == 0
    assert audits == []


def test_tuning_row_failure_does_not_abort_others():
    mod, calls, audits = _tuning_stub(_th(warning_value=None), [
        {"resource_id": "db-1", "typical_value": 150.0, "typical_stddev": 1.0, "total_samples": 40}])
    assert mod.auto_tune_static_thresholds() == 0   # TypeError on None swallowed per row
    assert audits == []


# ── settings.py ─────────────────────────────────────────────────────

def _load_settings(perms_seen=None):
    perms_seen = perms_seen if perms_seen is not None else []
    executed = []

    class _Cur(FakeCursor):
        lastrowid = 42

        def execute(self, sql, params=None):
            n = " ".join(sql.split())
            executed.append((n, params))
            if n.startswith("SELECT id FROM metric_catalog"):
                self._pending = [{"id": params[0]}] if params[0] != 404 else []
            elif n.startswith("SELECT aws_account_id FROM thresholds"):
                self._pending = [{"aws_account_id": 7}]
            else:
                self._pending = []

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cur([])

    factory = lambda: _Conn([])  # noqa: E731
    install_stub("app.db", get_connection=factory, get_db_cursor=_fake_get_db_cursor(factory))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)

    def _require_permission(code):
        perms_seen.append(code)
        return lambda: None
    install_stub("app.auth.permissions", require_permission=_require_permission)
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    return load_module("app/api/settings.py"), executed


_USER = {"username": "admin", "role": "admin"}


def _payload(**kw):
    p = {"account_id": 7, "metric_id": 1, "resource_type": "ec2", "warning_value": 70,
         "critical_value": 90, "comparison": ">", "evaluation_period": 5, "enabled": 1}
    p.update(kw)
    return p


@pytest.mark.parametrize("bad", [
    {"warning_value": None},
    {"warning_value": float("nan")},
    {"critical_value": math.inf},
    {"comparison": "=="},
    {"warning_value": 95, "critical_value": 90},                    # inverted for '>'
    {"comparison": "<", "warning_value": 5, "critical_value": 10},  # inverted for '<'
    {"evaluation_period": 0},
    {"enabled": 2},
    {"metric_id": "abc"},
    {"metric_id": 404},                                             # unknown catalog id
])
def test_upsert_rejects_invalid_payload_with_400(bad):
    from fastapi import HTTPException
    mod, _ = _load_settings()
    with pytest.raises(HTTPException) as e:
        mod.upsert_threshold(payload=_payload(**bad), current_user=_USER)
    assert e.value.status_code == 400


def test_upsert_valid_payload_saves_and_returns_id():
    mod, executed = _load_settings()
    out = mod.upsert_threshold(payload=_payload(resource_type="alb"), current_user=_USER)
    assert out == {"status": "saved", "id": 42}
    insert = [p for n, p in executed if n.startswith("INSERT INTO thresholds")][0]
    assert insert[1] == "elb"
    assert "LAST_INSERT_ID(id)" in [n for n, _ in executed if n.startswith("INSERT INTO thresholds")][0]


def test_dynamic_k_nan_rejected():
    from fastapi import HTTPException
    mod, _ = _load_settings()
    with pytest.raises(HTTPException) as e:
        mod.toggle_dynamic_threshold(threshold_id=1, payload={"use_dynamic": 1, "dynamic_k": float("nan")},
                                     current_user=_USER)
    assert e.value.status_code == 400


def test_check_endpoint_requires_configure_permission():
    perms = []
    _load_settings(perms)
    # route decorators run in declaration order; /check is the last route
    assert perms[-1] == "alerts.configure"
