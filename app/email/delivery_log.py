# app/email/delivery_log.py
"""
One row per email handed to SMTP, from every feature (alerts, reports, escalation, tests, welcome / reset mails).

OFF unless EMAIL_CENTER_ENABLED=true: nothing is written and no table is touched otherwise, so this cannot change
behaviour on a box that has not applied migration 135. record() never raises and never blocks a send: a log that cannot
be written is a warning, not a failed alert.
"""
import logging
import os

logger = logging.getLogger(__name__)

STATUSES = ("sent", "failed", "skipped")
_MAX = {"category": 30, "to_addr": 254, "subject": 255, "error": 300}


def enabled() -> bool:
    return os.getenv("EMAIL_CENTER_ENABLED", "false").strip().lower() in ("true", "1", "yes")


def _clean(value, field):
    if value is None:
        return None
    text = " ".join(str(value).split())          # no CR/LF, no runs of whitespace
    return text[:_MAX[field]] or None


def record(category, to_addr, subject, status, error=None, report_id=None, schedule_id=None) -> bool:
    """True when a row was written. Never raises."""
    if not enabled() or status not in STATUSES:
        return False
    try:
        from app.db import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO email_delivery_log (category, to_addr, subject, status, error, report_id, schedule_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (_clean(category, "category") or "system", _clean(to_addr, "to_addr") or "(none)",
                 _clean(subject, "subject"), status, _clean(error, "error"), report_id, schedule_id))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        logger.warning(f"[email-log] could not record a delivery (non-fatal): {type(exc).__name__}: {str(exc)[:160]}")
        return False
