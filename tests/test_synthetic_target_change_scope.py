# tests/test_synthetic_target_change_scope.py
"""After a check's target is edited, results from before the edit (the old host's certificate, TLS version, last error
and uptime) must not be shown against the new target. PATCH records target_changed_at (migration 087); the list and
results endpoints ignore older rows for those figures. Rows are kept, never deleted."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app          # noqa: F401
import app.auth     # noqa: F401

from tests.conftest import load_module, install_stub, FakeConn

_USER = {"id": 1, "username": "u"}
_ROOT = Path(__file__).resolve().parents[1]


def _api():
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    return load_module("app/api/synthetic.py")


def _conn(sqls, fetchall=(), fetchone=()):
    """records every statement; fetchall / fetchone answers are consumed in order"""
    all_q, one_q = list(fetchall), list(fetchone)

    class Cur:
        def execute(self, sql, params=None):
            sqls.append((" ".join(sql.split()), tuple(params or ())))

        def fetchall(self):
            return all_q.pop(0) if all_q else []

        def fetchone(self):
            return one_q.pop(0) if one_q else None

        def close(self):
            pass

    class Conn:
        def cursor(self, dictionary=False):
            return Cur()

        def commit(self):
            pass

        def close(self):
            pass

    return Conn()


def _patch(monkeypatch, payload, stored_target="http://8.8.8.8/"):
    mod = _api()
    sqls = []
    monkeypatch.setattr(mod, "_get_check_account_id", lambda cid: 4)
    monkeypatch.setattr(mod, "_get_check_row", lambda cid: {
        "check_type": "http", "target": stored_target, "expect_https_redirect": 0})
    monkeypatch.setattr(mod, "get_connection", lambda: _conn(sqls))
    assert mod.update_check(7, payload, current_user=_USER)["status"] == "updated"
    return [s for s in sqls if s[0].startswith("UPDATE synthetic_checks")][0][0]


def test_changing_the_target_records_when(monkeypatch):
    assert "target_changed_at = NOW()" in _patch(monkeypatch, {"target": "http://8.8.4.4/"})


def test_resubmitting_the_same_target_does_not(monkeypatch):
    sql = _patch(monkeypatch, {"target": "http://8.8.8.8/"})
    assert "target_changed_at" not in sql


@pytest.mark.parametrize("payload", [{"name": "x"}, {"timeout_seconds": 20}, {"interval_seconds": 120}])
def test_other_edits_do_not(monkeypatch, payload):
    assert "target_changed_at" not in _patch(monkeypatch, payload)


def test_list_scopes_uptime_last_probe_and_tls_to_the_current_target(monkeypatch):
    mod = _api()
    sqls = []
    monkeypatch.setattr(mod, "get_connection", lambda: _conn(sqls, fetchall=[[]]))
    mod.list_checks(current_user=_USER)
    sql = sqls[0][0]
    # uptime (r), last probe (r3) and last TLS (r2) are each limited to rows at/after the change
    for alias in ("r", "r3", "r2"):
        assert f"{alias}.checked_at >= c.target_changed_at" in sql


def test_results_are_clamped_and_report_the_change(monkeypatch):
    mod = _api()
    sqls = []
    stamp = "2026-10-13 06:33:00"
    monkeypatch.setattr(mod, "_get_check_account_id", lambda cid: 4)
    monkeypatch.setattr(mod, "get_connection",
                        lambda: _conn(sqls, fetchall=[[{"success": 0}, {"success": 1}]],
                                      fetchone=[{"n": 2, "ok": 1}, {"target_changed_at": stamp}]))
    out = mod.get_check_results(7, hours=24, limit=100, current_user=_USER)
    assert all("r.checked_at >= c.target_changed_at" in s[0] for s in sqls[:2])
    assert out["target_changed_at"] == stamp and out["uptime_pct"] == 50.0 and out["truncated"] is False
    assert [r["success"] for r in out["results"]] == [1, 0]          # newest-first query, returned oldest-first


def test_results_cap_rows_but_uptime_covers_the_whole_window(monkeypatch):
    mod = _api()
    monkeypatch.setattr(mod, "_get_check_account_id", lambda cid: 4)
    monkeypatch.setattr(mod, "get_connection", lambda: _conn(
        [], fetchall=[[{"success": 1}] * 3], fetchone=[{"n": 10, "ok": 9}, None]))
    out = mod.get_check_results(7, hours=24, limit=3, current_user=_USER)
    assert len(out["results"]) == 3 and out["total_results"] == 10 and out["truncated"] is True
    assert out["uptime_pct"] == 90.0


def test_results_report_nothing_when_the_change_is_outside_the_window(monkeypatch):
    mod = _api()
    monkeypatch.setattr(mod, "_get_check_account_id", lambda cid: 4)
    monkeypatch.setattr(mod, "get_connection", lambda: _conn([], fetchall=[[]], fetchone=[{"n": 0, "ok": 0}, None]))
    out = mod.get_check_results(7, hours=24, limit=100, current_user=_USER)
    assert out["target_changed_at"] is None and out["uptime_pct"] is None


def test_migration_is_idempotent_and_additive():
    sql = (_ROOT / "db/migrations/087_synthetic_target_changed_at.sql").read_text()
    assert "information_schema.columns" in sql and "ADD COLUMN target_changed_at DATETIME NULL" in sql
    code = "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--")).upper().replace("DEALLOCATE PREPARE", "")
    assert "DROP" not in code and "DELETE" not in code and "UPDATE" not in code
