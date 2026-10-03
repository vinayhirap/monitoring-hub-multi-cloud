# app/api/reports.py
"""
CloudOps client/stakeholder report engine API.

Flow: CloudOps -> POST /generate (enqueues, returns immediately) ->
background worker (app/reports/worker.py) builds the PDF and puts it
in S3 -> GET /{id}/status polls -> GET /{id}/download streams it back
(RBAC + account-scope + integrity-checked) -> POST /{id}/email sends it
via SMTP once SMTP_HOST is configured (app/email/mailer.py), a no-op
501 until then.

RBAC: reports.view / reports.generate / reports.download / reports.email
(db/migrations/047_reports_engine.sql), layered with the SAME
account-scope check as incidents/alerts (get_accessible_account_ids) --
a report is just another account-scoped artifact, not a separate
authorization model.

Per-environment enable flag (2026-09-19): REPORTS_ENABLED=true|false in
.env, defaulting to false -- deliberately opt-IN, unlike most flags in
this app, so a fresh checkout never spins up the S3-writing background
sweeper (app/reports/worker.py's run_sweeper_loop) or accepts report
requests until someone explicitly turns it on for that box. Intended
use: enabled on prod only, left off on dev, so dev doesn't run S3
background jobs, doesn't need the IAM policy live there at all (even
though the instance role is shared with prod), and its logs/journalctl
stay free of report-sweeper noise. Every endpoint here calls
_require_enabled() first and returns 503 until then, same pattern as
app/api/sso.py's _check_enabled() for SSO_SAML_ENABLED. This is a
dependency check, not conditional router mounting, so the route always
exists (consistent 503 with a clear message rather than a bare 404)
and flipping the flag needs nothing but a restart -- no code change,
no redeploy.
"""
import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
import io

from app.audit import write_audit
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids
from app.db import get_db_cursor
from app.email import mailer
from app.reports import s3_client
from app.reports.worker import run_job

logger = logging.getLogger(__name__)
def is_enabled() -> bool:
    return os.getenv("REPORTS_ENABLED", "false").strip().lower() in ("true", "1", "yes")


def _require_enabled():
    if not is_enabled():
        raise HTTPException(
            status_code=503,
            detail="Reports is not enabled on this environment -- set REPORTS_ENABLED=true in .env to activate it",
        )


router = APIRouter(prefix="/api/reports", tags=["Reports"], dependencies=[Depends(_require_enabled)])

_VALID_REPORT_TYPES = {"WEEKLY", "MONTHLY", "QUARTERLY", "CUSTOM"}
_VALID_SCOPE_TYPES = {"ACCOUNT", "RESOURCE", "INCIDENT", "CLIENT"}


def _require_account_access(account_id: int | None, user: dict) -> None:
    if account_id is None:
        return
    accessible = get_accessible_account_ids(user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")


def _resolve_period(report_type: str, period_start: str | None, period_end: str | None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if report_type == "WEEKLY":
        return now - timedelta(days=7), now
    if report_type == "MONTHLY":
        return now - timedelta(days=30), now
    if report_type == "QUARTERLY":
        return now - timedelta(days=90), now
    # CUSTOM
    if not period_start or not period_end:
        raise HTTPException(status_code=400, detail="period_start and period_end are required for CUSTOM reports")
    try:
        start = datetime.fromisoformat(period_start)
        end = datetime.fromisoformat(period_end)
    except ValueError:
        raise HTTPException(status_code=400, detail="period_start/period_end must be ISO-8601")
    if end <= start:
        raise HTTPException(status_code=400, detail="period_end must be after period_start")
    if (end - start) > timedelta(days=400):
        raise HTTPException(status_code=400, detail="Custom range too large (max ~400 days)")
    return start, end


_INFLIGHT_MAX_MINUTES = 15       # a QUEUED/PROCESSING job older than this is presumed stuck and does not block
_RECENT_COMPLETE_SECONDS = 60    # a just-finished identical report answers a double click


def _find_duplicate_job(cur, report_type, scope_type, scope_id, account_id, username, start, end):
    """Most recent job that already satisfies an identical request, or None."""
    sql = """SELECT id, status FROM report_jobs
             WHERE report_type = %s AND scope_type = %s AND scope_id = %s
               AND account_id <=> %s AND requested_by = %s
               AND ( (status IN ('QUEUED','PROCESSING') AND created_at > NOW() - INTERVAL %s MINUTE)
                  OR (status = 'COMPLETE' AND created_at > NOW() - INTERVAL %s SECOND) )"""
    params = [report_type, scope_type, scope_id, account_id, username,
              _INFLIGHT_MAX_MINUTES, _RECENT_COMPLETE_SECONDS]
    if report_type == "CUSTOM":      # fixed ranges: the same range, not merely the same type
        sql += " AND period_start = %s AND period_end = %s"
        params += [start, end]
    cur.execute(sql + " ORDER BY id DESC LIMIT 1", params)
    return cur.fetchone()


@router.post("/generate")
def generate_report(
    background_tasks: BackgroundTasks,
    request: Request,
    report_type: str = Query(...),
    scope_type: str = Query(...),
    scope_id: str = Query(...),
    account_id: int | None = Query(None),
    period_start: str | None = Query(None),
    period_end: str | None = Query(None),
    current_user: dict = Depends(require_permission("reports.generate")),
):
    report_type = report_type.upper()
    scope_type = scope_type.upper()
    if report_type not in _VALID_REPORT_TYPES:
        raise HTTPException(status_code=400, detail=f"report_type must be one of {sorted(_VALID_REPORT_TYPES)}")
    if scope_type not in _VALID_SCOPE_TYPES:
        raise HTTPException(status_code=400, detail=f"scope_type must be one of {sorted(_VALID_SCOPE_TYPES)}")
    if scope_type in ("INCIDENT", "RESOURCE", "ACCOUNT") and not account_id:
        raise HTTPException(status_code=400, detail=f"account_id is required for scope_type={scope_type} "
                                                      "-- a resource/incident id is only unique within one "
                                                      "account, not globally (see db/migrations/048_add_account_scoping_to_alerts.sql). "
                                                      "Without it, gather_report_data() applies no account filter at all and the "
                                                      "resulting report aggregates every account's alerts/incidents.")
    # CLIENT is the one scope type that legitimately spans multiple
    # accounts. account_id is deliberately never required for it, but
    # that also means account_id stays NULL on the resulting
    # report_jobs/reports row, and _require_account_access() below is
    # a no-op for a NULL account_id -- there is no RBAC scope
    # dimension for "client" (2e79edb removed it from the UI as
    # "unbacked" but the API enum still accepts it), so any principal
    # holding the role-level reports.generate/reports.download
    # permission could otherwise generate or pull a report spanning
    # every account in the system regardless of their own account
    # scope. Until a real client-scope dimension exists, restrict
    # CLIENT-scoped generation to admin, matching the precedent this
    # catalog already set for accounts.delete/credentials.manage and
    # every rbac.* code (041/049): actions that cross the account
    # boundary stay admin-only regardless of scope.
    if scope_type == "CLIENT" and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="CLIENT-scoped reports span multiple accounts and are admin-only")
    _require_account_access(account_id, current_user)

    start, end = _resolve_period(report_type, period_start, period_end)

    # IDEMPOTENCY (audit B11/C6): a double click, a retry after a slow response, or two tabs used to
    # create two jobs and two identical PDFs (the library held duplicate "Weekly" reports and the audit
    # log showed two requests 44 s apart). An identical request by the same user for the same scope is
    # now answered with the job that already exists -- one still running (up to _INFLIGHT_MAX_MINUTES)
    # or one that finished within the last _RECENT_COMPLETE_SECONDS. FAILED jobs never block a retry.
    # GET_LOCK serialises the check-then-insert so two simultaneous requests cannot both miss.
    lock_name = "report_gen:" + hashlib.sha1(
        f"{report_type}|{scope_type}|{scope_id}|{account_id}|{current_user['username']}".encode()
    ).hexdigest()
    with get_db_cursor(dictionary=True) as (_, cur):
        cur.execute("SELECT GET_LOCK(%s, 3) AS got", (lock_name,))
        try:
            existing = _find_duplicate_job(cur, report_type, scope_type, scope_id, account_id,
                                           current_user["username"], start, end)
            if existing:
                return {"job_id": existing["id"], "status": existing["status"], "deduplicated": True}
            cur.execute(
                """INSERT INTO report_jobs
                   (report_type, scope_type, scope_id, account_id, period_start, period_end,
                    requested_by, requested_by_role)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (report_type, scope_type, scope_id, account_id, start, end,
                 current_user["username"], current_user.get("role")),
            )
            job_id = cur.lastrowid
        finally:
            cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))

    write_audit(current_user["username"], "Report generation requested",
                f"{report_type} report for {scope_type}={scope_id}",
                role=current_user.get("role"), request=request)

    # Runs AFTER this response is returned -- generate_report itself
    # never blocks on the DB query + PDF render + S3 PUT below.
    background_tasks.add_task(run_job, job_id)
    return {"job_id": job_id, "status": "QUEUED"}


@router.get("/jobs/{job_id}/status")
def get_job_status(job_id: int, current_user: dict = Depends(require_permission("reports.view"))):
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        cur.execute("SELECT * FROM report_jobs WHERE id=%s", (job_id,))
        job = cur.fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _require_account_access(job["account_id"], current_user)
    # AUDIT FIX (b21/082, MEDIUM): error_message is run_job()'s raw
    # str(exception) (see worker.py's _mark_failed), stored verbatim and
    # previously returned as-is via this SELECT * -- raw exception text
    # returned to a client regardless of who's asking. The full text is
    # already in the server logs (worker.py logs it with a traceback);
    # this endpoint only needs to tell the caller their job failed.
    # FOLLOW-UP (b21): the redaction used to run only for status=FAILED,
    # but a job that failed once and then succeeded on a retry ends up
    # COMPLETE with its earlier raw exception text still in error_message
    # (found on prod: job 1, COMPLETE, attempts=2, "Unknown column 'name'").
    # Redact whenever any raw text is present, whatever the status.
    if job.get("error_message"):
        if job.get("status") == "COMPLETE":
            job["error_message"] = None
        else:
            job["error_message"] = "Report generation failed -- contact an administrator for details."
    return job


@router.get("/resources")
def list_scopeable_resources(
    account_id: int = Query(...),
    current_user: dict = Depends(require_permission("reports.view")),
):
    """Resources for the RESOURCE-scope dropdown on the Reports page.
    Deliberately its own query rather than reusing app/api/live_data.py's
    per-service endpoints (/api/live/ec2/{id}, /api/live/rds/{id}, ...)
    -- those need a service picked first and would mean 7 separate
    dropdowns; this reads the resources table directly, which already
    has every service in one place. Capped at 1000, generous for
    per-account resource counts (unlike incidents/alerts, which can
    genuinely run into the thousands -- see /reports/incidents below)."""
    _require_account_access(account_id, current_user)
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        cur.execute(
            "SELECT resource_id, resource_type, name, region FROM resources "
            "WHERE aws_account_id=%s ORDER BY resource_type, name LIMIT 1000",
            (account_id,),
        )
        return cur.fetchall()


@router.get("/incidents")
def list_scopeable_incidents(
    account_id: int = Query(...),
    limit: int = Query(50, le=200),
    current_user: dict = Depends(require_permission("reports.view")),
):
    """Incidents for the INCIDENT-scope dropdown. Capped at 50 by
    default (200 max) -- an account's full incident history can be
    large, so this intentionally shows only the most recent ones
    rather than trying to be exhaustive; the UI also keeps a manual
    ID entry field for anything older than what's listed here."""
    _require_account_access(account_id, current_user)
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        cur.execute(
            "SELECT id, title, severity, status, started_at FROM incidents "
            "WHERE aws_account_id=%s ORDER BY last_seen_at DESC LIMIT %s",
            (account_id, limit),
        )
        return cur.fetchall()


@router.get("")
def list_reports(
    account_id: int | None = Query(None),
    scope_type: str | None = Query(None),
    scope_id: str | None = Query(None),
    limit: int = Query(50, le=200),
    current_user: dict = Depends(require_permission("reports.view")),
):
    """History: previously generated reports, filterable by
    client/account/resource/incident. Account-scoped the same way as
    every other list endpoint in this app."""
    accessible = get_accessible_account_ids(current_user)
    where, params = [], []
    if account_id is not None:
        _require_account_access(account_id, current_user)
        where.append("account_id = %s"); params.append(account_id)
    elif accessible is not None:
        if not accessible:
            return []
        where.append(f"account_id IN ({','.join(['%s'] * len(accessible))})")
        params.extend(accessible)
    if scope_type:
        where.append("scope_type = %s"); params.append(scope_type.upper())
    if scope_id:
        where.append("scope_id = %s"); params.append(scope_id)

    sql = "SELECT id, job_id, report_type, scope_type, scope_id, scope_label, account_id, " \
          "period_start, period_end, size_bytes, generated_by, generated_at, expires_at, " \
          "emailed_at, emailed_to FROM reports"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY generated_at DESC LIMIT %s"
    params.append(limit)

    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        cur.execute(sql, params)
        return cur.fetchall()


def _load_report_or_404(report_id: int, current_user: dict) -> dict:
    with get_db_cursor(dictionary=True, commit=False) as (_, cur):
        cur.execute("SELECT * FROM reports WHERE id=%s", (report_id,))
        report = cur.fetchone()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if report["account_id"] is None:
        # No account to scope-check against -- a CLIENT-scoped report
        # (or a pre-fix legacy row left over from before this patch).
        # Same admin-only line drawn in generate_report() for CLIENT:
        # there is no RBAC scope dimension to check a non-admin
        # against here, so fail closed rather than let
        # _require_account_access's None no-op wave it through.
        if current_user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="This report has no account scope and can only be accessed by an admin")
        return report
    _require_account_access(report["account_id"], current_user)
    return report


@router.get("/{report_id}/download")
def download_report(
    report_id: int,
    request: Request,
    current_user: dict = Depends(require_permission("reports.download")),
):
    report = _load_report_or_404(report_id, current_user)
    try:
        data = s3_client.get_report_bytes(report["s3_key"], report["sha256"])
    except ValueError:
        raise HTTPException(status_code=409, detail="Report failed integrity verification -- contact support")
    except Exception as e:
        logger.error(f"report {report_id} download failed: {e}")
        raise HTTPException(status_code=502, detail="Could not retrieve report from storage")

    write_audit(current_user["username"], "Report downloaded",
                f"report_id={report_id} key={report['s3_key']}",
                role=current_user.get("role"), request=request)

    filename = report["s3_key"].rsplit("/", 1)[-1]
    return StreamingResponse(
        io.BytesIO(data),
        media_type=report["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_EMAIL_RE = re.compile(r"^[^\s@\"'<>\r\n]+@[^\s@\"'<>\r\n]+\.[^\s@\"'<>\r\n]+$")


@router.post("/{report_id}/email")
def email_report(
    report_id: int,
    request: Request,
    to_addr: str = Query(...),
    current_user: dict = Depends(require_permission("reports.email")),
):
    """SMTP-gated: returns 501 until SMTP_HOST/SMTP_PORT/SMTP_USERNAME/
    SMTP_PASSWORD/SMTP_FROM(or MAIL_FROM) are set in .env -- see
    app/email/mailer.py. Nothing else needs code changes to enable
    this once those env vars are filled in.

    AUDIT FIX (b21/082, HIGH): to_addr previously had zero validation --
    any string at all, including one containing CR/LF (a classic email-
    header-injection vector if app/email/mailer.py's own message
    construction doesn't already guard against it) was accepted as-is.
    reports.email is granted to the 'editor' role by default (see
    db/migrations/047_reports_engine.sql), not just admin, so this was
    reachable by a broad set of non-admin users -- a real exfiltration
    path: anyone holding reports.email could have any already-generated
    report (which can span a full account's or, if scope_type=CLIENT,
    every account's incident/alert history) emailed to an arbitrary
    external address, with no recipient restriction. This fix adds
    basic format validation (also rejects CR/LF and quote characters,
    closing the header-injection angle at this layer regardless of
    what mailer.py does). It deliberately does NOT restrict *which*
    well-formed addresses are allowed -- whether to cap this to
    known/registered stakeholder addresses or require admin approval
    for external domains is a product policy decision, not a bug fix;
    see this audit's findings/handoff notes.
    """
    if not _EMAIL_RE.match(to_addr):
        raise HTTPException(status_code=400, detail="to_addr is not a valid email address")
    if not mailer.is_configured():
        raise HTTPException(
            status_code=501,
            detail="SMTP is not configured. Set SMTP_HOST/SMTP_PORT/SMTP_USERNAME/"
                   "SMTP_PASSWORD/SMTP_FROM in .env to enable emailing reports.",
        )
    report = _load_report_or_404(report_id, current_user)
    try:
        data = s3_client.get_report_bytes(report["s3_key"], report["sha256"])
    except ValueError:
        raise HTTPException(status_code=409, detail="Report failed integrity verification -- contact support")
    except Exception as e:
        # AUDIT FIX (b21/082, MEDIUM): previously f"Could not retrieve
        # report: {e}" -- raw exception text returned to the client.
        # download_report just above already gets this right; mirror it.
        logger.error(f"report {report_id} email fetch failed: {e}")
        raise HTTPException(status_code=502, detail="Could not retrieve report from storage")

    sent = mailer.send_report_email(to_addr, report, data)
    if not sent:
        raise HTTPException(status_code=502, detail="Email send failed -- check server logs")

    with get_db_cursor() as (_, cur):
        cur.execute("UPDATE reports SET emailed_at=NOW(), emailed_to=%s WHERE id=%s", (to_addr, report_id))
    write_audit(current_user["username"], "Report emailed",
                f"report_id={report_id} to={to_addr}", role=current_user.get("role"), request=request)
    return {"status": "sent", "to": to_addr}
