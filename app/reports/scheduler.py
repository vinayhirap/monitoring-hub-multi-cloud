# app/reports/scheduler.py
"""
Runs the report schedules saved in the Email & Reports centre (report_schedules, migration 135).

One leader-guarded daemon thread (started from app/main.py next to the report sweeper, only when EMAIL_CENTER_ENABLED=true)
wakes every minute, asks app/email/schedules.due() which schedules owe a run, and for each one:

  1. CLAIMS the period with one atomic UPDATE (last_period_end), so a period is sent once even if two instances ever raced;
  2. resolves the recipient list (current user emails, group members, typed addresses; account scope applied);
  3. creates a report_jobs row requested_by 'scheduler', runs it with the normal report worker and waits for the PDF;
  4. emails the PDF to every recipient, one message each, each one written to email_delivery_log;
  5. records the outcome on the schedule (last_status SENT / PARTIAL / FAILED / SKIPPED, last_error) and in the audit log.

Environments without what a run needs are handled, not skipped silently: with REPORTS_ENABLED=false (dev) or SMTP unset the
run does everything up to the PDF, writes one 'skipped' delivery-log row per recipient with the reason, and records SKIPPED.
That lets dev exercise schedules, periods and recipient resolution without S3 or a mail server.

execute() is also what POST /api/email-center/schedules/{id}/run calls ("run now"); that path never claims a period.
"""
import logging
import os
import time
from datetime import datetime, timezone

from app.audit import write_audit
from app.db import get_db_cursor
from app.email import delivery_log, mailer, recipients, schedules

logger = logging.getLogger(__name__)

SYSTEM_USER = "scheduler"
SYSTEM_ROLE = "admin"          # runs with the report engine's full view; the recipients are filtered per account instead
_WAIT_MINUTES = int(os.getenv("REPORT_SCHEDULE_WAIT_MINUTES", "20") or "20")
_POLL_SECONDS = 5


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in ("true", "1", "yes")


def _naive_utc(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)


def _short(text, n=300):
    return " ".join(str(text or "").split())[:n]


def _allowed_domains():
    return [d.strip().lower().lstrip("@") for d in (os.getenv("REPORT_EMAIL_ALLOWED_DOMAINS") or "").split(",") if d.strip()]


def _finish(sid, status, error=None):
    """Record the outcome on the schedule. Never raises."""
    try:
        with get_db_cursor() as (_, cur):
            cur.execute("UPDATE report_schedules SET last_run_at = UTC_TIMESTAMP(), last_status = %s, last_error = %s WHERE id = %s",
                        (status, _short(error) or None, sid))
    except Exception as exc:
        logger.error(f"[report-scheduler] could not record the outcome of schedule {sid}: {exc}")
    return {"status": status, "error": error}


def _record_skips(sid, addresses, reason):
    for a in addresses:
        delivery_log.record("report", a, "", "skipped", reason, schedule_id=sid)


def _wait_for_report(job_id):
    """-> (report_row | None, error text). run_job() has already run once; a failed attempt is re-queued by the sweeper
    (up to max_attempts), so poll until the job reaches a final state."""
    deadline = time.monotonic() + _WAIT_MINUTES * 60
    while True:
        with get_db_cursor(dictionary=True, commit=False) as (_, cur):
            cur.execute("SELECT status, error_message FROM report_jobs WHERE id = %s", (job_id,))
            job = cur.fetchone()
            if job and job["status"] == "COMPLETE":
                cur.execute("SELECT * FROM reports WHERE job_id = %s", (job_id,))
                row = cur.fetchone()
                return (row, "") if row else (None, "the report finished but its record is missing")
            if job and job["status"] == "FAILED":
                return None, _short(job["error_message"]) or "report generation failed"
        if time.monotonic() >= deadline:
            return None, f"the report was not ready after {_WAIT_MINUTES} minutes"
        time.sleep(_POLL_SECONDS)


def _create_job(sched, period):
    with get_db_cursor(dictionary=True) as (_, cur):
        cur.execute("INSERT INTO report_jobs (report_type, scope_type, scope_id, account_id, period_start, period_end, "
                    "requested_by, requested_by_role) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (sched["report_type"], sched["scope_type"], sched["scope_id"], sched["account_id"],
                     period["period_start"], period["period_end"], SYSTEM_USER, SYSTEM_ROLE))
        return cur.lastrowid


def resolve_recipients(sched):
    """-> (addresses to mail, [{"who", "reason"}] left out). Applies the recipient list, the typed extras, the account
    scope and REPORT_EMAIL_ALLOWED_DOMAINS."""
    row = None
    if sched.get("recipient_list_id"):
        with get_db_cursor(dictionary=True, commit=False) as (_, cur):
            cur.execute("SELECT * FROM email_recipient_lists WHERE id = %s", (sched["recipient_list_id"],))
            row = cur.fetchone()
    res = recipients.resolve(row, extra_emails=sched.get("extra_emails"), account_id=sched.get("account_id"))
    addresses, skipped = [], list(res["skipped"])
    allowed = _allowed_domains()
    for a in res["addresses"]:
        if allowed and a.rsplit("@", 1)[-1].lower() not in allowed:
            skipped.append({"who": a, "reason": "domain not in REPORT_EMAIL_ALLOWED_DOMAINS"})
        else:
            addresses.append(a)
    return addresses, skipped


def execute(sched, period):
    """Generate and send one schedule for one period. Never raises; returns {"status", "error", "sent", "failed"}."""
    sid = sched["id"]
    try:
        addresses, skipped = resolve_recipients(sched)
        if not addresses:
            why = "; ".join(f"{s['who']}: {s['reason']}" for s in skipped[:3]) or "the recipient list is empty"
            return _finish(sid, "FAILED", "no one to send to (" + why + ")")
        if not _flag("REPORTS_ENABLED"):
            _record_skips(sid, addresses, "reports are disabled on this environment (REPORTS_ENABLED=false)")
            return _finish(sid, "SKIPPED", "Reports are disabled on this environment (REPORTS_ENABLED=false)")
        if not mailer.is_configured():
            _record_skips(sid, addresses, "SMTP is not configured (SMTP_HOST unset)")
            return _finish(sid, "SKIPPED", "SMTP is not configured (SMTP_HOST unset)")

        from app.reports import s3_client
        from app.reports.worker import run_job
        job_id = _create_job(sched, period)
        run_job(job_id)                                           # synchronous: this thread is the worker
        report, err = _wait_for_report(job_id)
        if report is None:
            return _finish(sid, "FAILED", err)
        try:
            pdf = s3_client.get_report_bytes(report["s3_key"], report["sha256"])
        except Exception as exc:
            logger.error(f"[report-scheduler] schedule {sid}: could not fetch report {report['id']}: {exc}")
            return _finish(sid, "FAILED", "the report was generated but could not be read back from storage")

        sent, failed, errors = 0, 0, []
        for a in addresses:
            ok, e = mailer.send_report_email_ex(a, report, pdf, schedule_id=sid)
            if ok:
                sent += 1
            else:
                failed += 1
                errors.append(f"{a}: {e}")
        status = "SENT" if not failed else ("PARTIAL" if sent else "FAILED")
        out = _finish(sid, status, "; ".join(errors[:3]) or None)
        out.update(sent=sent, failed=failed)
        try:
            write_audit(SYSTEM_USER, "Scheduled report emailed",
                        f"'{sched['name']}' ({sched['report_type']} {sched['scope_type']}={sched['scope_id']}): "
                        f"{sent} sent, {failed} failed, report #{report['id']}", role=SYSTEM_ROLE)
        except Exception:
            pass
        return out
    except Exception as exc:
        logger.error(f"[report-scheduler] schedule {sid} failed: {type(exc).__name__}: {exc}")
        return _finish(sid, "FAILED", f"{type(exc).__name__}: {exc}")


def _claim(sid, period_end, previous):
    """Mark the period as taken. True only for the caller whose UPDATE changed the row."""
    with get_db_cursor(dictionary=True) as (_, cur):
        cur.execute("UPDATE report_schedules SET last_period_end = %s, last_status = 'RUNNING', last_error = NULL, "
                    "last_run_at = UTC_TIMESTAMP() WHERE id = %s AND last_period_end <=> %s",
                    (period_end, sid, previous))
        return cur.rowcount == 1


def tick(now=None) -> int:
    """One scheduling pass; returns how many schedules were run. Never raises."""
    ran = 0
    try:
        with get_db_cursor(dictionary=True, commit=False) as (_, cur):
            cur.execute("SELECT * FROM report_schedules WHERE enabled = 1")
            rows = cur.fetchall()
    except Exception as exc:
        logger.error(f"[report-scheduler] could not read the schedules: {exc}")
        return 0
    now = now or datetime.now(timezone.utc)
    for sched in rows:
        try:
            period = schedules.due(sched, now, created_at=sched.get("created_at"), last_period_end=sched.get("last_period_end"))
            if period and _claim(sched["id"], period["period_end"], sched.get("last_period_end")):
                logger.info(f"[report-scheduler] running '{sched['name']}' for {period['period_start']} .. {period['period_end']}")
                execute(sched, period)
                ran += 1
        except Exception as exc:
            logger.error(f"[report-scheduler] schedule {sched.get('id')} errored: {exc}")
    return ran


def run_scheduler_loop(leader_event, interval_seconds: int = 60) -> None:
    """Leader-guarded daemon thread, same pattern as app/reports/worker.run_sweeper_loop."""
    while True:
        if not leader_event.is_set():
            logger.warning("[report-scheduler] leadership lost -- stopping")
            return
        tick()
        time.sleep(interval_seconds)
