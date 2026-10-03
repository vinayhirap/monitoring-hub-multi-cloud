# tests/test_audit_p1_drift_and_health_retry.py
"""
Audit Phase 1 (A2 / D1 / D2):
  * resource_id width check must not report a correctly-widened column
    (alert_pending is 512 after migration 059) and must still report a
    column that is genuinely too narrow.
  * recompute_health_scores must retry on MySQL 1213/1205 and must not use
    a locking multi-table DELETE.
"""
import sys

import app          # noqa: F401
import app.auth     # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, FakeCursor  # noqa: E402


# ── width drift ──────────────────────────────────────────────────────

def _integrity():
    install_stub("app.db", get_connection=lambda: None)
    return load_module("app/collector/integrity_check.py")


def _widths_cursor(widths):
    def pred(sql, params):
        return "information_schema.COLUMNS" in sql
    class C(FakeCursor):
        def execute(self, sql, params=None):
            self._pending = [{"CHARACTER_MAXIMUM_LENGTH": widths[(params[0], params[1])]}]
    return C([])


def test_expected_alert_pending_width_is_512():
    mod = _integrity()
    assert mod._EXPECTED_RESOURCE_ID_WIDTHS[("alert_pending", "resource_id")] == 512


def test_fully_migrated_schema_reports_no_drift():
    mod = _integrity()
    widths = dict(mod._EXPECTED_RESOURCE_ID_WIDTHS)
    assert mod.check_resource_id_column_widths(_widths_cursor(widths)) == []


def test_wider_than_expected_is_not_drift():
    mod = _integrity()
    widths = {k: v + 100 for k, v in mod._EXPECTED_RESOURCE_ID_WIDTHS.items()}
    assert mod.check_resource_id_column_widths(_widths_cursor(widths)) == []


def test_narrow_column_is_still_reported():
    mod = _integrity()
    widths = dict(mod._EXPECTED_RESOURCE_ID_WIDTHS)
    widths[("alerts", "resource_id")] = 50
    drift = mod.check_resource_id_column_widths(_widths_cursor(widths))
    assert drift == [{"table": "alerts", "column": "resource_id", "expected": 512, "actual": 50}]


# ── health recompute retry ───────────────────────────────────────────

class _DeadlockError(Exception):
    errno = 1213


def _health():
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.alert_rules", firing_where=lambda: "1=1", base_where=lambda: "1=1")
    return load_module("app/collector/health_score.py")


def test_retries_on_deadlock_then_succeeds(monkeypatch):
    mod = _health()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _DeadlockError("Deadlock found")
        return 7

    monkeypatch.setattr(mod, "_recompute_health_scores_once", flaky)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    assert mod.recompute_health_scores() == 7
    assert calls["n"] == 3


def test_gives_up_after_max_attempts(monkeypatch):
    mod = _health()
    calls = {"n": 0}

    def always():
        calls["n"] += 1
        raise _DeadlockError("Deadlock found")

    monkeypatch.setattr(mod, "_recompute_health_scores_once", always)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    try:
        mod.recompute_health_scores()
        assert False, "should have raised"
    except _DeadlockError:
        pass
    assert calls["n"] == mod._MAX_ATTEMPTS


def test_non_retryable_error_is_not_retried(monkeypatch):
    mod = _health()
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ValueError("syntax")

    monkeypatch.setattr(mod, "_recompute_health_scores_once", boom)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    try:
        mod.recompute_health_scores()
    except ValueError:
        pass
    assert calls["n"] == 1


def test_recovery_delete_is_by_primary_key_not_a_join():
    src = open(__file__.rsplit("/tests/", 1)[0] + "/app/collector/health_score.py").read()
    assert "DELETE rh FROM" not in src
    assert "DELETE FROM resource_health WHERE aws_account_id = %s AND resource_id = %s" in src
