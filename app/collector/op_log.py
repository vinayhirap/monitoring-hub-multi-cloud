# app/collector/op_log.py
"""
Structured operational event logging — roadmap phase 5 (2026-09-13).

log_event() is additive: it always calls the normal Python logger first
(so nothing about existing server-log behavior changes), then ALSO
writes a row to op_events (db/migrations/022_op_events_table.sql) for
the narrow set of event types worth being able to search/filter later
-- collector cycle failures, alert-evaluation errors, discovery
failures. It is not a replacement for the 71+ existing logger.warning/
logger.error call sites across the collector -- only the outer,
cycle-level failure points call this; call-site selection favors "would
this have helped find the 2026-08-26 RCA faster" over "log everything".

The DB write is wrapped in its own try/except and never raises -- a
failure to log an operational event must never be the thing that takes
down the operation being logged.
"""
import json
import logging
from app.db import get_connection

logger = logging.getLogger(__name__)


def log_event(event_type: str, message: str, severity: str = "ERROR",
              account_id: int = None, resource_id: str = None, detail: dict = None) -> None:
    log_fn = {"INFO": logger.info, "WARNING": logger.warning}.get(severity, logger.error)
    log_fn(f"[{event_type}] {message}" + (f" (account={account_id})" if account_id else ""))

    # AUDIT(b06): conn/cursor are now closed in `finally`. Previously a
    # failed INSERT (the exact moment this is most likely: DB trouble, or
    # an event_type > VARCHAR(80)) leaked the pooled connection, and this
    # runs on collector failure paths -- a failure storm could drain the
    # 10-connection pool and take logins down with it.
    conn = cur = None
    try:
        conn = get_connection(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO op_events
                (event_type, severity, aws_account_id, resource_id, message, detail)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            (event_type or "")[:80], (severity or "ERROR")[:10], account_id,
            resource_id[:512] if resource_id else None, (message or "")[:4000],
            json.dumps(detail, default=str) if detail else None,
        ))
        conn.commit()
    except Exception as e:
        # Deliberately swallowed -- see module docstring. Falls back to
        # the logger call above, which already happened.
        logger.warning(f"[op_log] failed to persist op_event (non-fatal): {e}")
    finally:
        for closer in (cur, conn):
            if closer is not None:
                try:
                    closer.close()
                except Exception:
                    pass


def prune_op_events(retain_days: int = 30) -> int:
    """Same retention pattern as metrics_writer.prune_metric_history() --
    called from the low tier alongside it."""
    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute("DELETE FROM op_events WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY)", (retain_days,))
        conn.commit()
        return cur.rowcount
    finally:
        cur.close(); conn.close()
