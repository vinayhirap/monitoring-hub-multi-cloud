# tests/test_audit_b08.py
"""Audit b08: webhook hardening, escalation send/commit + recipient scope,
cross-account correlation/RCA, trend forecast edge cases."""
import sys
import types

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app          # noqa: F401
import app.auth     # noqa: F401

from tests.conftest import load_module, install_stub


class RecCursor:
    """Records every statement; answers from `answers` (list of
    (substring, rows)) -- first match wins, else []."""
    def __init__(self, log, answers=(), fail_on=None):
        self.log, self.answers, self.fail_on = log, answers, fail_on
        self._rows, self.rowcount, self.lastrowid = [], 1, 99

    def execute(self, sql, params=None):
        n = " ".join(sql.split())
        self.log.append((n, params))
        if self.fail_on and self.fail_on in n:
            raise RuntimeError("db down")
        self._rows = next((rows for sub, rows in self.answers if sub in n), [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class RecConn:
    def __init__(self, log, answers=(), fail_on=None, events=None):
        self.log, self.answers, self.fail_on, self.events = log, answers, fail_on, events

    def cursor(self, dictionary=False):
        return RecCursor(self.log, self.answers, self.fail_on)

    def commit(self):
        if self.events is not None:
            self.events.append("commit")

    def rollback(self):
        if self.events is not None:
            self.events.append("rollback")

    def close(self):
        pass


# ── webhook ──────────────────────────────────────────────────────────

def _webhooks(monkeypatch, token="s3cret-token", answers=(("FROM aws_accounts", [(1,)]),), fail_on=None):
    log = []
    install_stub("app.db", get_connection=lambda: RecConn(log, answers, fail_on))
    install_stub("app.auth.rate_limit", check_rate_limit=lambda *a, **k: None, _client_ip=lambda r: "1.2.3.4")
    if token is None:
        monkeypatch.delenv("DEPLOY_WEBHOOK_TOKEN", raising=False)
    else:
        monkeypatch.setenv("DEPLOY_WEBHOOK_TOKEN", token)
    return load_module("app/api/webhooks.py"), log


def _call(mod, payload, token="s3cret-token"):
    return mod.record_deployment(types.SimpleNamespace(client=None), payload=payload, x_webhook_token=token)


def test_webhook_unconfigured_is_503(monkeypatch):
    from fastapi import HTTPException
    mod, _ = _webhooks(monkeypatch, token=None)
    with pytest.raises(HTTPException) as e:
        _call(mod, {"aws_account_id": 1})
    assert e.value.status_code == 503


@pytest.mark.parametrize("bad", ["wrong", "tök€n", None])
def test_webhook_bad_token_is_401_not_500(monkeypatch, bad):
    from fastapi import HTTPException
    mod, _ = _webhooks(monkeypatch)
    with pytest.raises(HTTPException) as e:
        _call(mod, {"aws_account_id": 1}, token=bad)
    assert e.value.status_code == 401


@pytest.mark.parametrize("payload", [
    {"aws_account_id": "abc"}, {"aws_account_id": True}, {},
    {"aws_account_id": 1, "service": "x" * 201},
    {"aws_account_id": 1, "description": "d" * 2001},
    {"aws_account_id": 1, "resource_id": 123},
    {"aws_account_id": 1, "actor": {"a": 1}},
])
def test_webhook_payload_validation_is_400(monkeypatch, payload):
    from fastapi import HTTPException
    mod, _ = _webhooks(monkeypatch)
    with pytest.raises(HTTPException) as e:
        _call(mod, payload)
    assert e.value.status_code == 400


def test_webhook_records_and_returns_id(monkeypatch):
    mod, log = _webhooks(monkeypatch)
    out = _call(mod, {"aws_account_id": "1", "service": "api", "version": "v2", "resource_id": "i-1"})
    assert out == {"status": "recorded", "id": 99}
    ins = [p for s, p in log if s.startswith("INSERT INTO op_events")][0]
    assert ins[0] == 1 and ins[1] == "i-1" and ins[2] == "Deployment: api v2 by unknown"


def test_webhook_db_failure_is_not_reported_as_recorded(monkeypatch):
    from fastapi import HTTPException
    mod, _ = _webhooks(monkeypatch, fail_on="INSERT INTO op_events")
    with pytest.raises(HTTPException) as e:
        _call(mod, {"aws_account_id": 1})
    assert e.value.status_code == 503


# ── escalation collector ─────────────────────────────────────────────

def _escalation(rows, members, scope_by_user, notify_raises=False, sent=None):
    log, events = [], []
    answers = [("FROM alerts a JOIN resources r", rows), ("FROM user_group_memberships", members)]
    install_stub("app.db", get_connection=lambda: RecConn(log, answers, events=events))
    install_stub("app.alert_rules", firing_where=lambda *a, **k: "1=1", base_where=lambda *a, **k: "1=1")
    install_stub("app.auth.authorization",
                 get_accessible_account_ids=lambda u: scope_by_user[u["id"]])
    install_stub("app.collector.op_log", log_event=lambda *a, **k: events.append("op_event"))

    def send(to, subject, body):
        if notify_raises:
            raise RuntimeError("smtp down")
        events.append(f"email:{to}")
        (sent if sent is not None else []).append(to)
        return True
    install_stub("app.email.mailer", is_configured=lambda: True, get_public_app_url=lambda: "https://x",
                 send_email=send)
    return load_module("app/collector/escalation.py"), log, events


_ROW = {"alert_id": 7, "resource_id": "i-1", "metric_name": "cpu", "severity": "CRITICAL",
        "triggered_at": None, "aws_account_id": 5, "policy_id": 1, "ack_sla_minutes": 10,
        "escalate_to_group_id": 3, "group_name": "L2"}


def test_escalation_commits_before_emailing():
    mod, _, events = _escalation([_ROW], [{"id": 1, "role": "editor", "email": "a@x"}], {1: {5}})
    assert mod.evaluate_escalations() == 1
    assert events.index("commit") < events.index("email:a@x")


def test_escalation_only_emails_members_with_account_access():
    sent = []
    mod, _, _ = _escalation([_ROW], [
        {"id": 1, "role": "editor", "email": "in@x"},
        {"id": 2, "role": "editor", "email": "out@x"},
        {"id": 3, "role": "admin", "email": "admin@x"},
    ], {1: {5}, 2: {6}, 3: None}, sent=sent)
    mod.evaluate_escalations()
    assert sorted(sent) == ["admin@x", "in@x"]


def test_escalation_email_failure_does_not_undo_escalation():
    mod, log, events = _escalation([_ROW], [{"id": 1, "role": "editor", "email": "a@x"}], {1: {5}},
                                   notify_raises=True)
    assert mod.evaluate_escalations() == 1
    assert "rollback" not in events
    upd = [s for s, _ in log if s.startswith("UPDATE alerts")][0]
    assert "UTC_TIMESTAMP()" in upd


def test_escalation_recipient_query_excludes_inactive_users():
    mod, log, _ = _escalation([_ROW], [], {})
    mod.evaluate_escalations()
    q = [s for s, _ in log if "FROM user_group_memberships" in s][0]
    assert "COALESCE(u.active, 1) = 1" in q


# ── escalation API ───────────────────────────────────────────────────

def _esc_api(answers=()):
    log = []
    install_stub("app.db", get_connection=lambda: RecConn(log, answers))
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda u: {3})
    return load_module("app/api/escalation.py"), log


@pytest.mark.parametrize("payload", [{"ack_sla_minutes": 0}, {"ack_sla_minutes": -5},
                                     {"ack_sla_minutes": "abc"}, {"escalate_to_group_id": "x"}])
def test_policy_patch_validation(payload):
    from fastapi import HTTPException
    mod, _ = _esc_api()
    with pytest.raises(HTTPException) as e:
        mod.update_policy(1, payload=payload, current_user={"id": 1, "role": "editor"})
    assert e.value.status_code == 400


def test_policy_create_accepts_string_account_and_checks_group():
    from fastapi import HTTPException
    mod, _ = _esc_api(answers=[("FROM org_groups", [])])
    with pytest.raises(HTTPException) as e:
        mod.create_policy(payload={"severity": "CRITICAL", "ack_sla_minutes": 15,
                                   "escalate_to_group_id": 9, "aws_account_id": "3"},
                          current_user={"id": 1, "role": "editor"})
    assert e.value.status_code == 400 and "group" in e.value.detail


def test_policy_patch_same_value_is_not_404():
    mod, _ = _esc_api(answers=[("SELECT aws_account_id FROM escalation_policies", [{"aws_account_id": 3}])])
    assert mod.update_policy(1, payload={"enabled": True},
                             current_user={"id": 1, "role": "editor"}) == {"status": "updated"}


# ── correlation / RCA ────────────────────────────────────────────────

def test_correlation_is_pinned_to_the_alerts_account():
    log = []
    alert = {"id": 1, "resource_id": "web-1", "severity": "WARNING", "created_at": None, "aws_account_id": 5}
    answers = [("FROM alerts a WHERE a.status = 'active' AND a.aws_account_id IS NOT NULL", [alert]),
               ("SELECT a2.id AS other_alert_id", [{"other_alert_id": 2, "other_severity": "CRITICAL"}])]
    install_stub("app.db", get_connection=lambda: RecConn(log, answers))
    install_stub("app.collector.rca", rank_probable_cause=lambda i: None)
    mod = load_module("app/collector/correlate.py")
    mod.correlate_alerts_into_incidents()
    join_existing = [(s, p) for s, p in log if s.startswith("SELECT DISTINCT i.id")][0]
    assert "i.aws_account_id = %s" in join_existing[0] and "rel.aws_account_id = %s" in join_existing[0]
    partner = [(s, p) for s, p in log if s.startswith("SELECT a2.id AS other_alert_id")][0]
    assert "a2.aws_account_id = %s" in partner[0] and 5 in partner[1]
    ins = [p for s, p in log if s.startswith("INSERT INTO incidents")][0]
    assert ins[2] == "CRITICAL"   # worst of the two seeding alerts


def test_explain_alert_scopes_cloudtrail_and_deploys_to_account():
    log = []
    alert = {"id": 1, "aws_account_id": 5, "resource_id": "prod_db", "metric_name": "cpu",
             "severity": "CRITICAL", "triggered_at": "2026-09-24 10:00:00", "current_value": 1, "threshold": 1}
    answers = [("FROM alerts WHERE id = %s", [alert]), ("AS in_degree", [{"in_degree": 0}])]
    install_stub("app.db", get_connection=lambda: RecConn(log, answers))
    mod = load_module("app/collector/rca.py")
    mod.explain_alert(1)
    ce = [(s, p) for s, p in log if "FROM cloud_events ce" in s][0]
    assert "ce.aws_account_id = %s" in ce[0] and ce[1][0] == 5
    dep = [(s, p) for s, p in log if "event_type = 'deployment'" in s][0]
    assert dep[1][0] == 5
    assert not any("FROM resources r JOIN aws_accounts acc" in s for s, _ in log)
    audit = [p for s, p in log if "FROM audit_logs" in s][0]
    # One backslash in the bound VALUE (mysql-connector escapes it on the
    # wire); JSON_SEARCH's default escape char then makes '_' literal.
    assert audit[3] == "%prod\\_db%"
    trend = [(s, p) for s, p in log if "FROM metric_history h" in s][0]
    assert "r.aws_account_id = %s" in trend[0]


# ── trend ────────────────────────────────────────────────────────────

def _trend(rows):
    log = []
    install_stub("app.db", get_connection=lambda: RecConn(log, [("FROM metric_history h", rows)]))
    return load_module("app/collector/trend.py"), log


def _series(start, step, n=30, rid=1, metric_start=0):
    return [{"rid": rid, "aws_resource_id": "vol", "aws_account_id": 5,
             "ts": metric_start + i * 3600, "metric_value": start + step * i} for i in range(n)]


def test_trend_empty_scope_returns_nothing_without_querying():
    mod, log = _trend(_series(50, 1))
    assert mod.compute_capacity_forecasts(aws_account_ids=set()) == [] and log == []


def test_trend_recovering_disk_is_not_reported():
    # disk_used_percent above 100 and FALLING must not be "exhausting".
    mod, _ = _trend(_series(130, -1))
    out = mod.compute_capacity_forecasts(aws_account_ids=[5])
    assert not [f for f in out if f["metric_name"] in ("DiskSpaceUtilization", "disk_used_percent")]


def test_trend_free_space_rising_from_zero_is_not_reported():
    mod, _ = _trend(_series(0, 5))
    out = mod.compute_capacity_forecasts(aws_account_ids=[5])
    assert not [f for f in out if f["metric_name"] in ("FreeStorageSpace", "EBSFreeSpacePercent")]


def test_trend_filling_disk_is_reported_with_positive_eta():
    mod, _ = _trend(_series(50, 0.5))
    out = [f for f in mod.compute_capacity_forecasts(aws_account_ids=[5]) if f["metric_name"] == "disk_used_percent"]
    assert out and out[0]["days_to_exhaustion"] > 0 and out[0]["aws_account_id"] == 5


def test_linear_trend_edge_cases():
    mod, _ = _trend([])
    assert mod._linear_trend([1] * 30, list(range(30))) is None           # one timestamp
    assert mod._linear_trend(list(range(10)), list(range(10))) is None    # too few points
    assert mod._linear_trend(list(range(30)), [float("nan")] * 30) is None
    slope, _ = mod._linear_trend([i * 86400 for i in range(30)], [5.0] * 30)
    assert abs(slope) < 1e-9


def test_health_fan_out_is_account_scoped():
    log = []
    answers = [("GROUP BY r.resource_id", [{"resource_id": "web-1", "aws_account_id": 5,
                                            "critical_count": 1, "warning_count": 0}]),
               ("AS fan_out", [{"fan_out": 2}])]
    install_stub("app.db", get_connection=lambda: RecConn(log, answers))
    install_stub("app.alert_rules", firing_where=lambda *a, **k: "1=1", base_where=lambda *a, **k: "1=1")
    mod = load_module("app/collector/health_score.py")
    mod.recompute_health_scores()
    q = [(s, p) for s, p in log if "AS fan_out" in s][0]
    assert "aws_account_id = %s" in q[0] and q[1] == (5, "web-1")


# ── audit b08 follow-up (2026-09-24) ─────────────────────────────────

def test_escalation_emails_only_after_connection_released():
    order = []

    class Conn(RecConn):
        def close(self):
            order.append("close")
    log = []
    answers = [("FROM alerts a JOIN resources r", [_ROW])]
    install_stub("app.db", get_connection=lambda: Conn(log, answers))
    install_stub("app.alert_rules", firing_where=lambda *a, **k: "1=1", base_where=lambda *a, **k: "1=1")
    mod = load_module("app/collector/escalation.py")
    mod._notify_escalation = lambda *a, **k: order.append("notify")
    assert mod.evaluate_escalations() == 1
    assert order == ["close", "notify"]


def test_escalation_already_claimed_alert_is_not_notified():
    mod, log, events = _escalation([_ROW], [{"id": 1, "role": "editor", "email": "a@x"}], {1: {5}})

    class NoClaim(RecCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            self.rowcount = 0 if sql.strip().startswith("UPDATE alerts") else 1
    import sys
    sys.modules["app.db"].get_connection = lambda: type("C", (RecConn,), {
        "cursor": lambda self, dictionary=False: NoClaim(self.log, self.answers)})(log, [
            ("FROM alerts a JOIN resources r", [_ROW])])
    mod = load_module("app/collector/escalation.py")
    assert mod.evaluate_escalations() == 0
    assert not [e for e in events if e.startswith("email:")]


def test_correlation_ack_does_not_resolve_incident_and_skips_hidden_metrics():
    log = []
    install_stub("app.db", get_connection=lambda: RecConn(log, []))
    install_stub("app.alert_rules",
                 base_where=lambda a="a": f"{a}.metric_name NOT IN ('multivariate_anomaly')")
    install_stub("app.collector.rca", rank_probable_cause=lambda i: None)
    mod = load_module("app/collector/correlate.py")
    mod.correlate_alerts_into_incidents()
    loose = [s for s, _ in log if s.startswith("SELECT a.id, a.resource_id")][0]
    assert "NOT EXISTS" in loose and "multivariate_anomaly" in loose
    resolve = [s for s, _ in log if s.startswith("UPDATE incidents i SET status = 'resolved'")][0]
    assert "a.status IN ('active', 'acknowledged')" in resolve


def test_correlation_ranks_after_connection_released():
    order = []

    class Conn(RecConn):
        def close(self):
            order.append("close")
    alert = {"id": 1, "resource_id": "web-1", "severity": "WARNING", "created_at": None, "aws_account_id": 5}
    answers = [("FROM alerts a WHERE a.status = 'active' AND a.aws_account_id IS NOT NULL", [alert]),
               ("SELECT a2.id AS other_alert_id", [{"other_alert_id": 2, "other_severity": "WARNING"}])]
    install_stub("app.db", get_connection=lambda: Conn([], answers))
    install_stub("app.alert_rules",
                 base_where=lambda a="a": f"{a}.metric_name NOT IN ('multivariate_anomaly')")
    install_stub("app.collector.rca", rank_probable_cause=lambda i: order.append("rank"))
    mod = load_module("app/collector/correlate.py")
    assert mod.correlate_alerts_into_incidents() == (1, 2)
    assert order == ["close", "rank"]


def test_trend_context_duplicate_recent_timestamps_not_nan():
    install_stub("app.db", get_connection=lambda: RecConn([], []))
    mod = load_module("app/collector/rca.py")
    # 10 spread points then 4 samples all at the SAME latest timestamp.
    pts = [{"ts": i * 600, "metric_value": 10 + i} for i in range(10)] + \
          [{"ts": 9 * 600, "metric_value": 50 + i} for i in range(4)]

    class Cur(RecCursor):
        def fetchall(self):
            return pts
    out = mod._trend_context(Cur([]), "i-1", "cpu", "2026-09-24 10:00:00", aws_account_id=5)
    assert out["pattern"] in ("sudden_spike", "gradual_trend", "flat_then_breach", "insufficient_data")


def test_explain_related_prefers_active_incident():
    log = []
    alert = {"id": 1, "aws_account_id": 5, "resource_id": "db", "metric_name": "cpu",
             "severity": "CRITICAL", "triggered_at": "2026-09-24 10:00:00", "current_value": 1, "threshold": 1}
    answers = [("FROM alerts WHERE id = %s", [alert]), ("AS in_degree", [{"in_degree": 0}])]
    install_stub("app.db", get_connection=lambda: RecConn(log, answers))
    mod = load_module("app/collector/rca.py")
    mod.explain_alert(1)
    rel = [s for s, _ in log if "AS other_count" in s][0]
    assert "i.status = 'active'" in rel and "ORDER BY" in rel


def test_webhook_per_account_token_is_limited_to_its_account(monkeypatch):
    from fastapi import HTTPException
    mod, log = _webhooks(monkeypatch, token=None)
    monkeypatch.setenv("DEPLOY_WEBHOOK_TOKENS", "1:tok-one, 2:tok-two, bad, x:y")
    assert _call(mod, {"aws_account_id": 1}, token="tok-one")["status"] == "recorded"
    with pytest.raises(HTTPException) as e:
        _call(mod, {"aws_account_id": 2}, token="tok-one")
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        _call(mod, {"aws_account_id": 1}, token="nope")
    assert e.value.status_code == 401


def test_webhook_shared_token_still_writes_any_account(monkeypatch):
    mod, _ = _webhooks(monkeypatch)
    monkeypatch.setenv("DEPLOY_WEBHOOK_TOKENS", "2:tok-two")
    assert _call(mod, {"aws_account_id": 1})["status"] == "recorded"


def test_webhook_inactive_account_rejected(monkeypatch):
    from fastapi import HTTPException
    mod, log = _webhooks(monkeypatch, answers=())
    with pytest.raises(HTTPException) as e:
        _call(mod, {"aws_account_id": 1})
    assert e.value.status_code == 404
    q = [s for s, _ in log if "FROM aws_accounts" in s][0]
    assert "status = 'active'" in q


def test_deploy_risk_active_accounts_only():
    log = []
    install_stub("app.db", get_connection=lambda: RecConn(log, []))
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda u: None)
    install_stub("app.collector.rca", DEPLOY_LOOKBACK_MINUTES=45)
    mod = load_module("app/api/deploy_risk.py")
    assert mod.list_deploy_risk(days=7, current_user={"id": 1}) == []
    assert "acc.status = 'active'" in log[0][0]
