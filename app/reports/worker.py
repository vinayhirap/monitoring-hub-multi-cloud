# app/reports/worker.py
"""
Background execution for report generation.

Why not Celery/RQ: this app has no existing task-queue broker wired up
(Redis here is used only for the WebSocket pubsub bus, see app/ws/) and
report generation is CPU-light + I/O-bound (one SQL query set + one S3
PUT) -- FastAPI's own BackgroundTasks (runs after the HTTP response is
sent, same process, same event loop's executor) is sufficient and adds
zero new infrastructure. If report volume ever grows enough to need
work distributed across multiple app instances, report_jobs' claim
columns (claimed_by/claimed_at) are already there to make that migration
straightforward.

Reliability without a broker:
  - Every job is a DB row (report_jobs) BEFORE the background task ever
    runs, with status=QUEUED -- if the process crashes between enqueue
    and execution, the job is still visible (as stuck QUEUED) rather
    than silently lost.
  - run_job() claims its own row (UPDATE ... WHERE status='QUEUED') so
    it's safe even if something calls it twice for the same job_id.
  - _sweep_stuck_jobs(), started as one leader-guarded background
    thread (same leader-election lock as the metrics collector, see
    app/collector/leader.py), requeues any job stuck in PROCESSING
    past a timeout (e.g. the worker that claimed it died mid-run) up
    to max_attempts, then marks it FAILED.
"""
import logging
import os
import socket
import time
import traceback
from datetime import datetime, timedelta, timezone

from app.audit import write_audit
from app.db import get_db_cursor
from app.reports.engine import gather_report_data, render_report_pdf
from app.reports import s3_client

logger = logging.getLogger(__name__)

_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_STUCK_TIMEOUT_MINUTES = int(os.getenv("REPORT_JOB_STUCK_TIMEOUT_MINUTES", "15"))
_RETENTION_DAYS = int(os.getenv("REPORT_RETENTION_DAYS", "365"))


def _claim(job_id: int) -> dict | None:
    with get_db_cursor(dictionary=True) as (_, cur):
        cur.execute(
            "UPDATE report_jobs SET status='PROCESSING', claimed_by=%s, claimed_at=NOW(), "
            "attempts = attempts + 1 WHERE id=%s AND status IN ('QUEUED','PROCESSING')",
            (_WORKER_ID, job_id),
        )
        if cur.rowcount == 0:
            return None
        cur.execute("SELECT * FROM report_jobs WHERE id=%s", (job_id,))
        return cur.fetchone()


def _mark_complete(job_id: int, meta: dict, scope_label: str) -> None:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=_RETENTION_DAYS)
    with get_db_cursor() as (_, cur):
        cur.execute(
            "SELECT report_type, scope_type, scope_id, account_id, period_start, period_end, requested_by "
            "FROM report_jobs WHERE id=%s", (job_id,),
        )
        job = cur.fetchone()
        cur.execute(
            """INSERT INTO reports
               (job_id, report_type, scope_type, scope_id, scope_label, account_id,
                period_start, period_end, s3_bucket, s3_key, s3_version_id, sha256,
                size_bytes, generated_by, expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (job_id, job[0], job[1], job[2], scope_label, job[3], job[4], job[5],
             meta["bucket"], meta["key"], meta.get("version_id"), meta["sha256"],
             meta["size_bytes"], job[6], expires_at),
        )
        cur.execute("UPDATE report_jobs SET status='COMPLETE' WHERE id=%s", (job_id,))


def _mark_failed(job_id: int, error: str, attempts: int, max_attempts: int) -> None:
    status = "QUEUED" if attempts < max_attempts else "FAILED"
    with get_db_cursor() as (_, cur):
        cur.execute(
            "UPDATE report_jobs SET status=%s, error_message=%s WHERE id=%s",
            (status, error[:4000], job_id),
        )


def run_job(job_id: int) -> None:
    """Entry point handed to FastAPI's BackgroundTasks. Never raises --
    all failures are recorded on the job row, not propagated (there's
    no HTTP caller left listening by the time this runs)."""
    job = _claim(job_id)
    if not job:
        logger.warning(f"report_jobs id={job_id}: could not claim (already running/finished elsewhere)")
        return
    try:
        data = gather_report_data(
            job["scope_type"], job["scope_id"], job["account_id"],
            job["period_start"], job["period_end"],
        )
        scope_label = (data.get("account") or {}).get("account_name") or job["scope_id"]
        pdf_bytes = render_report_pdf(
            report_type=job["report_type"], scope_type=job["scope_type"],
            scope_id=job["scope_id"], scope_label=scope_label,
            period_start=job["period_start"], period_end=job["period_end"],
            data=data, generated_by=job["requested_by"],
        )
        put_meta = s3_client.put_report(
            s3_client.build_key(
                job["scope_type"], job["scope_id"], job["report_type"],
                job["period_start"], job["period_end"],
                __import__("hashlib").sha256(pdf_bytes).hexdigest(),
            ),
            pdf_bytes,
        )
        put_meta["bucket"] = os.getenv("REPORTS_S3_BUCKET")
        put_meta["key"] = s3_client.build_key(
            job["scope_type"], job["scope_id"], job["report_type"],
            job["period_start"], job["period_end"], put_meta["sha256"],
        )
        _mark_complete(job_id, put_meta, scope_label)
        write_audit(job["requested_by"], "Report generated",
                    f"{job['report_type']} report for {job['scope_type']}={job['scope_id']}",
                    role=job.get("requested_by_role"))
        logger.info(f"report_jobs id={job_id}: complete -> {put_meta['key']}")
    except Exception as e:
        logger.error(f"report_jobs id={job_id}: failed: {e}\n{traceback.format_exc()}")
        _mark_failed(job_id, str(e), job["attempts"], job["max_attempts"])


def sweep_stuck_jobs() -> int:
    """Requeue/fail jobs stuck in PROCESSING past the timeout. Call
    periodically from a leader-guarded background thread."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=_STUCK_TIMEOUT_MINUTES)
    with get_db_cursor(dictionary=True) as (_, cur):
        cur.execute(
            "SELECT id, attempts, max_attempts FROM report_jobs "
            "WHERE status='PROCESSING' AND claimed_at < %s",
            (cutoff,),
        )
        stuck = cur.fetchall()
        for j in stuck:
            new_status = "QUEUED" if j["attempts"] < j["max_attempts"] else "FAILED"
            cur.execute(
                "UPDATE report_jobs SET status=%s, error_message='Requeued: worker timeout' WHERE id=%s",
                (new_status, j["id"]),
            )
    return len(stuck)


def run_sweeper_loop(leader_event, interval_seconds: int = 120) -> None:
    """Started as one leader-guarded daemon thread from app/main.py,
    mirroring the collector's own leader-election pattern so only one
    app instance runs this housekeeping."""
    while True:
        if not leader_event.is_set():
            logger.warning("[report-sweeper] leadership lost -- stopping")
            return
        try:
            n = sweep_stuck_jobs()
            if n:
                logger.warning(f"[report-sweeper] requeued/failed {n} stuck report job(s)")
        except Exception as e:
            logger.error(f"[report-sweeper] error: {e}")
        time.sleep(interval_seconds)
