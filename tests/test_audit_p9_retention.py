# tests/test_audit_p9_retention.py
"""Audit D3/D9: bounded growth for resolved alerts and the notification log, without ever touching live alerts."""
import sys
from pathlib import Path

import app          # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class _Cur:
    def __init__(self, plan, log):
        self.plan, self.log, self.rowcount = plan, log, 0
    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))
        self.rowcount = self.plan.pop(0) if self.plan else 0


class _Conn:
    def __init__(self, plan, log):
        self.plan, self.log, self.commits, self.closed = plan, log, 0, 0
    def cursor(self):
        return _Cur(self.plan, self.log)
    def commit(self):
        self.commits += 1
    def close(self):
        self.closed += 1


def _mod(plan=None, log=None):
    log = log if log is not None else []
    plan = plan if plan is not None else []
    conns = []
    def gc():
        c = _Conn(plan, log); conns.append(c); return c
    install_stub("app.db", get_connection=gc)
    return load_module("app/collector/retention.py"), log, conns


def test_only_resolved_alerts_are_deleted_and_never_active_or_acknowledged():
    m, log, _ = _mod([5])
    assert m.prune_resolved_alerts(sleep=lambda s: None) == 5
    sql, params = log[0]
    assert "status = 'resolved'" in sql and "resolved_at IS NOT NULL" in sql and "LIMIT 1000" in sql
    assert params == (400,)


def test_batches_until_a_short_batch_then_stops():
    m, log, conns = _mod([1000, 1000, 250])
    sleeps = []
    assert m.prune_resolved_alerts(sleep=sleeps.append) == 2250
    assert len(log) == 3 and len(sleeps) == 2                    # paused between full batches only
    assert all(c.commits == 1 and c.closed == 1 for c in conns)  # one short transaction per batch


def test_run_is_bounded_per_invocation():
    m, log, _ = _mod([1000] * 500)
    m.prune_resolved_alerts(sleep=lambda s: None)
    assert len(log) == m.MAX_BATCHES


def test_env_controls_and_floor(monkeypatch):
    m, log, _ = _mod([0])
    monkeypatch.setenv("ALERT_RETENTION_DAYS", "0")
    assert m.alert_retention_days() == 0 and m.prune_resolved_alerts() == 0 and log == []   # disabled: no query at all
    monkeypatch.setenv("ALERT_RETENTION_DAYS", "7")
    assert m.alert_retention_days() == 90                         # a typo cannot wipe recent history
    monkeypatch.setenv("ALERT_RETENTION_DAYS", "abc")
    assert m.alert_retention_days() == 400
    monkeypatch.delenv("ALERT_RETENTION_DAYS")
    assert m.alert_retention_days() == 400


def test_notification_log_prune_and_disable(monkeypatch):
    m, log, _ = _mod([3])
    assert m.prune_notification_log(sleep=lambda s: None) == 3
    assert "DELETE FROM notification_log" in log[0][0] and log[0][1] == (90,)
    monkeypatch.setenv("NOTIFICATION_LOG_RETENTION_DAYS", "0")
    assert m.prune_notification_log() == 0


def test_run_retention_never_raises_and_reports_failure():
    install_stub("app.db", get_connection=lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    m = load_module("app/collector/retention.py")
    assert m.run_retention() == {"alerts": -1, "notification_log": -1}


def test_wired_into_the_daily_block_and_audit_logs_are_not_pruned():
    sched = (ROOT / "app/collector/scheduler.py").read_text()
    assert "run_retention()" in sched and sched.index('if tier == "low":') < sched.index("run_retention()")
    src = (ROOT / "app/collector/retention.py").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"""')) and "DELETE FROM" in l)
    assert "audit_logs" not in code
    from app import threshold_defaults as td
    assert not td.is_placeholder_threshold(*td.DEFAULT_THRESHOLDS["ExecutionsTimedOut"])
