# app/collector/baseline.py
"""
Nightly baseline recompute for dynamic thresholds (roadmap phase 1,
2026-09-13; hardened 2026-09-14 with outlier-robust stats -- see
"ROBUSTNESS" below). Reads app/collector's own `metric_history` table
(already being written by the direct-call collectors -- see
app/aws/collector_direct.py, app/providers/{azure,gcp}/metrics_collector.py)
and produces per-(resource, metric, hour-of-day, day-of-week) mean/stddev
buckets into `metric_baseline` (db/migrations/020_metric_baseline_dynamic_thresholds.sql).

This module is provider-agnostic by construction -- it groups purely on
`resources.resource_id` + `metric_history.metric_name`, both populated
identically for AWS/Azure/GCP rows (see collector_direct.py's Phase 1 GMD
collector and both providers/{azure,gcp}/metrics_collector.py's Phase 2/3
collectors, all of which write through the same write_metric_history_batch()).
No provider-specific branching needed here or in alert_evaluator.py's
consumption of this table -- every resource/metric this app tracks, on
any cloud, gets a baseline the same way.

This is intentionally SQL aggregation, not a Python loop over rows --
MySQL's own AVG/STDDEV_SAMP do the numeric work, this module just shapes
the upsert. Runs against `metric_history`, so it needs enough retention
to see real weekly seasonality (Monday-morning batch jobs, weekend
traffic dips) -- see LOOKBACK_DAYS below and metrics_writer.py's
prune_metric_history() retain_days, which this module's caller
(scheduler.py) keeps set to at least that many days or dynamic
thresholds will never leave cold-start.

Cold start: a (resource, metric, hour, weekday) bucket with fewer than
MIN_SAMPLES_PER_BUCKET post-clip readings is not written at all --
alert_evaluator.py falls back to the static threshold for that bucket
until enough history accumulates. There is no cost to this -- it's pure
computation over data already collected, no cloud API calls, no
external service.

ROBUSTNESS (2026-09-14 change)
-------------------------------
The original single-pass AVG/STDDEV_SAMP had a real failure mode: if a
past incident (e.g. a 20-minute CPU spike from a bad deploy) falls
inside a given hour/weekday bucket's 30-day lookback window, that spike
inflates the bucket's own mean and stddev -- so the model "learns" that
spikes are normal, and the *next* real incident in that same slot may
no longer cross mean+k*stddev at all. This is the standard weakness of
plain mean/stddev on data that isn't actually clean Gaussian noise.

Fix: two-pass sigma-clipping, a standard robust-statistics technique
(used in astronomy/signal-processing for exactly this "one outlier
skews the whole distribution" problem) --
  Pass 1: compute a raw mean/stddev per bucket, same as before.
  Pass 2: recompute mean/stddev over only the readings within
          CLIP_SIGMA standard deviations of the PASS-1 mean, discarding
          the rest as likely incident/outlier noise rather than normal
          variation.
A flat-line bucket (pass-1 stddev == 0) skips clipping entirely --
there's nothing to clip relative to, and clipping there would just
divide by zero / discard everything.

This is still one SQL round-trip (the pass-1 aggregate is a derived
table joined back in, not a second query from Python), so it costs
nothing extra in cloud API calls or app complexity -- same "pure
computation over already-collected data" cost profile as before.

CONFIDENCE (sample_count)
--------------------------
`sample_count` written to metric_baseline is now the POST-CLIP count
(how many readings actually shaped the final mean/stddev), not the raw
count -- alert_evaluator.py's _dynamic_bounds() uses this to blend
between the dynamic band and the static threshold when a bucket is
short on history, rather than snapping straight from "static" to "100%
dynamic" the moment MIN_SAMPLES_PER_BUCKET is crossed. See
alert_evaluator.py's CONFIDENT_SAMPLES for where that blend lives.
"""
import logging
from app.db import get_connection

logger = logging.getLogger(__name__)

# How far back to look when recomputing. Should not exceed
# metrics_writer.prune_metric_history()'s retain_days -- there is no
# point asking for 30 days of pattern if only 7 are actually kept.
LOOKBACK_DAYS = 30

# A hour/weekday bucket needs at least this many historical readings
# (POST sigma-clip -- see module docstring) before we write it at all.
# At a 5-minute collection cycle, one single Tuesday-9am occurrence
# gives ~1 sample; this requires roughly 3+ occurrences of that same
# hour+weekday slot before it's used, so the first 2-3 weeks of a new
# resource are cold-start (static thresholds only) by design.
MIN_SAMPLES_PER_BUCKET = 3

# How many standard deviations (from the pass-1 mean) a reading may
# fall before pass 2 treats it as an outlier and excludes it from the
# final mean/stddev. 3.0 is the conventional "extreme outlier, not
# normal variance" cutoff for roughly-bell-shaped metrics -- tight
# enough to actually exclude a real incident spike, loose enough not to
# quietly trim a metric that's just naturally spiky (e.g. queue depth).
CLIP_SIGMA = 3.0


def recompute_baselines() -> int:
    """
    Recomputes every (resource_id, metric_name, hour_of_day, day_of_week)
    bucket from metric_history and upserts into metric_baseline, using
    sigma-clipped mean/stddev (see module docstring's "ROBUSTNESS"
    section) so a single past incident doesn't permanently widen a
    bucket's normal band. Returns the number of buckets written. Safe to
    run repeatedly -- each run fully recomputes and overwrites existing
    bucket values (no incremental/streaming state to get out of sync).
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT
                r.aws_account_id,
                r.resource_id,
                h.metric_name,
                HOUR(h.metric_timestamp)    AS hour_of_day,
                WEEKDAY(h.metric_timestamp) AS day_of_week,
                AVG(
                    CASE WHEN p1.stddev1 = 0
                              OR ABS(h.metric_value - p1.mean1) <= %s * p1.stddev1
                         THEN h.metric_value END
                )                           AS mean_value,
                COALESCE(STDDEV_SAMP(
                    CASE WHEN p1.stddev1 = 0
                              OR ABS(h.metric_value - p1.mean1) <= %s * p1.stddev1
                         THEN h.metric_value END
                ), 0)                       AS stddev_value,
                SUM(
                    CASE WHEN p1.stddev1 = 0
                              OR ABS(h.metric_value - p1.mean1) <= %s * p1.stddev1
                         THEN 1 ELSE 0 END
                )                           AS sample_count,
                COUNT(*)                    AS raw_sample_count
            FROM metric_history h
            JOIN resources r ON r.id = h.resource_id
            JOIN (
                -- Pass 1: raw (unclipped) mean/stddev per bucket, used
                -- only to find pass 2's clip boundary -- never written
                -- to metric_baseline itself.
                SELECT
                    h2.resource_id,
                    h2.metric_name,
                    HOUR(h2.metric_timestamp)    AS hour_of_day,
                    WEEKDAY(h2.metric_timestamp) AS day_of_week,
                    AVG(h2.metric_value)         AS mean1,
                    COALESCE(STDDEV_SAMP(h2.metric_value), 0) AS stddev1
                FROM metric_history h2
                WHERE h2.metric_timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY)
                  AND h2.metric_value IS NOT NULL
                GROUP BY h2.resource_id, h2.metric_name,
                         HOUR(h2.metric_timestamp), WEEKDAY(h2.metric_timestamp)
            ) p1
                ON p1.resource_id  = h.resource_id
               AND p1.metric_name = h.metric_name
               AND p1.hour_of_day = HOUR(h.metric_timestamp)
               AND p1.day_of_week = WEEKDAY(h.metric_timestamp)
            WHERE h.metric_timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND h.metric_value IS NOT NULL
            -- Group by r.id (the true per-resource identity), NOT
            -- r.resource_id (the raw AWS name string) -- two different
            -- accounts' resources rows CAN share the same resource_id
            -- string (e.g. "System", a stock CloudWatch Logs group
            -- name; see the 2026-09-16 AuroGov Mumbai/U4RAD incident),
            -- and grouping by the string alone would silently average
            -- two unrelated accounts' metric history into one bucket.
            -- r.resource_id/r.aws_account_id in the SELECT list are
            -- functionally dependent on r.id, so this is valid under
            -- ONLY_FULL_GROUP_BY without needing to list them here too.
            GROUP BY r.id, h.metric_name,
                     HOUR(h.metric_timestamp), WEEKDAY(h.metric_timestamp)
            HAVING sample_count >= %s
        """, (
            CLIP_SIGMA, CLIP_SIGMA, CLIP_SIGMA,   # the three CASE clips (pass 2)
            LOOKBACK_DAYS,                         # pass-1 subquery window
            LOOKBACK_DAYS,                         # pass-2 outer window
            MIN_SAMPLES_PER_BUCKET,                # HAVING gate (post-clip count)
        ))
        buckets = cursor.fetchall()

        written = 0
        clipped_buckets = 0
        for b in buckets:
            if b["sample_count"] < b["raw_sample_count"]:
                clipped_buckets += 1
            cursor.execute("""
                INSERT INTO metric_baseline
                    (aws_account_id, resource_id, metric_name, hour_of_day, day_of_week,
                     mean_value, stddev_value, sample_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    mean_value   = VALUES(mean_value),
                    stddev_value = VALUES(stddev_value),
                    sample_count = VALUES(sample_count)
            """, (
                b["aws_account_id"], b["resource_id"], b["metric_name"], b["hour_of_day"], b["day_of_week"],
                b["mean_value"], b["stddev_value"], b["sample_count"],
            ))
            written += 1

        conn.commit()
        logger.info(
            f"[baseline] recomputed {written} bucket(s) from {LOOKBACK_DAYS}d of "
            f"metric_history ({clipped_buckets} had outlier readings clipped at "
            f"{CLIP_SIGMA}sigma)"
        )
        return written
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
