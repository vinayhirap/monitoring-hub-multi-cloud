# tests/test_threshold_auto_tune_history.py
"""
Coverage for GET /api/settings/thresholds/{id}/auto-tune-history
(2026-09-14) -- surfaces app/collector/threshold_tuning.py's
audit_logs trail in the Settings UI, so an admin can see WHY a
threshold went dynamic without grepping journalctl or querying the DB
directly (as this session's live debugging had to do).

Also guards a real bug found while building this: app/audit.py's
write_audit() treats `detail` and `payload` as MUTUALLY EXCLUSIVE
(passing both silently drops `detail`) -- threshold_tuning.py's write_
audit() call was passing both, so the human-readable reason was never
actually persisted to audit_logs, only to op_events. Fixed by moving
the note text into payload["detail"] instead.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app          # noqa: F401 -- real, empty __init__.py, safe
import app.auth     # noqa: F401 -- see test_resource_health_json_parsing.py's
                     # own comment for why this must precede install_stub()
                     # touching any app.auth.* leaf module.

from tests.conftest import load_module, install_stub, FakeCursor, FakeConn


def _fake_get_db_cursor(get_connection):
    """Mirror of app.db.get_db_cursor for stubs (settings.py uses it
    since audit B07)."""
    from contextlib import contextmanager

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


def _install_db_stub(conn_factory):
    install_stub("app.db", get_connection=conn_factory,
                 get_db_cursor=_fake_get_db_cursor(conn_factory))


def _install_common_stubs():
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))


def test_threshold_tuning_note_lands_in_payload_detail_not_dropped():
    """Regression guard for the write_audit mutual-exclusivity bug --
    the note text must be inside the payload dict, not passed as a
    separate top-level `detail` kwarg alongside `payload` (which
    write_audit() silently ignores)."""
    captured = {}

    def _fake_write_audit(**kwargs):
        captured.update(kwargs)

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT t.id, t.aws_account_id"):
                self._pending = [{
                    "id": 60, "aws_account_id": 7, "resource_type": "ec2", "metric_id": 9,
                    "warning_value": 1_000_000, "critical_value": 5_000_000,
                    "comparison": ">", "dynamic_k": None, "metric_name": "NetworkIn", "account_name": "U4RAD",
                }]
            elif normalized.startswith("SELECT b.resource_id, AVG"):
                self._pending = [{"resource_id": "i-loud", "typical_value": 9_000_000,
                                  "typical_stddev": 500_000, "total_samples": 40}]
            elif normalized.startswith("SELECT COUNT(*) AS cnt FROM alerts"):
                self._pending = [{"cnt": 0}]  # no manual false-positive marks in this test
            elif normalized.startswith("SELECT id FROM alerts"):
                self._pending = [{"id": 1}]
            elif normalized.startswith("UPDATE thresholds SET use_dynamic"):
                self._pending = []
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    install_stub("app.audit", write_audit=_fake_write_audit)
    install_stub("app.collector.op_log", log_event=lambda *a, **k: None)
    mod = load_module("app/collector/threshold_tuning.py")

    switched = mod.auto_tune_static_thresholds()

    assert switched == 1
    # `detail` must NOT be a separate top-level kwarg (that's the bug --
    # write_audit() silently drops it when payload is also given).
    assert "detail" not in captured or captured.get("detail") is None
    assert "detail" in captured["payload"]
    assert len(captured["payload"]["detail"]) > 20  # a real sentence, not empty


def test_auto_tune_history_returns_parsed_detail_and_trigger_path():
    audit_row = {
        "created_at": "2026-09-14 07:13:55",
        "payload": '{"detail": "Auto-switched NetworkIn threshold...", "trigger_path": "chronic_mean", "threshold_id": 60}',
    }

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT aws_account_id FROM thresholds"):
                self._pending = [{"aws_account_id": 7}]
            elif normalized.startswith("SELECT created_at, payload"):
                self._pending = [audit_row]
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    _install_db_stub(lambda: _Conn([]))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    _install_common_stubs()
    mod = load_module("app/api/settings.py")

    result = mod.get_threshold_auto_tune_history(threshold_id=60, current_user={"username": "admin"})

    assert len(result) == 1
    assert result[0]["trigger_path"] == "chronic_mean"
    assert "Auto-switched NetworkIn" in result[0]["detail"]


def test_auto_tune_history_empty_for_never_tuned_threshold():
    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT aws_account_id FROM thresholds"):
                self._pending = [{"aws_account_id": 7}]
            elif normalized.startswith("SELECT created_at, payload"):
                self._pending = []
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    _install_db_stub(lambda: _Conn([]))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    _install_common_stubs()
    mod = load_module("app/api/settings.py")

    result = mod.get_threshold_auto_tune_history(threshold_id=61, current_user={"username": "admin"})
    assert result == []


def test_auto_tune_history_404_for_unknown_threshold():
    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            self._pending = []  # no row -> _get_threshold_account_id returns None

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    _install_db_stub(lambda: _Conn([]))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    _install_common_stubs()
    mod = load_module("app/api/settings.py")

    from fastapi import HTTPException
    try:
        mod.get_threshold_auto_tune_history(threshold_id=99999, current_user={"username": "admin"})
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404
