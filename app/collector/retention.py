# app/collector/retention.py
"""
Data retention for tables that grew without bound (audit D3 / D9).

Already pruned elsewhere: metric_history (30 d), op_events (30 d), synthetic results (30 d), cloud_events (90 d).
This module adds the two that were not:

  prune_resolved_alerts()   RESOLVED alerts older than ALERT_RETENTION_DAYS. Active and acknowledged alerts are
                            never touched. incident_alerts rows cascade (FK). Reports can span ~400 days
                            (reports.py caps a custom range at 400), so the default keeps 400 days and the floor
                            is 90: a typo can't wipe recent history.
  prune_notification_log()  delivery log rows older than NOTIFICATION_LOG_RETENTION_DAYS (default 90).

Settings (environment, read on every run):
  ALERT_RETENTION_DAYS=400            0 disables alert pruning entirely; any value 1-89 is raised to 90
  NOTIFICATION_LOG_RETENTION_DAYS=90  0 disables

Deletes run in small batches with a pause between them so the alerts table (read by the Alerts page and the
evaluator) is never locked for long. A run does at most MAX_BATCHES batches; the next daily run continues.
audit_logs is deliberately NOT pruned: it is the compliance trail.
"""
import logging
import os
import time

from app.db import get_connection

logger = logging.getLogger(__name__)

DEFAULT_ALERT_RETENTION_DAYS = 400
MIN_ALERT_RETENTION_DAYS = 90
DEFAULT_NOTIFICATION_LOG_RETENTION_DAYS = 90
BATCH_SIZE = 1000
MAX_BATCHES = 50
PAUSE_SECONDS = 0.2


def _env_days(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"[retention] {name}={raw!r} is not an integer; using {default}")
        return default


def alert_retention_days() -> int:
    """0 = disabled; otherwise at least MIN_ALERT_RETENTION_DAYS."""
    days = _env_days("ALERT_RETENTION_DAYS", DEFAULT_ALERT_RETENTION_DAYS)
    if days <= 0:
        return 0
    return max(days, MIN_ALERT_RETENTION_DAYS)


def _delete_in_batches(sql: str, params: tuple, sleep=time.sleep) -> int:
    """Runs `sql` (which must end in LIMIT n) repeatedly, committing per batch. Returns rows deleted."""
    total = 0
    for _ in range(MAX_BATCHES):
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            n = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        total += n
        if n < BATCH_SIZE:
            break
        sleep(PAUSE_SECONDS)
    return total


def prune_resolved_alerts(sleep=time.sleep) -> int:
    days = alert_retention_days()
    if not days:
        return 0
    return _delete_in_batches(
        f"DELETE FROM alerts WHERE status = 'resolved' AND resolved_at IS NOT NULL "
        f"AND resolved_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s DAY) LIMIT {BATCH_SIZE}",
        (days,), sleep)


def prune_notification_log(sleep=time.sleep) -> int:
    days = _env_days("NOTIFICATION_LOG_RETENTION_DAYS", DEFAULT_NOTIFICATION_LOG_RETENTION_DAYS)
    if days <= 0:
        return 0
    return _delete_in_batches(
        f"DELETE FROM notification_log WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY) LIMIT {BATCH_SIZE}",
        (max(days, 7),), sleep)


ORPHAN_METRICS_HOURS_DEFAULT = 72      # must stay above the collector's own 48 h "resource is gone" rule (runner.STALE_RESOURCE_HOURS)
ORPHAN_METRICS_HOURS_MIN = 48


def orphan_metrics_hours() -> int:
    return max(_env_days("ORPHAN_METRICS_HOURS", ORPHAN_METRICS_HOURS_DEFAULT), ORPHAN_METRICS_HOURS_MIN)


def prune_orphaned_metric_rows(sleep=time.sleep) -> int:
    """Delete the cached last-value rows (`metrics`) of AWS resources that no longer exist.

    The collector stops polling a resource once discovery has not re-confirmed it for 48 h (or it is terminated), but nothing
    ever removed its last-value rows, so every deleted instance / volume / event bus kept "metrics" 24-30 days old forever.
    They are not used for anything (the evaluator ignores them, charts show no data) and only showed up as stale. The
    `resources` row itself is KEPT: old alerts and reports still read its name and type. History in metric_history is untouched.

    MySQL does not allow LIMIT on a multi-table DELETE, hence the derived-table form (also what avoids "can't specify target
    table"). Batched like the other pruning jobs."""
    hours = orphan_metrics_hours()
    return _delete_in_batches(
        f"""DELETE FROM metrics WHERE id IN (
                SELECT id FROM (
                    SELECT m.id FROM metrics m
                    JOIN resources r ON r.id = m.resource_id
                    JOIN aws_accounts a ON a.id = r.aws_account_id AND a.provider = 'aws'
                    WHERE r.instance_state = 'terminated'
                       OR r.last_seen_at < DATE_SUB(NOW(), INTERVAL %s HOUR)
                    LIMIT {BATCH_SIZE}
                ) doomed
            )""",
        (hours,), sleep)


def run_retention() -> dict:
    """Called from the daily (low-tier) scheduler block. Never raises."""
    out = {}
    for name, fn in (("alerts", prune_resolved_alerts), ("notification_log", prune_notification_log),
                     ("orphaned_metric_rows", prune_orphaned_metric_rows)):
        try:
            out[name] = fn()
        except Exception as exc:
            logger.warning(f"[retention] pruning {name} failed (non-fatal): {exc}")
            out[name] = -1
    if any(v > 0 for v in out.values()):
        logger.info(f"[retention] pruned {out}")
    return out
