# tests/test_synthetic_probe_race_guard.py
"""run_due_checks: a probe whose check was edited/disabled while it ran is discarded -- no result row, no status
or streak write -- so the old target's result is never stored as the new target's."""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401

from tests.conftest import load_module, install_stub

_DUE = {"id": 7, "aws_account_id": 42, "name": "api", "check_type": "http", "target": "http://8.8.8.8/",
        "expected_status_code": 200, "expected_keyword": None, "expect_https_redirect": 0,
        "timeout_seconds": 10, "interval_seconds": 300, "consecutive_failure_threshold": 2,
        "environment": "prod", "consecutive_failures": 0, "current_status": "up"}


def _mod():
    install_stub("app.db", get_connection=lambda: None)
    return load_module("app/collector/synthetic.py")


def _run(mod, monkeypatch, row_unchanged):
    sqls = []

    class Cur:
        def execute(self, sql, params=None):
            self._last = " ".join(sql.split())
            sqls.append((self._last, params))

        def fetchall(self):
            return [dict(_DUE)] if self._last.startswith("SELECT id, aws_account_id") else []

        def fetchone(self):
            if self._last.startswith("SELECT id FROM synthetic_checks"):
                return {"id": 7} if row_unchanged else None
            return None

        def close(self):
            pass
        rowcount = 0

    class Conn:
        rolled_back = 0

        def cursor(self, dictionary=False):
            return Cur()

        def commit(self):
            pass

        def rollback(self):
            Conn.rolled_back += 1

        def close(self):
            pass
    monkeypatch.setattr(mod, "get_connection", lambda: Conn())
    monkeypatch.setattr(mod, "_run_probe", lambda check: (True, 12, 200, None))
    return mod.run_due_checks(), sqls


def test_unchanged_check_is_recorded_and_row_is_locked(monkeypatch):
    n, sqls = _run(_mod(), monkeypatch, row_unchanged=True)
    assert n == 1
    guard = [s for s, _ in sqls if s.startswith("SELECT id FROM synthetic_checks")][0]
    assert guard.endswith("FOR UPDATE")
    assert [s for s, _ in sqls if s.startswith("INSERT INTO synthetic_check_results")]


def test_check_edited_during_probe_is_discarded(monkeypatch):
    n, sqls = _run(_mod(), monkeypatch, row_unchanged=False)
    assert n == 0
    assert not [s for s, _ in sqls if s.startswith("INSERT INTO synthetic_check_results")]
    assert not [s for s, _ in sqls if s.startswith("UPDATE synthetic_checks")]
