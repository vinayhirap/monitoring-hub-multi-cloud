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
"""
import logging
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
router = APIRouter(prefix="/api/reports", tags=["Reports"])

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
    if scope_type == "INCIDENT" and not account_id:
        raise HTTPException(status_code=400, detail="account_id is required for scope_type=INCIDENT "
                                                      "(incidents are looked up per-account, same as the Incidents page)")
    _require_account_access(account_id, current_user)

    start, end = _resolve_period(report_type, period_start, period_end)

    with get_db_cursor() as (_, cur):
        cur.execute(
            """INSERT INTO report_jobs
               (report_type, scope_type, scope_id, account_id, period_start, period_end,
                requested_by, requested_by_role)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (report_type, scope_type, scope_id, account_id, start, end,
             current_user["username"], current_user.get("role")),
        )
        job_id = cur.lastrowid

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
    return job


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
    this once those env vars are filled in."""
    if not mailer.is_configured():
        raise HTTPException(
            status_code=501,
            detail="SMTP is not configured. Set SMTP_HOST/SMTP_PORT/SMTP_USERNAME/"
                   "SMTP_PASSWORD/SMTP_FROM in .env to enable emailing reports.",
        )
    report = _load_report_or_404(report_id, current_user)
    try:
        data = s3_client.get_report_bytes(report["s3_key"], report["sha256"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not retrieve report: {e}")

    sent = mailer.send_report_email(to_addr, report, data)
    if not sent:
        raise HTTPException(status_code=502, detail="Email send failed -- check server logs")

    with get_db_cursor() as (_, cur):
        cur.execute("UPDATE reports SET emailed_at=NOW(), emailed_to=%s WHERE id=%s", (to_addr, report_id))
    write_audit(current_user["username"], "Report emailed",
                f"report_id={report_id} to={to_addr}", role=current_user.get("role"), request=request)
    return {"status": "sent", "to": to_addr}
