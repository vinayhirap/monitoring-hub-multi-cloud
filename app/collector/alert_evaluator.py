# app/collector/alert_evaluator.py
"""
Production alert evaluator.

- Reads latest metric per resource from the `metrics` table (a last-value
  cache, upserted every standard-tier cycle — see metrics_writer.py; it is
  NOT a history table, so "was this sustained" can't be answered by
  querying it for a time range).
- Joins with thresholds per service type and account.
- Requires a breach to be RE-CONFIRMED for thresholds.evaluation_period
  minutes before it becomes a visible alert (alert_pending holds the
  candidate until then) — this is the "for N minutes" semantics the column
  was added for but that the evaluator never actually read.
- Requires the same number of consecutive healthy readings before
  resolving an active alert, to stop single-good-reading flapping.
- Touches last_seen_at on every cycle a still-open alert is re-confirmed
  (breach OR pending-recovery), so the API can tell "still breaching, just
  hasn't changed" apart from "haven't heard from this metric in hours" —
  without silently resolving the latter (see 008_revert_falsely_resolved_alerts.sql
  for why that was tried before and reverted).
- Publishes new alerts AND resolutions to Redis for real-time WebSocket push.

Standard tier runs every 5 minutes (app/collector/scheduler.py), so one
"cycle" below is ~5 minutes unless that scheduler interval changes.
"""
import json
import logging
import math
from datetime import datetime
from app.db import get_connection
from app.ws.publisher import publish_alert, publish_alert_resolved

logger = logging.getLogger(__name__)

# Must track app/collector/scheduler.py's STANDARD_INTERVAL. Kept as a
# separate constant (not imported) so this module has no import-time
# dependency on the scheduler; if you change STANDARD_INTERVAL, update
# this too.
CYCLE_MINUTES = 5


def compare(value, threshold, op):
    if threshold is None or value is None:
        return False
    try:
        v = float(value)
        t = float(threshold)
    except (TypeError, ValueError):
        return False
    ops = {
        ">":  v >  t,
        ">=": v >= t,
        "<":  v <  t,
        "<=": v <= t,
    }
    return ops.get(op, False)


def _required_cycles(evaluation_period_minutes):
    """
    thresholds.evaluation_period is in minutes (default 5 -- i.e. one
    cycle, which reproduces the old fire-immediately behavior for anyone
    who hasn't deliberately raised it). Round UP so a period of e.g. 12
    minutes still requires 3 full cycles, not 2 (never resolve/fire on
    LESS confirmation than configured).
    """
    period = evaluation_period_minutes or CYCLE_MINUTES
    return max(1, math.ceil(period / CYCLE_MINUTES))


def _auto_resolve_stale_alerts(cursor):
    """
    Auto-resolves alerts in exactly two SAFE cases, both meaning the thing
    being alerted on no longer exists at all -- not just "quiet":

      1. The account was removed/deactivated (unchanged from before).
      2. The specific resource has no matching row in `resources` at all
         -- i.e. not "hasn't reported recently", but literally doesn't
         exist in current discovery. This catches orphaned rows from
         before the VM/YACE migration (mistyped/legacy resource_ids that
         can never receive a fresh metric again because nothing writes
         for a resource_id discovery doesn't know about) without touching
         alerts for resources that are simply between metric readings.

    Deliberately does NOT resolve purely because metrics stopped flowing
    for a still-discovered, still-active resource (collector down,
    VictoriaMetrics outage, network blip). That was tried once already
    and reverted -- see db/migrations/008_revert_falsely_resolved_alerts.sql.
    "No data" for an existing resource is surfaced as staleness by the API
    (last_seen_at), not auto-resolved.
    """
    cursor.execute("""
        UPDATE alerts a
        JOIN resources r
            ON r.resource_id = a.resource_id
        LEFT JOIN aws_accounts aa
            ON aa.id = r.aws_account_id
           AND aa.status = 'active'
        SET a.status      = 'resolved',
            a.resolved_at = NOW(),
            a.last_seen_at = NOW()
        WHERE a.status = 'active'
          AND aa.id IS NULL
    """)
    resolved_ids = []
    account_removed = cursor.rowcount

    # Case 2: resource_id has no matching row in `resources` at all.
    cursor.execute("""
        SELECT a.id
        FROM alerts a
        LEFT JOIN resources r ON r.resource_id = a.resource_id
        WHERE a.status = 'active' AND r.id IS NULL
    """)
    orphaned_ids = [row["id"] for row in cursor.fetchall()]
    if orphaned_ids:
        fmt = ",".join(["%s"] * len(orphaned_ids))
        cursor.execute(f"""
            UPDATE alerts
            SET status = 'resolved', resolved_at = NOW(), last_seen_at = NOW()
            WHERE id IN ({fmt})
        """, orphaned_ids)

    total = account_removed + len(orphaned_ids)
    return total, account_removed, len(orphaned_ids)


def _touch_pending(cursor, resource_id, metric_name, severity, environment,
                    metric_value, threshold_value):
    """
    Upsert a breach candidate. Returns the row's breach_cycles AFTER this
    touch (so the caller can decide whether to promote it this cycle).
    """
    cursor.execute("""
        INSERT INTO alert_pending
            (resource_id, metric_name, severity, environment,
             first_breach_at, last_seen_at, breach_cycles,
             current_value, threshold_value)
        VALUES (%s, %s, %s, %s, NOW(), NOW(), 1, %s, %s)
        ON DUPLICATE KEY UPDATE
            last_seen_at    = NOW(),
            breach_cycles   = breach_cycles + 1,
            current_value   = VALUES(current_value),
            threshold_value = VALUES(threshold_value),
            -- escalate severity if this cycle is worse than before
            severity = IF(VALUES(severity) = 'CRITICAL', 'CRITICAL', severity)
    """, (resource_id, metric_name, severity, environment,
          metric_value, threshold_value))

    cursor.execute("""
        SELECT breach_cycles, severity, first_breach_at
        FROM alert_pending
        WHERE resource_id = %s AND metric_name = %s
    """, (resource_id, metric_name))
    return cursor.fetchone()


def _clear_pending(cursor, resource_id, metric_name):
    cursor.execute("""
        DELETE FROM alert_pending WHERE resource_id = %s AND metric_name = %s
    """, (resource_id, metric_name))


def evaluate_alerts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        _evaluate_alerts_body(conn, cursor)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cursor.close()
        conn.close()


def _evaluate_alerts_body(conn, cursor):
    """
    Body of evaluate_alerts(), split out so the connection/cursor
    acquired in evaluate_alerts() are guaranteed to be released via
    try/finally even if a MySQL deadlock (observed in production, Sep
    5 2026) or any other exception happens partway through -- this
    used to leak the connection every time that happened, which is
    what exhausted the pool and took the dashboard offline for hours.
    """
    stale_total, stale_accounts, stale_orphans = _auto_resolve_stale_alerts(cursor)
    conn.commit()
    if stale_total:
        logger.info(
            f"Auto-resolved {stale_total} stale alert(s) "
            f"({stale_accounts} account removed, {stale_orphans} orphaned resource_id)"
        )
        try:
            publish_alert_resolved(alert_id=None, account_id=None, bulk=True)
        except Exception as e:
            logger.warning(f"Bulk-resolve publish failed: {e}")

    # ── Fetch latest metric per resource+metric combo ─────────
    cursor.execute("""
        SELECT
            m.resource_id          AS db_resource_id,
            r.resource_id          AS aws_resource_id,
            r.resource_type,
            r.aws_account_id,
            r.tags,
            r.region,
            aa.account_name,
            aa.default_region,
            m.metric_name,
            m.metric_value,
            m.metric_timestamp,
            t.id                   AS threshold_id,
            t.warning_value,
            t.critical_value,
            t.comparison,
            t.evaluation_period
        FROM metrics m
        JOIN resources r
            ON r.id = m.resource_id
        JOIN aws_accounts aa
            ON aa.id = r.aws_account_id
           AND aa.status = 'active'
        JOIN metric_catalog mc
            ON mc.metric_name = m.metric_name
        JOIN thresholds t
            ON t.metric_id       = mc.id
           AND t.resource_type   = r.resource_type
           AND t.aws_account_id  = r.aws_account_id
           AND t.enabled         = 1
        JOIN (
            SELECT resource_id, metric_name, MAX(metric_timestamp) AS ts
            FROM metrics
            GROUP BY resource_id, metric_name
        ) latest
            ON latest.resource_id = m.resource_id
           AND latest.metric_name = m.metric_name
           AND latest.ts          = m.metric_timestamp
        WHERE m.metric_timestamp >= DATE_SUB(NOW(), INTERVAL 10 MINUTE)
    """)

    rows = cursor.fetchall()
    logger.info(f"Evaluating {len(rows)} metric readings")

    new_alerts     = 0
    resolved       = 0
    already_open   = 0
    pending_touched = 0
    pending_cleared = 0

    for row in rows:
        aws_resource_id = row["aws_resource_id"]
        metric_name     = row["metric_name"]
        metric_value    = row["metric_value"]
        aws_account_id  = row["aws_account_id"]
        required_cycles = _required_cycles(row["evaluation_period"])

        try:
            tags = json.loads(row["tags"] or "{}")
        except Exception:
            tags = {}
        environment = tags.get("environment", tags.get("Environment", "prod")).lower()

        is_critical = compare(metric_value, row["critical_value"], row["comparison"])
        is_warning  = compare(metric_value, row["warning_value"],  row["comparison"])
        is_breaching = is_critical or is_warning

        # ── Existing open alert for this resource+metric? ─────
        cursor.execute("""
            SELECT id, severity FROM alerts
            WHERE resource_id = %s AND metric_name = %s AND status = 'active'
            LIMIT 1
        """, (aws_resource_id, metric_name))
        existing = cursor.fetchone()

        if not is_breaching:
            # Healthy reading. A candidate that never sustained: drop it,
            # it was a transient blip, not a real breach.
            _clear_pending(cursor, aws_resource_id, metric_name)

            if existing:
                # Sustained-recovery: require `required_cycles` consecutive
                # healthy readings before actually resolving, so one good
                # datapoint doesn't flap an active alert closed and let it
                # reopen (as a brand-new alert row) a few minutes later.
                cursor.execute("""
                    UPDATE alerts
                    SET current_value  = %s,
                        last_seen_at   = NOW(),
                        healthy_streak = healthy_streak + 1
                    WHERE id = %s
                """, (metric_value, existing["id"]))

                cursor.execute("SELECT healthy_streak FROM alerts WHERE id = %s", (existing["id"],))
                streak = cursor.fetchone()["healthy_streak"]

                if streak >= required_cycles:
                    cursor.execute("""
                        UPDATE alerts
                        SET status = 'resolved', resolved_at = NOW()
                        WHERE id = %s
                    """, (existing["id"],))
                    resolved += 1
                    try:
                        publish_alert_resolved(alert_id=existing["id"], account_id=aws_account_id)
                    except Exception as e:
                        logger.warning(f"Resolve publish failed: {e}")
            continue

        # ── Breaching ───────────────────────────────────────────
        if is_critical:
            severity = "CRITICAL"
        elif environment in ("prod", "production"):
            severity = "WARNING"
        else:
            severity = "INFO"

        threshold_value = row["critical_value"] if is_critical else row["warning_value"]

        if existing:
            # Already a visible alert — keep it fresh every cycle (fixes
            # values/timestamps freezing for months while still breaching)
            # and reset the recovery streak since we're breaching again.
            update_fields = ["current_value = %s", "threshold = %s",
                              "last_seen_at = NOW()", "healthy_streak = 0"]
            params = [metric_value, threshold_value]
            if existing["severity"] != severity and severity == "CRITICAL":
                update_fields.append("severity = %s")
                params.append(severity)
                logger.debug(f"Escalated alert {existing['id']} to CRITICAL")
            params.append(existing["id"])
            cursor.execute(
                f"UPDATE alerts SET {', '.join(update_fields)} WHERE id = %s",
                params,
            )
            already_open += 1
            continue

        # No visible alert yet — this breach must sustain for
        # `required_cycles` before it becomes one.
        pending = _touch_pending(
            cursor, aws_resource_id, metric_name, severity, environment,
            metric_value, threshold_value,
        )
        pending_touched += 1

        if pending["breach_cycles"] < required_cycles:
            continue  # not sustained long enough yet — stays invisible

        # Sustained — promote to a real, visible alert.
        promoted_severity = pending["severity"]
        cursor.execute("""
            INSERT INTO alerts
                (resource_id, metric_name, severity,
                 environment, status, triggered_at, last_seen_at,
                 healthy_streak, current_value, threshold)
            VALUES (%s, %s, %s, %s, 'active', %s, NOW(), 0, %s, %s)
        """, (
            aws_resource_id, metric_name, promoted_severity, environment,
            pending["first_breach_at"], metric_value, threshold_value,
        ))
        new_alert_id = cursor.lastrowid
        new_alerts  += 1
        pending_cleared += 1
        _clear_pending(cursor, aws_resource_id, metric_name)

        try:
            publish_alert(
                alert_id     = new_alert_id,
                severity     = promoted_severity,
                metric       = metric_name,
                value        = metric_value,
                threshold    = threshold_value,
                account_id   = aws_account_id,
                account_name = row["account_name"],
                region       = row["region"] or row["default_region"],
            )
        except Exception as e:
            logger.warning(f"Alert publish failed: {e}")

    conn.commit()

    logger.info(
        f"Alert evaluation complete — "
        f"new: {new_alerts}, resolved: {resolved}, already open: {already_open}, "
        f"pending touched: {pending_touched}, pending promoted: {pending_cleared}"
    )
