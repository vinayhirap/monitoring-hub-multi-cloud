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
from app.alert_rules import (
    SYSTEM_METRICS, cadence_class_sql, eval_window_sql, hard_expiry_hours_sql,
)
from app.threshold_defaults import is_placeholder_threshold

# 2026-09-15 fix: this background evaluator resolves alerts directly via SQL
# (both the stale/stopped-instance sweep in _auto_resolve_stale_alerts() and
# the normal healthy-streak recovery path below) and, until this patch,
# never told either API-layer cache that anything changed. Every alerts.py
# mutation endpoint (ack/resolve/mute/bulk-ack) already calls BOTH of these
# together -- see alerts.py's own comment above _invalidate_cache() and
# live_data.py's docstring on invalidate_accounts_cache() for the exact
# "Overview banner says 3 WARNING, Alerts page says 0 Active" symptom this
# omission produces: live_data.py's _accounts_cache (up to 60s TTL) and
# alerts.py's own _alerts_cache/_counts_cache kept serving pre-resolve
# critical/warning counts after a bulk auto-resolve, because nothing here
# ever called the one function that clears them. Importing both here mirrors
# exactly what every API-triggered resolve path already does.
from app.api.live_data import invalidate_accounts_cache
from app.api.alerts import _invalidate_cache

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


# A baseline bucket needs at least this many post-clip samples (see
# app/collector/baseline.py's CLIP_SIGMA) before it's trusted at FULL
# weight against the static threshold. Below this, the dynamic band is
# blended with the static threshold in proportion to how much history
# the bucket actually has -- see the "CONFIDENCE" blending below. This
# smooths the cold-start transition: a bucket that just crossed
# baseline.py's MIN_SAMPLES_PER_BUCKET (3) used to jump straight from
# "100% static" to "100% dynamic" in one nightly recompute, which could
# visibly move an alert's effective threshold in a single step. Blending
# means that jump is gradual across the next several weeks of history
# instead of a one-time cliff.
CONFIDENT_SAMPLES = 20


def _baseline_bucket(cursor, aws_account_id, aws_resource_id, metric_name):
    """(mean, stddev, sample_count) for this resource+metric's CURRENT
    hour-of-day/day-of-week bucket, or None. Account-scoped (see migration
    046). HOUR()/WEEKDAY() are evaluated in UTC (UTC_TIMESTAMP) because
    baseline.py buckets CloudWatch datapoint timestamps, which are UTC; using
    NOW() here would silently look up the wrong slot on any DB whose session
    time zone is not UTC."""
    cursor.execute("""
        SELECT mean_value, stddev_value, sample_count
        FROM metric_baseline
        WHERE aws_account_id = %s AND resource_id = %s AND metric_name = %s
          AND hour_of_day = HOUR(UTC_TIMESTAMP()) AND day_of_week = WEEKDAY(UTC_TIMESTAMP())
    """, (aws_account_id, aws_resource_id, metric_name))
    row = cursor.fetchone()
    if not row:
        return None
    return (row["mean_value"], row["stddev_value"] or 0, row.get("sample_count") or 0)


def _dynamic_bounds(cursor, aws_account_id, aws_resource_id, metric_name, comparison, k,
                     static_warning=None, static_critical=None):
    """
    Looks up this resource+metric's current hour-of-day/day-of-week
    bucket in metric_baseline (populated by app/collector/baseline.py) and
    returns (warning, critical), or None if there is no usable bucket
    (cold start, or a flat-line metric with stddev 0 -- any deviation from a
    flat line would "breach", which is noise, not signal).

    dynamic critical = mean +/- k * stddev (">"/">=" metrics are bad when
    HIGH -> mean + k*stddev; "<"/"<=" metrics are bad when LOW -> mean -
    k*stddev). warning uses a tighter 0.66*k band.

    CONFIDENCE BLENDING: below CONFIDENT_SAMPLES the band is blended toward
    the static threshold by sample_count / CONFIDENT_SAMPLES, so a bucket
    that just crossed baseline.py's MIN_SAMPLES_PER_BUCKET does not jump from
    100% static to 100% dynamic in one step.

    NOTE: this returns the RAW band. Guard rails (clamp_dynamic_bounds) are
    applied by the caller so this function keeps its historical contract.
    """
    bucket = _baseline_bucket(cursor, aws_account_id, aws_resource_id, metric_name)
    if not bucket:
        return None
    mean, stddev, sample_count = bucket
    if stddev == 0:
        return None

    warn_k = k * 0.66
    if comparison in (">", ">="):
        dyn_warning, dyn_critical = mean + warn_k * stddev, mean + k * stddev
    else:  # "<", "<="
        dyn_warning, dyn_critical = mean - warn_k * stddev, mean - k * stddev

    if (static_warning is None or static_critical is None
            or sample_count >= CONFIDENT_SAMPLES):
        return (dyn_warning, dyn_critical)

    weight = max(0.0, min(1.0, sample_count / CONFIDENT_SAMPLES))
    return (weight * dyn_warning + (1 - weight) * float(static_warning),
            weight * dyn_critical + (1 - weight) * float(static_critical))


# GUARD RAILS for dynamic thresholds (2026-09-20 audit) --------------------
# mean +/- k*stddev is only meaningful relative to a metric's real-world
# limits, and it had none:
#   * a quiet resource (CPU 2% +/- 0.5) got a CRITICAL line at ~3.5%;
#   * a noisy percentage metric got a line above 100% and could never fire;
#   * auto_tune_static_thresholds() flips rows to dynamic automatically, so
#     a human never reviewed the resulting band.
# A dynamic band may therefore RELAX a static threshold by any amount (that is
# its whole purpose: stop chronic noise) but may TIGHTEN it by at most
# MAX_TIGHTEN_FACTOR, and percentage bands are capped just under 100.
MAX_TIGHTEN_FACTOR = 0.5   # ">" band may not fall below 50% of the static value
PERCENT_CAP = 99.9


def clamp_dynamic_bounds(dyn_warning, dyn_critical, static_warning, static_critical,
                          comparison, unit=None):
    """Guard-railed dynamic band -> (warning, critical).

    * WARNING may tighten to at most MAX_TIGHTEN_FACTOR of the static value
      (an "unusual for this resource" early signal) and relax without limit.
    * CRITICAL may only RELAX, never tighten: it is never lower than the static
      critical (never higher, for "<"). A statistically unusual reading that
      has not reached the configured critical line is a WARNING, not an
      outage. (Observed in prod on the first day: an EC2 memory alert went
      CRITICAL at 81.2% against a dynamic band of 81.03%, while the static
      critical was well above that.)
    * Percentage bands are capped at 99.9 so they can never become unreachable.
    """
    sw, sc = float(static_warning), float(static_critical)
    if comparison in (">", ">="):
        floor_w = sw * MAX_TIGHTEN_FACTOR
        w = max(dyn_warning, floor_w)
        c = max(dyn_critical, sc)
        if (unit or "").lower() == "percent" and sc <= 100:
            cap = PERCENT_CAP if sc <= PERCENT_CAP else sc
            w, c = min(w, cap), min(c, cap)
        if w > c:
            w = c
    else:
        # lower-is-worse: "tightening" means a HIGHER trip point
        ceil_w = sw / MAX_TIGHTEN_FACTOR if sw > 0 else sw
        w = min(dyn_warning, ceil_w)
        c = min(dyn_critical, sc)
        if c > w:
            c = w
    return w, c


# ANOMALY-ONLY mode (placeholder thresholds): see threshold_defaults.py
ANOMALY_MIN_RATIO = 1.5      # must also exceed 1.5x the bucket mean
ANOMALY_MIN_CYCLES = 3       # ~15 min sustained on the 5-min tier


def _anomaly_only_bound(cursor, aws_account_id, aws_resource_id, metric_name, k):
    """Upper anomaly line for a volume metric, or None when there is not
    enough evidence to alert at all (cold start / low confidence / flat)."""
    bucket = _baseline_bucket(cursor, aws_account_id, aws_resource_id, metric_name)
    if not bucket:
        return None
    mean, stddev, n = bucket
    if n < CONFIDENT_SAMPLES or stddev == 0:
        return None
    line = mean + max(k, 3.0) * stddev
    if mean > 0:
        line = max(line, mean * ANOMALY_MIN_RATIO)
    return line


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


OPEN_STATUSES = ("active", "acknowledged")

# An alert whose evaluation input has vanished (threshold disabled, metric
# de-selected) is only resolved once the evaluator has NOT confirmed it for
# this long -- protects against a Settings edit that briefly removes and
# re-adds a threshold row.
ORPHAN_GRACE_MINUTES = 30
RESOURCE_GONE_HOURS = 24
PENDING_EXPIRY_MINUTES = 30


def _resolve_ids(cursor, ids, reason):
    """Resolve alerts by id, recording WHY. Returns rows changed. Only ever
    touches still-open rows so a concurrent human action is never overwritten."""
    ids = list(ids)
    total = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        fmt = ",".join(["%s"] * len(chunk))
        cursor.execute(f"""
            UPDATE alerts
            SET status = 'resolved', resolved_at = UTC_TIMESTAMP(),
                last_seen_at = UTC_TIMESTAMP(),
                resolution_reason = %s, resolved_by = 'system'
            WHERE id IN ({fmt}) AND status IN ('active', 'acknowledged')
        """, (reason, *chunk))
        total += cursor.rowcount
    return total


def _auto_resolve_stale_alerts(cursor):
    """
    Auto-resolves an open (active OR acknowledged) alert ONLY when the thing
    it describes definitively no longer exists / can no longer be true.
    Every resolution records `resolution_reason` so it is auditable and
    distinguishable from a human click. Never "silence == healthy" (see
    db/migrations/008_revert_falsely_resolved_alerts.sql).

      account_inactive    the account was removed/deactivated
      resource_gone       no `resources` row at all, OR (AWS) discovery has
                          not seen the resource for RESOURCE_GONE_HOURS
      instance_stopped    EC2 in a definitive stopped/terminated state
                          (now ACCOUNT-scoped; it used to match any account's
                          same-id instance)
      threshold_disabled  no enabled threshold governs this metric any more
                          (disabled/deleted/de-selected) and the evaluator has
                          not confirmed the alert for ORPHAN_GRACE_MINUTES
      no_data_expired     stale (no reading) for the cadence-class hard expiry
                          (72h core/extended, 7d slow tier) -- not a live
                          signal any more, and it re-opens on its own if the
                          metric breaches again

    Returns (total, {reason: count}).
    """
    by_reason = {}

    def run(reason, sql, params=()):
        cursor.execute(sql, params)
        ids = [row["id"] for row in cursor.fetchall()]
        if ids:
            n = _resolve_ids(cursor, ids, reason)
            if n:
                by_reason[reason] = by_reason.get(reason, 0) + n

    open_in = "('active','acknowledged')"

    # account removed / deactivated
    run("account_inactive", f"""
        SELECT a.id FROM alerts a
        JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
        LEFT JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
        WHERE a.status IN {open_in} AND aa.id IS NULL
    """)

    # resource row is gone entirely (scoped to the alert's OWN account)
    run("resource_gone", f"""
        SELECT a.id FROM alerts a
        LEFT JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
        WHERE a.status IN {open_in} AND a.aws_account_id IS NOT NULL AND r.id IS NULL
    """)

    # AWS resource no longer reported by discovery for a long time
    run("resource_gone", f"""
        SELECT a.id FROM alerts a
        JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
        JOIN aws_accounts aa ON aa.id = a.aws_account_id AND aa.provider = 'aws'
        WHERE a.status IN {open_in}
          AND r.last_seen_at IS NOT NULL
          AND r.last_seen_at < DATE_SUB(NOW(), INTERVAL {RESOURCE_GONE_HOURS} HOUR)
    """)

    # EC2 definitively stopped/terminated -- ACCOUNT-scoped
    run("instance_stopped", f"""
        SELECT a.id FROM alerts a
        JOIN resources r ON r.resource_id = a.resource_id
                        AND r.aws_account_id = a.aws_account_id
                        AND r.resource_type = 'ec2'
                        AND r.instance_state IN ('stopped', 'terminated')
        WHERE a.status IN {open_in}
    """)

    # no enabled threshold governs this metric any more
    sys_in = ",".join(f"'{m}'" for m in SYSTEM_METRICS)
    run("threshold_disabled", f"""
        SELECT a.id FROM alerts a
        JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
        WHERE a.status IN {open_in}
          AND a.metric_name NOT IN ({sys_in})
          AND COALESCE(a.last_seen_at, a.triggered_at) < DATE_SUB(UTC_TIMESTAMP(), INTERVAL {ORPHAN_GRACE_MINUTES} MINUTE)
          AND NOT EXISTS (
              SELECT 1 FROM thresholds t
              JOIN metric_catalog mc ON mc.id = t.metric_id
              WHERE t.aws_account_id = a.aws_account_id
                AND t.resource_type  = r.resource_type
                AND t.enabled = 1
                AND mc.metric_name = a.metric_name
          )
    """)

    # stale beyond the hard expiry
    run("no_data_expired", f"""
        SELECT a.id FROM alerts a
        JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
        JOIN aws_accounts aa ON aa.id = a.aws_account_id
        WHERE a.status IN {open_in}
          AND COALESCE(a.last_seen_at, a.triggered_at)
              < DATE_SUB(UTC_TIMESTAMP(), INTERVAL {hard_expiry_hours_sql('r', 'aa')} HOUR)
    """)

    # unattributable legacy rows (aws_account_id NULL) can never appear in any
    # view; let them expire instead of lingering as ghosts
    run("no_data_expired", f"""
        SELECT a.id FROM alerts a
        WHERE a.status IN {open_in} AND a.aws_account_id IS NULL
          AND COALESCE(a.last_seen_at, a.triggered_at) < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 72 HOUR)
    """)

    # pending breach candidates that were never re-confirmed must not be
    # able to combine with a LATER, unrelated blip into a "sustained" breach
    cursor.execute(
        "DELETE FROM alert_pending WHERE last_seen_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s MINUTE)",
        (PENDING_EXPIRY_MINUTES,),
    )

    total = sum(by_reason.values())
    return total, by_reason


def _touch_pending(cursor, aws_account_id, resource_id, metric_name, severity, environment,
                    metric_value, threshold_value):
    """Upsert a breach candidate; returns the row's state AFTER the touch.
    aws_account_id is REQUIRED (see migration 046)."""
    cursor.execute("""
        INSERT INTO alert_pending
            (aws_account_id, resource_id, metric_name, severity, environment,
             first_breach_at, last_seen_at, breach_cycles,
             current_value, threshold_value)
        VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(), UTC_TIMESTAMP(), 1, %s, %s)
        ON DUPLICATE KEY UPDATE
            last_seen_at    = UTC_TIMESTAMP(),
            breach_cycles   = breach_cycles + 1,
            current_value   = VALUES(current_value),
            threshold_value = VALUES(threshold_value),
            severity = IF(VALUES(severity) = 'CRITICAL', 'CRITICAL', severity)
    """, (aws_account_id, resource_id, metric_name, severity, environment,
          metric_value, threshold_value))

    cursor.execute("""
        SELECT breach_cycles, severity, first_breach_at
        FROM alert_pending
        WHERE aws_account_id = %s AND resource_id = %s AND metric_name = %s
    """, (aws_account_id, resource_id, metric_name))
    return cursor.fetchone()


def _clear_pending(cursor, aws_account_id, resource_id, metric_name):
    cursor.execute("""
        DELETE FROM alert_pending
        WHERE aws_account_id = %s AND resource_id = %s AND metric_name = %s
    """, (aws_account_id, resource_id, metric_name))


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


def _silenced_now(cursor):
    """{(account_id, resource_id): reason} for active maintenance windows.
    Never lets a maintenance-lookup failure stop alert evaluation."""
    try:
        from app.collector.maintenance import active_silenced_map
        return active_silenced_map(cursor)
    except Exception as e:
        logger.warning(f"maintenance lookup failed (alerts evaluated un-silenced): {e}")
        return {}


def _evaluate_alerts_body(conn, cursor):
    """
    Body of evaluate_alerts(), split out so the connection/cursor acquired in
    evaluate_alerts() are released via try/finally even if a MySQL deadlock
    (observed in production, Sep 5 2026) or any other exception happens
    partway through.
    """
    stale_total, stale_by_reason = _auto_resolve_stale_alerts(cursor)
    conn.commit()
    if stale_total:
        logger.info(f"Auto-resolved {stale_total} alert(s): "
                    + ", ".join(f"{n} {r}" for r, n in sorted(stale_by_reason.items())))
        invalidate_accounts_cache()
        _invalidate_cache()
        try:
            publish_alert_resolved(alert_id=None, account_id=None, bulk=True)
        except Exception as e:
            logger.warning(f"Bulk-resolve publish failed: {e}")

    silenced_map = _silenced_now(cursor)

    # ── Latest metric per resource+metric ─────────────────────
    # `metrics` is a last-value cache with UNIQUE (resource_id, metric_name)
    # (migration 004), so no "latest per pair" self-join is needed.
    # The freshness window is per COLLECTION CADENCE (alert_rules.py): the
    # old flat 10 minutes could never see hourly/daily extended-tier metrics.
    cursor.execute(f"""
        SELECT
            m.resource_id          AS db_resource_id,
            r.resource_id          AS aws_resource_id,
            r.resource_type,
            r.aws_account_id,
            r.tags,
            r.region,
            aa.account_name,
            aa.default_region,
            {cadence_class_sql('r', 'aa')} AS cadence,
            mc.unit                AS unit,
            m.metric_name,
            m.metric_value,
            m.metric_timestamp,
            t.id                   AS threshold_id,
            t.warning_value,
            t.critical_value,
            t.comparison,
            t.evaluation_period,
            t.use_dynamic,
            t.dynamic_k
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
        WHERE m.metric_timestamp >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL {eval_window_sql('r', 'aa')} MINUTE)
    """)

    rows = cursor.fetchall()
    logger.info(f"Evaluating {len(rows)} metric readings")

    new_alerts      = 0
    resolved        = 0
    already_open    = 0
    pending_touched = 0
    reopened        = 0

    for row in rows:
        aws_resource_id = row["aws_resource_id"]
        metric_name     = row["metric_name"]
        metric_value    = row["metric_value"]
        aws_account_id  = row["aws_account_id"]
        cadence         = row["cadence"]
        # Hourly/daily metrics are one reading per collection; requiring N
        # 5-minute "cycles" of the SAME reading would be meaningless.
        required_cycles = _required_cycles(row["evaluation_period"]) if cadence == "core" else 1

        try:
            tags = json.loads(row["tags"] or "{}")
        except Exception:
            tags = {}
        environment = tags.get("environment", tags.get("Environment", "prod")).lower()

        warning_value, critical_value = row["warning_value"], row["critical_value"]
        comparison = row["comparison"]
        anomaly_only = is_placeholder_threshold(warning_value, critical_value, comparison)
        severity_cap = None

        if anomaly_only:
            # Placeholder default (see threshold_defaults.PLACEHOLDER_THRESHOLD):
            # volume is not failure. Alert only on a sustained, confident,
            # statistically extreme reading vs. this resource's OWN baseline,
            # never above WARNING.
            line = _anomaly_only_bound(cursor, aws_account_id, aws_resource_id,
                                       metric_name, row["dynamic_k"] or 3.0)
            required_cycles = max(required_cycles, ANOMALY_MIN_CYCLES) if cadence == "core" else 1
            severity_cap = "WARNING"
            if line is None:
                warning_value = critical_value = None
            else:
                warning_value = critical_value = line
        elif row.get("use_dynamic"):
            dynamic = _dynamic_bounds(
                cursor, aws_account_id, aws_resource_id, metric_name,
                comparison, row["dynamic_k"] or 3.0,
                static_warning=warning_value, static_critical=critical_value,
            )
            if dynamic is not None:
                warning_value, critical_value = clamp_dynamic_bounds(
                    dynamic[0], dynamic[1], row["warning_value"], row["critical_value"],
                    comparison, row.get("unit"))

        is_critical = compare(metric_value, critical_value, comparison) and severity_cap is None
        is_warning  = compare(metric_value, warning_value,  comparison)
        is_breaching = is_critical or is_warning

        cursor.execute("""
            SELECT id, severity, status FROM alerts
            WHERE aws_account_id = %s AND resource_id = %s AND metric_name = %s
              AND status IN ('active', 'acknowledged')
            ORDER BY id DESC LIMIT 1
        """, (aws_account_id, aws_resource_id, metric_name))
        existing = cursor.fetchone()

        if not is_breaching:
            _clear_pending(cursor, aws_account_id, aws_resource_id, metric_name)

            if existing:
                # threshold shown on the alert tracks the value actually in
                # force (static or dynamic/anomaly), matched to its severity.
                # In anomaly-only mode with no usable baseline there is no
                # line; keep the previous displayed threshold.
                resolve_threshold_value = (
                    critical_value if existing["severity"] == "CRITICAL" else warning_value
                )
                if resolve_threshold_value is None:
                    cursor.execute("""
                        UPDATE alerts SET current_value = %s, last_seen_at = UTC_TIMESTAMP(),
                                          healthy_streak = healthy_streak + 1
                        WHERE id = %s
                    """, (metric_value, existing["id"]))
                else:
                    cursor.execute("""
                        UPDATE alerts
                        SET current_value  = %s,
                            threshold      = %s,
                            last_seen_at   = UTC_TIMESTAMP(),
                            healthy_streak = healthy_streak + 1
                        WHERE id = %s
                    """, (metric_value, resolve_threshold_value, existing["id"]))

                cursor.execute("SELECT healthy_streak FROM alerts WHERE id = %s", (existing["id"],))
                streak = cursor.fetchone()["healthy_streak"]

                # `existing` may be ACKNOWLEDGED: those used to be skipped by
                # the evaluator entirely, so they never resolved on recovery.
                if streak >= required_cycles:
                    cursor.execute("""
                        UPDATE alerts
                        SET status = 'resolved', resolved_at = UTC_TIMESTAMP(),
                            resolution_reason = %s, resolved_by = 'system'
                        WHERE id = %s AND status IN ('active', 'acknowledged')
                    """, ("placeholder_threshold" if anomaly_only and warning_value is None else "recovered",
                          existing["id"]))
                    resolved += cursor.rowcount
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

        threshold_value = critical_value if is_critical else warning_value

        if existing:
            update_fields = ["current_value = %s", "threshold = %s",
                              "last_seen_at = UTC_TIMESTAMP()", "healthy_streak = 0"]
            params = [metric_value, threshold_value]
            if existing["severity"] == "CRITICAL" and severity != "CRITICAL":
                # DE-ESCALATION (2026-09-21). An open CRITICAL whose reading no
                # longer reaches the critical line -- volume anomalies (never
                # CRITICAL), or an alert raised by the old evaluator against a
                # tight dynamic band -- is brought down to what it is now
                # instead of staying CRITICAL until it fully recovers.
                update_fields.append("severity = %s")
                params.append(severity)
                logger.debug(f"De-escalated alert {existing['id']} to {severity}")
            elif existing["severity"] != severity and severity == "CRITICAL":
                update_fields.append("severity = %s")
                params.append(severity)
                if existing["status"] == "acknowledged":
                    # It got WORSE after a human acknowledged the milder
                    # state: the acknowledgement no longer covers it.
                    update_fields += ["status = 'active'", "acked = 0",
                                      "acked_by = NULL", "acked_at = NULL"]
                    reopened += 1
                logger.debug(f"Escalated alert {existing['id']} to CRITICAL")
            params.append(existing["id"])
            cursor.execute(f"UPDATE alerts SET {', '.join(update_fields)} WHERE id = %s", params)
            already_open += 1
            continue

        pending = _touch_pending(
            cursor, aws_account_id, aws_resource_id, metric_name, severity, environment,
            metric_value, threshold_value,
        )
        pending_touched += 1

        if pending["breach_cycles"] < required_cycles:
            continue

        promoted_severity = pending["severity"]
        if severity_cap == "WARNING" and promoted_severity == "CRITICAL":
            promoted_severity = "WARNING"
        group_key = f"{aws_account_id}:{row['resource_type']}:{metric_name}"
        silence_reason = silenced_map.get((aws_account_id, aws_resource_id))
        cursor.execute("""
            INSERT INTO alerts
                (aws_account_id, resource_id, metric_name, severity,
                 environment, group_key, status, triggered_at, last_seen_at,
                 healthy_streak, current_value, threshold, silenced, silenced_reason)
            VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, UTC_TIMESTAMP(), 0, %s, %s, %s, %s)
        """, (
            aws_account_id, aws_resource_id, metric_name, promoted_severity, environment,
            group_key, pending["first_breach_at"], metric_value, threshold_value,
            1 if silence_reason else 0, silence_reason,
        ))
        new_alert_id = cursor.lastrowid
        new_alerts  += 1
        _clear_pending(cursor, aws_account_id, aws_resource_id, metric_name)

        if silence_reason:
            continue   # born silenced by a maintenance window: no toast/sound/page
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

    if resolved or reopened:
        invalidate_accounts_cache()
        _invalidate_cache()

    logger.info(
        f"Alert evaluation complete — "
        f"new: {new_alerts}, resolved: {resolved}, already open: {already_open}, "
        f"reopened after escalation: {reopened}, pending touched: {pending_touched}"
    )
