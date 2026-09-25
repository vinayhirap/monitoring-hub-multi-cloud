# tests/test_reports_account_scoping.py
"""
Regression coverage for the D03 audit finding in
db/migrations/047_reports_engine.sql / app/api/reports.py: ACCOUNT-
and CLIENT-scoped reports could be generated and downloaded with no
account_id filter at all, aggregating every onboarded account's
alerts/incidents into one report that any principal holding the
(role-level, not account-scoped) reports.generate/reports.download
permission could produce or fetch -- a cross-tenant data leak.

Covers the two-part fix:
  1. generate_report() now requires account_id for scope_type ==
     "ACCOUNT" (previously only INCIDENT/RESOURCE), and refuses
     scope_type == "CLIENT" for a non-admin caller.
  2. _load_report_or_404() fails closed (admin-only) for any report
     row whose account_id is NULL, instead of letting
     _require_account_access()'s "account_id is None -> no check"
     no-op wave a non-admin straight through.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn


class _CursorWithLastRowId(FakeCursor):
    lastrowid = 99


class _DBCursorCtx:
    """Fake for the `with get_db_cursor(...) as (conn, cursor):` context
    manager app/db.py provides -- reports.py uses this, not the older
    get_connection()/cursor()/close() pattern."""
    def __init__(self, script):
        self._cursor = _CursorWithLastRowId(script)

    def __enter__(self):
        return (None, self._cursor)

    def __exit__(self, *exc):
        return False


def _install_stub(script):
    def _get_db_cursor(dictionary=True, commit=True):
        return _DBCursorCtx(script)

    install_stub("app.db", get_db_cursor=_get_db_cursor, get_connection=lambda: FakeConn(script))
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    install_stub("app.email.mailer", is_configured=lambda: False, send_report_email=lambda *a, **k: True)
    install_stub("app.reports.s3_client", get_report_bytes=lambda key, sha: b"pdf-bytes")
    install_stub("app.reports.worker", run_job=lambda job_id: None)


def _load_reports_module(script=None):
    _install_stub(script or [])
    return load_module("app/api/reports.py")


class _FakeBackgroundTasks:
    def add_task(self, fn, *args, **kwargs):
        pass


def _user(role="editor"):
    return {"username": "priya", "role": role}


# ── generate_report: ACCOUNT scope now requires account_id ────────────

def test_generate_report_account_scope_requires_account_id():
    mod = _load_reports_module()
    try:
        mod.generate_report(
            background_tasks=_FakeBackgroundTasks(),
            request=None,
            report_type="WEEKLY",
            scope_type="ACCOUNT",
            scope_id="1",
            account_id=None,
            period_start=None,
            period_end=None,
            current_user=_user("editor"),
        )
        assert False, "expected HTTPException for missing account_id on ACCOUNT scope"
    except Exception as e:
        assert getattr(e, "status_code", None) == 400


def test_generate_report_account_scope_with_account_id_succeeds():
    from tests.conftest import contains
    script = [
        (contains("INSERT INTO report_jobs"), []),
    ]
    mod = _load_reports_module(script)
    result = mod.generate_report(
        background_tasks=_FakeBackgroundTasks(),
        request=None,
        report_type="WEEKLY",
        scope_type="ACCOUNT",
        scope_id="1",
        account_id=1,
        period_start=None,
        period_end=None,
        current_user=_user("editor"),
    )
    assert result["status"] == "QUEUED"


# ── generate_report: CLIENT scope is admin-only ────────────────────────

def test_generate_report_client_scope_denied_for_non_admin():
    mod = _load_reports_module()
    try:
        mod.generate_report(
            background_tasks=_FakeBackgroundTasks(),
            request=None,
            report_type="MONTHLY",
            scope_type="CLIENT",
            scope_id="AuroGov",
            account_id=None,
            period_start=None,
            period_end=None,
            current_user=_user("editor"),
        )
        assert False, "expected HTTPException: CLIENT scope must be admin-only"
    except Exception as e:
        assert getattr(e, "status_code", None) == 403


def test_generate_report_client_scope_allowed_for_admin():
    from tests.conftest import contains
    script = [
        (contains("INSERT INTO report_jobs"), []),
    ]
    mod = _load_reports_module(script)
    result = mod.generate_report(
        background_tasks=_FakeBackgroundTasks(),
        request=None,
        report_type="MONTHLY",
        scope_type="CLIENT",
        scope_id="AuroGov",
        account_id=None,
        period_start=None,
        period_end=None,
        current_user=_user("admin"),
    )
    assert result["status"] == "QUEUED"


# ── _load_report_or_404: NULL account_id fails closed ──────────────────

def test_load_report_null_account_id_denied_for_non_admin():
    from tests.conftest import contains
    script = [
        (contains("SELECT * FROM reports WHERE id"),
         [{"id": 7, "account_id": None, "s3_key": "k", "sha256": "h"}]),
    ]
    mod = _load_reports_module(script)
    try:
        mod._load_report_or_404(7, _user("editor"))
        assert False, "expected HTTPException for NULL-account_id report accessed by non-admin"
    except Exception as e:
        assert getattr(e, "status_code", None) == 403


def test_load_report_null_account_id_allowed_for_admin():
    from tests.conftest import contains
    script = [
        (contains("SELECT * FROM reports WHERE id"),
         [{"id": 7, "account_id": None, "s3_key": "k", "sha256": "h"}]),
    ]
    mod = _load_reports_module(script)
    report = mod._load_report_or_404(7, _user("admin"))
    assert report["id"] == 7


def test_load_report_normal_account_scoped_report_unaffected():
    """A report row WITH an account_id still goes through the ordinary
    _require_account_access() path -- this fix must not change that."""
    from tests.conftest import contains
    script = [
        (contains("SELECT * FROM reports WHERE id"),
         [{"id": 8, "account_id": 42, "s3_key": "k", "sha256": "h"}]),
    ]
    mod = _load_reports_module(script)
    # get_accessible_account_ids stubbed to return None ("unrestricted"),
    # so this must succeed for a non-admin too.
    report = mod._load_report_or_404(8, _user("editor"))
    assert report["account_id"] == 42
