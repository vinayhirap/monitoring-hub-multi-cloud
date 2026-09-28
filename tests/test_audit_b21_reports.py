# tests/test_audit_b21_reports.py
"""
Regression coverage for the B21 audit (reports engine):
  - email_report: to_addr validated (rejects CR/LF + malformed), raw
    exception text no longer returned to the client.
  - get_job_status: raw error_message redacted for FAILED jobs.
  - worker._claim: only claims QUEUED jobs (not PROCESSING).
  - worker.sweep_stuck_jobs: actually re-invokes run_job for requeued
    and orphaned-QUEUED jobs.
  - engine.gather_report_data: member alerts fetched in one batched
    query, not one per incident.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, FakeCursor, contains


class _Ctx:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return (None, self._cursor)

    def __exit__(self, *exc):
        return False


class _RecordingCursor(FakeCursor):
    lastrowid = 1
    rowcount = 1

    def __init__(self, script):
        super().__init__(script)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        super().execute(sql, params)


def _stub_db(script, cursors=None):
    def _get_db_cursor(dictionary=True, commit=True):
        cur = _RecordingCursor(script)
        if cursors is not None:
            cursors.append(cur)
        return _Ctx(cur)

    install_stub("app.db", get_db_cursor=_get_db_cursor)


# ── api/reports.py ─────────────────────────────────────────────────────

def _load_reports(script=None, get_bytes=None, configured=True):
    _stub_db(script or [])
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    install_stub("app.email.mailer", is_configured=lambda: configured,
                 send_report_email=lambda *a, **k: True)
    install_stub("app.reports.s3_client",
                 get_report_bytes=get_bytes or (lambda key, sha: b"pdf"))
    install_stub("app.reports.worker", run_job=lambda job_id: None)
    return load_module("app/api/reports.py")


def _status(exc):
    return getattr(exc, "status_code", None)


def test_email_rejects_malformed_and_crlf_addresses():
    mod = _load_reports()
    for bad in ["not-an-email", "a@b", "x@y.com\r\nBcc: evil@z.com", "a b@c.com", "<a@b.com>"]:
        try:
            mod.email_report(1, None, to_addr=bad, current_user={"username": "u", "role": "editor"})
            assert False, f"expected 400 for {bad!r}"
        except Exception as e:
            assert _status(e) == 400, (bad, e)


def test_email_fetch_failure_does_not_leak_exception_text():
    def boom(key, sha):
        raise RuntimeError("secret-internal-detail bucket=xyz")

    script = [(contains("FROM reports WHERE id"),
               [{"id": 1, "account_id": 5, "s3_key": "k", "sha256": "h", "content_type": "application/pdf"}])]
    mod = _load_reports(script, get_bytes=boom)
    try:
        mod.email_report(1, None, to_addr="ok@example.com", current_user={"username": "u", "role": "editor"})
        assert False, "expected HTTPException"
    except Exception as e:
        assert _status(e) == 502
        assert "secret-internal-detail" not in str(getattr(e, "detail", ""))


def test_email_integrity_failure_is_409():
    def bad_hash(key, sha):
        raise ValueError("mismatch")

    script = [(contains("FROM reports WHERE id"),
               [{"id": 1, "account_id": 5, "s3_key": "k", "sha256": "h", "content_type": "application/pdf"}])]
    mod = _load_reports(script, get_bytes=bad_hash)
    try:
        mod.email_report(1, None, to_addr="ok@example.com", current_user={"username": "u", "role": "editor"})
        assert False
    except Exception as e:
        assert _status(e) == 409


def test_job_status_redacts_failed_error_message():
    row = {"id": 7, "account_id": 5, "status": "FAILED", "error_message": "Traceback: /opt/app/x.py password=hunter2"}
    mod = _load_reports([(contains("FROM report_jobs WHERE id"), [row])])
    out = mod.get_job_status(7, current_user={"username": "u", "role": "editor"})
    assert "hunter2" not in out["error_message"]
    assert "administrator" in out["error_message"]


def test_job_status_leaves_non_failed_untouched():
    row = {"id": 7, "account_id": 5, "status": "COMPLETE", "error_message": None}
    mod = _load_reports([(contains("FROM report_jobs WHERE id"), [row])])
    out = mod.get_job_status(7, current_user={"username": "u", "role": "editor"})
    assert out["error_message"] is None


# ── reports/worker.py ──────────────────────────────────────────────────

def _load_worker(script, cursors, run_calls=None):
    _stub_db(script, cursors)
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    install_stub("app.reports.engine",
                 gather_report_data=lambda *a, **k: {}, render_report_pdf=lambda **k: b"")
    install_stub("app.reports.s3_client")
    mod = load_module("app/reports/worker.py")
    if run_calls is not None:
        mod.run_job = lambda job_id: run_calls.append(job_id)
    return mod


def test_claim_only_matches_queued():
    cursors = []
    mod = _load_worker([(contains("UPDATE report_jobs SET status='PROCESSING'"), []),
                        (contains("SELECT * FROM report_jobs"), [{"id": 1}])], cursors)
    mod._claim(1)
    sql = cursors[0].executed[0][0]
    assert "status='QUEUED'" in sql
    assert "'PROCESSING')" not in sql and "IN ('QUEUED'" not in sql


def test_sweeper_retries_requeued_and_orphaned_jobs():
    calls, cursors = [], []
    script = [
        (contains("status='PROCESSING' AND claimed_at"),
         [{"id": 10, "attempts": 1, "max_attempts": 3},     # requeue -> retry
          {"id": 11, "attempts": 3, "max_attempts": 3}]),   # fail -> no retry
        (contains("UPDATE report_jobs SET status"), []),
        (contains("status='QUEUED' AND updated_at"), [{"id": 20}]),  # orphaned -> retry
    ]
    mod = _load_worker(script, cursors, run_calls=calls)
    n = mod.sweep_stuck_jobs()
    assert sorted(calls) == [10, 20]
    assert n == 3


# ── reports/engine.py ──────────────────────────────────────────────────

def test_member_alerts_fetched_in_one_batched_query():
    from datetime import datetime
    now = datetime(2026, 9, 1)
    incidents = [{"id": i, "title": "t", "severity": "CRITICAL", "status": "active",
                  "primary_resource_id": None, "probable_cause": None,
                  "started_at": now, "resolved_at": None, "last_seen_at": now} for i in (1, 2, 3)]
    members = [{"incident_id": 1, "id": 100, "resource_id": "r1"},
               {"incident_id": 3, "id": 101, "resource_id": "r2"}]
    script = [
        (contains("FROM alerts a JOIN resources r"), []),
        (contains("FROM incidents i"), incidents),
        (contains("FROM incident_alerts ia"), members),
    ]
    cursors = []
    _stub_db(script, cursors)
    mod = load_module("app/reports/engine.py")
    data = mod.gather_report_data("ACCOUNT", "1", None, now, now)
    ia_queries = [q for q, _ in cursors[0].executed if "FROM incident_alerts" in q]
    assert len(ia_queries) == 1
    by_id = {i["id"]: i["member_alerts"] for i in data["incidents"]}
    assert len(by_id[1]) == 1 and by_id[2] == [] and len(by_id[3]) == 1


def test_alert_and_incident_queries_are_capped():
    from datetime import datetime
    now = datetime(2026, 9, 1)
    script = [(contains("FROM alerts a JOIN resources r"), []),
              (contains("FROM incidents i"), [])]
    cursors = []
    _stub_db(script, cursors)
    mod = load_module("app/reports/engine.py")
    mod.gather_report_data("ACCOUNT", "1", None, now, now)
    joined = " ".join(q for q, _ in cursors[0].executed)
    assert f"LIMIT {mod._MAX_QUERY_ROWS}" in joined
    assert f"LIMIT {mod._MAX_QUERY_INCIDENTS}" in joined


# ── follow-up: stale error text on jobs that recovered via retry ───────

def test_job_status_clears_stale_error_on_complete_job():
    row = {"id": 1, "account_id": 5, "status": "COMPLETE",
           "error_message": "1054 (42S22): Unknown column 'name' in 'field list'"}
    mod = _load_reports([(contains("FROM report_jobs WHERE id"), [row])])
    out = mod.get_job_status(1, current_user={"username": "u", "role": "editor"})
    assert out["error_message"] is None


def test_job_status_redacts_error_on_requeued_job():
    row = {"id": 2, "account_id": 5, "status": "QUEUED", "error_message": "raw /opt/app/x.py trace"}
    mod = _load_reports([(contains("FROM report_jobs WHERE id"), [row])])
    out = mod.get_job_status(2, current_user={"username": "u", "role": "editor"})
    assert "trace" not in out["error_message"]


def test_mark_complete_clears_error_message():
    cursors = []
    script = [(contains("SELECT report_type, scope_type"),
               [("WEEKLY", "ACCOUNT", "1", 1, None, None, "u")]),
              (contains("INSERT INTO reports"), []),
              (contains("UPDATE report_jobs SET status='COMPLETE'"), [])]
    mod = _load_worker(script, cursors)
    mod._mark_complete(1, {"bucket": "b", "key": "k", "sha256": "h", "size_bytes": 1}, "label")
    sqls = [q for q, _ in cursors[0].executed]
    assert any("status='COMPLETE'" in q and "error_message=NULL" in q for q in sqls)
