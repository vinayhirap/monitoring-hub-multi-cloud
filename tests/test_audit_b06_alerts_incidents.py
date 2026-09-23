# tests/test_audit_b06_alerts_incidents.py
"""
Regression coverage for audit b06 (alerts / incidents / op-events /
audit-logs / maintenance windows). Same stub-and-load technique as the
rest of this suite (see tests/conftest.py).
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app          # noqa: F401
import app.auth     # noqa: F401

import pytest
from fastapi import HTTPException

from tests.conftest import load_module, install_stub


class RecCursor:
    """Records every query; answers via a list of (predicate, rows,
    rowcount) handlers. Tracks close() so leaks are detectable."""
    def __init__(self, handlers, log):
        self.handlers, self.log = handlers, log
        self._rows, self.rowcount, self.closed, self.lastrowid = [], 0, False, 1

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        self.log.append((norm, tuple(params or ())))
        for pred, rows, rc in self.handlers:
            if pred(norm):
                self._rows, self.rowcount = list(rows), rc
                return
        raise AssertionError(f"unexpected query: {norm!r}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        self.closed = True


class RecConn:
    def __init__(self, handlers, log, conns):
        self.handlers, self.log, self.closed = handlers, log, False
        conns.append(self)

    def cursor(self, dictionary=False):
        return RecCursor(self.handlers, self.log)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _db(handlers):
    log, conns = [], []
    install_stub("app.db", get_connection=lambda: RecConn(handlers, log, conns))
    return log, conns


def _alerts_mod(accessible, handlers, audit=None):
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: accessible)
    install_stub("app.auth.deps", get_current_user=lambda: None, require_role=lambda *a: (lambda: None))
    install_stub("app.aws.federation", NoConsoleCredentialsError=Exception)
    install_stub("app.ws.publisher", publish_alert_resolved=lambda *a, **k: None)
    install_stub("app.api.live_data", invalidate_accounts_cache=lambda: None)
    install_stub("app.audit", write_audit=(audit if audit is not None else (lambda *a, **k: None)))
    log, conns = _db(handlers)
    return load_module("app/api/alerts.py"), log, conns


USER = {"id": 1, "username": "op", "role": "editor"}
ACCT = (lambda n: n.startswith("SELECT aws_account_id AS account_id"), [{"account_id": 7}], 1)


# ── alerts (only what upstream fc0560f did not already cover) ────────
def test_clear_alerts_scoped_to_caller_accounts_and_audited():
    audit = []
    mod, log, conns = _alerts_mod({7, 9}, [
        (lambda n: n.startswith("UPDATE alerts a SET a.status = 'resolved'"), [], 3),
    ], audit=lambda *a, **k: audit.append((a, k)))
    out = mod.clear_alerts(account_id=None, current_user=USER)
    assert out == {"status": "cleared", "count": 3}
    q, params = log[0]
    assert "a.aws_account_id IN (%s, %s)" in q
    assert params == ("op", 7, 9)
    assert len(audit) == 1 and "count=3" in audit[0][0][2]
    assert all(c.closed for c in conns)


def test_clear_alerts_single_account_and_foreign_account_forbidden():
    mod, log, _ = _alerts_mod({7}, [
        (lambda n: n.startswith("UPDATE alerts a SET a.status = 'resolved'"), [], 1),
    ])
    mod.clear_alerts(account_id=7, current_user=USER)
    assert log[0][1] == ("op", 7)
    with pytest.raises(HTTPException) as e:
        mod.clear_alerts(account_id=8, current_user=USER)
    assert e.value.status_code == 403
    assert len(log) == 1


def test_clear_alerts_no_accounts_touches_nothing():
    mod, log, _ = _alerts_mod(set(), [])
    assert mod.clear_alerts(account_id=None, current_user=USER)["count"] == 0
    assert log == []


def test_clear_alerts_admin_unscoped():
    mod, log, _ = _alerts_mod(None, [
        (lambda n: n.startswith("UPDATE alerts a SET a.status = 'resolved'"), [], 5),
    ])
    assert mod.clear_alerts(account_id=None, current_user=USER)["count"] == 5
    assert "IN (" not in log[0][0]


def test_alert_account_lookup_releases_connection_on_error():
    class _Boom(RecCursor):
        def execute(self, sql, params=None):
            raise RuntimeError("db down")
    mod, _, _ = _alerts_mod({7}, [])
    closed = []

    class _C:
        def cursor(self, dictionary=False):
            return _Boom([], [])
        def close(self):
            closed.append(True)
    install_stub("app.db", get_connection=lambda: _C())
    mod.get_connection = lambda: _C()
    with pytest.raises(RuntimeError):
        mod._get_alert_account_id(1)
    assert closed == [True]


# ── incidents ───────────────────────────────────────────────────────
def test_capacity_forecast_passes_account_scope():
    calls = []
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: {7})
    install_stub("app.collector.trend",
                 compute_capacity_forecasts=lambda **k: calls.append(k) or [])
    _db([])
    mod = load_module("app/api/incidents.py")
    mod.get_capacity_forecast(account_id=7, resource_id="i-1", current_user=USER)
    assert calls == [{"aws_resource_id": "i-1", "aws_account_ids": [7]}]


# ── op_log ──────────────────────────────────────────────────────────
def test_log_event_releases_connection_when_insert_fails():
    conns = []

    class _Cur:
        def execute(self, *a):
            raise RuntimeError("boom")

        def close(self):
            pass

    class _Conn:
        closed = False

        def cursor(self):
            return _Cur()

        def close(self):
            self.closed = True

    def _get():
        c = _Conn(); conns.append(c); return c

    install_stub("app.db", get_connection=_get)
    mod = load_module("app/collector/op_log.py")
    mod.log_event("x", "msg")          # must not raise
    assert conns and conns[0].closed


# ── maintenance API ─────────────────────────────────────────────────
def _maint_api(accessible=None):
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: accessible)
    log, _ = _db([
        (lambda n: n.startswith("SELECT 1 FROM resources"), [{"1": 1}], 1),
        (lambda n: n.startswith("INSERT INTO maintenance_windows"), [], 1),
    ])
    return load_module("app/api/maintenance.py"), log


def test_create_window_converts_offset_to_utc():
    mod, log = _maint_api()
    mod.create_window(payload={
        "aws_account_id": 7, "resource_id": "i-1", "reason": "patch",
        "starts_at": "2026-09-23T14:00:00+05:30", "ends_at": "2026-09-23T15:00:00.000Z",
    }, current_user=USER)
    ins = [p for q, p in log if q.startswith("INSERT INTO maintenance_windows")][0]
    assert str(ins[3]) == "2026-09-23 08:30:00"
    assert str(ins[4]) == "2026-09-23 15:00:00"


def test_create_window_rejects_inverted_range():
    mod, _ = _maint_api()
    with pytest.raises(HTTPException) as e:
        mod.create_window(payload={
            "aws_account_id": 7, "resource_id": "i-1", "reason": "x",
            "starts_at": "2026-09-23T15:00:00Z", "ends_at": "2026-09-23T14:00:00Z",
        }, current_user=USER)
    assert e.value.status_code == 400


# ── maintenance silencing ───────────────────────────────────────────
def test_dependency_walk_is_account_scoped_and_typed_from_table():
    log, _ = _db([
        (lambda n: n.startswith("SELECT id, aws_account_id, resource_id, reason"),
         [{"id": 1, "aws_account_id": 7, "resource_id": "alb-1", "reason": "x" * 500,
           "silence_downstream": 1}], 1),
        (lambda n: n.startswith("WITH RECURSIVE downstream"), [{"resource_id": "i-long-dependent"}], 1),
        (lambda n: n.startswith("UPDATE alerts SET silenced = 1"), [], 2),
        (lambda n: n.startswith("SELECT id, aws_account_id, resource_id FROM alerts"), [], 0),
    ])
    mod = load_module("app/collector/maintenance.py")
    mod.sync_maintenance_silencing()
    assert "UTC_TIMESTAMP()" in log[0][0] and "NOW()" not in log[0][0]
    cte, cte_params = [(q, p) for q, p in log if q.startswith("WITH RECURSIVE")][0]
    assert "SELECT %s, 0" not in cte
    assert "FROM resource_relationships rr0" in cte
    assert "rr.aws_account_id = %s" in cte
    assert cte_params == ("alb-1", 7, 7, 10)
    upd = [p for q, p in log if q.startswith("UPDATE alerts SET silenced = 1")][0]
    assert len(upd[0]) == 500                       # reason truncated to VARCHAR(500)
    assert upd[1] == 7 and set(upd[2:]) == {"alb-1", "i-long-dependent"}
