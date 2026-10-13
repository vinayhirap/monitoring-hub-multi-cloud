# tests/test_synthetic_edit_state_reset.py
"""PATCH /api/synthetic-checks/{id}: an edit that changes WHAT is probed restarts the failure streak and schedules
an immediate probe; an interval-only edit pulls a later next_check_at in; cosmetic edits touch nothing else."""
import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app          # noqa: F401
import app.auth     # noqa: F401

from tests.conftest import load_module, install_stub, FakeConn

_USER = {"id": 1, "username": "u"}


def _api():
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    return load_module("app/api/synthetic.py")


def _patch(monkeypatch, payload, check_type="http"):
    mod = _api()
    sqls = []

    class Cur:
        def execute(self, sql, params=None):
            sqls.append((" ".join(sql.split()), tuple(params or ())))

        def close(self):
            pass

    class Conn:
        def cursor(self, dictionary=False):
            return Cur()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(mod, "_get_check_account_id", lambda cid: 4)
    monkeypatch.setattr(mod, "_get_check_row", lambda cid: {
        "check_type": check_type, "target": "http://8.8.8.8/", "expect_https_redirect": 0})
    monkeypatch.setattr(mod, "get_connection", lambda: Conn())
    assert mod.update_check(7, payload, current_user=_USER)["status"] == "updated"
    updates = [s for s in sqls if s[0].startswith("UPDATE synthetic_checks")]
    assert len(updates) == 1
    return updates[0]


@pytest.mark.parametrize("payload", [
    {"target": "http://8.8.4.4/"},
    {"expected_status_code": 403},
    {"expected_keyword": "ok"},
    {"timeout_seconds": 15},
])
def test_probe_affecting_edit_restarts_streak_and_reprobes(monkeypatch, payload):
    sql, params = _patch(monkeypatch, payload)
    assert "consecutive_failures = 0" in sql and "next_check_at = NOW()" in sql
    assert params[-1] == 7
    # status / alert state are deliberately left to the next probe
    assert "current_status" not in sql and "last_error" not in sql


def test_interval_only_edit_only_pulls_schedule_in(monkeypatch):
    sql, params = _patch(monkeypatch, {"interval_seconds": 120})
    assert "consecutive_failures" not in sql
    assert "next_check_at = LEAST(COALESCE(next_check_at, NOW()), DATE_ADD(NOW(), INTERVAL %s SECOND))" in sql
    assert params == (120, 120, 7)


@pytest.mark.parametrize("payload", [{"name": "renamed"}, {"consecutive_failure_threshold": 5}])
def test_cosmetic_edit_touches_no_state(monkeypatch, payload):
    sql, _ = _patch(monkeypatch, payload)
    assert "consecutive_failures" not in sql and "next_check_at" not in sql


def test_probe_edit_beats_interval_in_same_request(monkeypatch):
    sql, params = _patch(monkeypatch, {"target": "http://8.8.4.4/", "interval_seconds": 90})
    assert "consecutive_failures = 0" in sql and "LEAST" not in sql
    assert sorted(map(str, params)) == sorted(["http://8.8.4.4/", "90", "7"]) and params[-1] == 7
