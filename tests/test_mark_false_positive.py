# tests/test_mark_false_positive.py
"""
Coverage for PATCH /alerts/{id}/false-positive (2026-09-14) -- closes
the loop on this session's false-alert-reduction work by letting a
human directly confirm an alert wasn't genuine, feeding
app/collector/threshold_tuning.py's new manually_confirmed path.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app          # noqa: F401 -- real, empty __init__.py, safe
import app.auth     # noqa: F401 -- see test_resource_health_json_parsing.py's
                     # own comment for why this must precede install_stub()
                     # touching any app.auth.* leaf module.

from tests.conftest import load_module, install_stub, FakeCursor, FakeConn


def _install_common_stubs(accessible_accounts=None):
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: accessible_accounts)
    install_stub("app.auth.deps", get_current_user=lambda: None, require_role=lambda *a: (lambda: None))
    install_stub("app.aws.federation", NoConsoleCredentialsError=Exception)
    install_stub("app.ws.publisher", publish_alert_resolved=lambda *a, **k: None)
    install_stub("app.api.live_data", invalidate_accounts_cache=lambda: None)


def _install_db_stub(account_id_row, alert_row, update_calls):
    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT acc.id AS account_id"):
                self._pending = [account_id_row] if account_id_row else []
            elif normalized.startswith("SELECT resource_id, metric_name, severity FROM alerts"):
                self._pending = [alert_row] if alert_row else []
            elif normalized.startswith("UPDATE alerts"):
                update_calls.append((normalized, params))
                self._pending = []
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))


def test_mark_false_positive_sets_columns_and_writes_audit():
    audit_calls = []
    install_stub("app.audit", write_audit=lambda *a, **k: audit_calls.append((a, k)))
    _install_common_stubs(accessible_accounts=None)

    updates = []
    _install_db_stub(
        account_id_row={"account_id": 7},
        alert_row={"resource_id": "i-abc", "metric_name": "NetworkOut", "severity": "WARNING"},
        update_calls=updates,
    )
    mod = load_module("app/api/alerts.py")

    result = mod.mark_false_positive(
        alert_id=42, payload={"marked": True},
        current_user={"username": "admin", "role": "admin"},
    )

    assert result == {"status": "updated", "marked_false_positive": True}
    assert len(updates) == 1
    sql, params = updates[0]
    assert "marked_false_positive = 1" in sql
    assert params[0] == "admin"  # false_positive_marked_by
    assert len(audit_calls) == 1


def test_unmark_false_positive_clears_columns():
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    _install_common_stubs(accessible_accounts=None)

    updates = []
    _install_db_stub(
        account_id_row={"account_id": 7},
        alert_row={"resource_id": "i-abc", "metric_name": "NetworkOut", "severity": "WARNING"},
        update_calls=updates,
    )
    mod = load_module("app/api/alerts.py")

    result = mod.mark_false_positive(
        alert_id=42, payload={"marked": False},
        current_user={"username": "admin", "role": "admin"},
    )

    assert result == {"status": "updated", "marked_false_positive": False}
    sql, params = updates[0]
    assert "marked_false_positive = 0" in sql


def test_default_payload_marks_true():
    """An empty/omitted body should default to marking=True (the
    common case: clicking 'not genuine' on an alert)."""
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    _install_common_stubs(accessible_accounts=None)

    updates = []
    _install_db_stub(
        account_id_row={"account_id": 7},
        alert_row={"resource_id": "i-abc", "metric_name": "NetworkOut", "severity": "WARNING"},
        update_calls=updates,
    )
    mod = load_module("app/api/alerts.py")

    result = mod.mark_false_positive(alert_id=42, payload={}, current_user={"username": "admin", "role": "admin"})
    assert result["marked_false_positive"] is True


def test_mark_false_positive_404_for_unknown_alert():
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    _install_common_stubs(accessible_accounts=None)

    updates = []
    _install_db_stub(account_id_row=None, alert_row=None, update_calls=updates)
    mod = load_module("app/api/alerts.py")

    from fastapi import HTTPException
    try:
        mod.mark_false_positive(alert_id=99999, payload={}, current_user={"username": "admin", "role": "admin"})
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404


def test_mark_false_positive_403_outside_accessible_scope():
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    _install_common_stubs(accessible_accounts={99})  # alert belongs to account 7, not 99

    updates = []
    _install_db_stub(
        account_id_row={"account_id": 7},
        alert_row={"resource_id": "i-abc", "metric_name": "NetworkOut", "severity": "WARNING"},
        update_calls=updates,
    )
    mod = load_module("app/api/alerts.py")

    from fastapi import HTTPException
    try:
        mod.mark_false_positive(alert_id=42, payload={}, current_user={"username": "restricted", "role": "viewer"})
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 403
    assert updates == []
