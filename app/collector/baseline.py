# app/collector/baseline.py
"""
Nightly baseline recompute for dynamic thresholds (roadmap phase 1,
2026-09-13). Reads app/collector's own `metric_history` table (already
being written by the direct-call collectors -- see
app/aws/collector_direct.py, app/providers/{azure,gcp}/metrics_collector.py)
and produces per-(resource, metric, hour-of-day, day-of-week) mean/stddev
buckets into `metric_baseline` (db/migrations/020_metric_baseline_dynamic_thresholds.sql).

This is intentionally a single SQL aggregation, not a Python loop over
rows -- MySQL's own AVG/STDDEV_SAMP do the numeric work, this module just
shapes the upsert. Runs against `metric_history`, so it needs enough
retention to see real weekly seasonality (Monday-morning batch jobs,
weekend traffic dips) -- see MIN_HISTORY_DAYS below and
metrics_writer.py's prune_metric_history() retain_days, which this
module's caller (scheduler.py) should keep set to at least that many
days or dynamic thresholds will never leave cold-start.

Cold start: a (resource, metric, hour, weekday) bucket with fewer than
MIN_SAMPLES_PER_BUCKET readings is not written at all -- alert_evaluator.py
falls back to the static threshold for that bucket until enough history
accumulates. There is no cost to this -- it's pure computation over data
already collected, no cloud API calls, no external service.
"""
import logging
from app.db import get_connection

logger = logging.getLogger(__name__)

# How far back to look when recomputing. Should not exceed
# metrics_writer.prune_metric_history()'s retain_days -- there is no
# point asking for 30 days of pattern if only 7 are actually kept.
LOOKBACK_DAYS = 30

# A hour/weekday bucket needs at least this many historical readings
# before we trust its mean/stddev enough to gate real alerts on it.
# At a 5-minute collection cycle, one single Tuesday-9am occurrence
# gives ~1 sample; this requires roughly 3+ occurrences of that same
# hour+weekday slot before it's used, so the first 2-3 weeks of a new
# resource are cold-start (static thresholds only) by design.
MIN_SAMPLES_PER_BUCKET = 3


def recompute_baselines() -> int:
    """
    Recomputes every (resource_id, metric_name, hour_of_day, day_of_week)
    bucket from metric_history and upserts into metric_baseline. Returns
    the number of buckets written. Safe to run repeatedly -- each run
    fully recomputes and overwrites existing bucket values (no
    incremental/streaming state to get out of sync).
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT
                r.resource_id,
                h.metric_name,
                HOUR(h.metric_timestamp)    AS hour_of_day,
                WEEKDAY(h.metric_timestamp) AS day_of_week,
                AVG(h.metric_value)         AS mean_value,
                COALESCE(STDDEV_SAMP(h.metric_value), 0) AS stddev_value,
                COUNT(*)                    AS sample_count
            FROM metric_history h
            JOIN resources r ON r.id = h.resource_id
            WHERE h.metric_timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND h.metric_value IS NOT NULL
            GROUP BY r.resource_id, h.metric_name, HOUR(h.metric_timestamp), WEEKDAY(h.metric_timestamp)
            HAVING COUNT(*) >= %s
        """, (LOOKBACK_DAYS, MIN_SAMPLES_PER_BUCKET))
        buckets = cursor.fetchall()

        written = 0
        for b in buckets:
            cursor.execute("""
                INSERT INTO metric_baseline
                    (resource_id, metric_name, hour_of_day, day_of_week,
                     mean_value, stddev_value, sample_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    mean_value   = VALUES(mean_value),
                    stddev_value = VALUES(stddev_value),
                    sample_count = VALUES(sample_count)
            """, (
                b["resource_id"], b["metric_name"], b["hour_of_day"], b["day_of_week"],
                b["mean_value"], b["stddev_value"], b["sample_count"],
            ))
            written += 1

        conn.commit()
        logger.info(f"[baseline] recomputed {written} bucket(s) from {LOOKBACK_DAYS}d of metric_history")
        return written
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
