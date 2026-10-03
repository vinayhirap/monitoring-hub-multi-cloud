# app/collector/multivariate_anomaly.py
"""
Multivariate anomaly detection -- AIOps roadmap Phase 2 (2026-09-14).

Everything in Phase 1 (baseline.py's sigma-clipped bands,
alert_evaluator.py's dynamic thresholds) scores ONE metric at a time
against ITS OWN history. That structurally cannot catch the case where
several metrics shift together by an amount that's unremarkable
per-metric but is a real anomaly as a PATTERN -- e.g. CPU +8%, network
+12%, disk I/O +15% all at once, none individually crossing a
threshold, but that combination never having happened together before.
This is exactly the gap scikit-learn's IsolationForest is built for:
an unsupervised model trained per-resource on its own multi-metric
history, scoring how "isolated" (unusual) the current combination of
readings is relative to everything that resource has done before.

FREE / LOCAL COMPUTE ONLY -- scikit-learn + pandas, runs on the
existing collector CPU, no cloud API calls, no paid service. See
AI_ML_ROADMAP.md Section 7's cost table: this is the "LOCAL COMPUTE"
tier, one step up from the pure-SQL Phase 1 work.

DESIGN CHOICE -- reuses the existing `alerts` table/pipeline instead of
a new one: writing a detected anomaly as an alert row
(metric_name='multivariate_anomaly') means it automatically flows
through everything already built -- topology-based correlation
(correlate.py), health scoring (health_score.py), probable-root-cause
ranking (rca.py -- an anomaly caught this way ranks in an incident
exactly like one caught by a plain threshold breach), and escalation --
with zero new UI/API surface needed. A dedicated table would have
needed all of that re-plumbed a second time for no real benefit.

CADENCE: "low" tier (15 min, see scheduler.py) -- retrains per resource
each cycle rather than persisting a model between cycles. Model fit at
this data volume (tens of metrics x thousands of points) is
sub-second; the complexity of a persisted-model cache isn't earned
yet at the resource counts this app currently manages.

ACCURACY NOTE (see AI_ML_ROADMAP.md's own caution): CONTAMINATION below
is a starting assumption (~2% of historical readings were "unusual"),
not yet tuned against real confirmed-incident history -- there isn't
enough of that history yet (the `incidents` table this same roadmap
phase 1 introduced is exactly what would let this be backtested and
tuned properly later, per the roadmap's own stated sequencing).

PHASE 2 AI/ML AUDIT (2026-10-02) -- precision work. Anomaly alerts here are
hidden from the Alerts UI (app/alert_visibility.py) and never seed incidents or
lower health scores, so today they are shadow output; the goal of this pass is
to make them trustworthy enough to be judged (scripts/anomaly_shadow_report.py)
before anyone decides to surface them. Changes, all deterministic and free:
  1. Sustained, not instantaneous: the model is trained on history EXCLUDING the
     last MIN_CONSECUTIVE_BUCKETS 5-minute buckets, and ALL of them must score as
     outliers. Stateless (no new table) and removes single-reading blips. Before
     this, one anomalous bucket raised an alert -- and CONTAMINATION=0.02 means
     ~2% of perfectly normal readings score as outliers by construction.
  2. Time-of-day features (sin/cos of the hour) so a normal daily peak is not an
     "anomaly" and a peak at 3 a.m. can be.
  3. Explainability + materiality gate: a robust z-score per metric (median vs the
     larger of MAD/std) says WHICH metrics moved and by how many sigmas. At least
     MIN_MOVED_METRICS (2) metrics must have moved by MIN_ROBUST_Z or more --
     one metric on its own is what baseline.py already covers, and an outlier where
     nothing moved materially is noise in a dense cluster. (Motivated by PROD
     2026-10-02: two ALB anomaly alerts at score ~-0.09, 11 mostly-zero metrics.)
  4. The cycle's log line reports raw/sustained/confirmed counts and elapsed
     seconds, so tuning and CPU cost are measurable from journalctl alone.
Tunable without a code change: ANOMALY_CONTAMINATION, ANOMALY_MIN_CONSECUTIVE_BUCKETS,
ANOMALY_MIN_ROBUST_Z, ANOMALY_MIN_MOVED_METRICS.
"""
import json
import logging
import os
import time

from app.db import get_connection

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 14
# Below this many distinct metrics, there's no real "combination" to
# score -- 1-2 metrics is exactly what baseline.py's per-metric sigma
# bands already cover; this module only adds value once there's a
# genuine multivariate pattern to look for.
MIN_METRICS_PER_RESOURCE = 3
MIN_SAMPLES_FOR_TRAINING = 50
# Was a hard-coded 0.02 (2% of normal readings flagged by construction); halved now that
# sustained-ness and the materiality gate do most of the filtering. Env-tunable.
CONTAMINATION = float(os.getenv("ANOMALY_CONTAMINATION", "0.01"))
# All of the last N 5-minute buckets must be outliers (N*5 minutes of sustained deviation).
MIN_CONSECUTIVE_BUCKETS = max(1, int(os.getenv("ANOMALY_MIN_CONSECUTIVE_BUCKETS", "3")))
MIN_ROBUST_Z = float(os.getenv("ANOMALY_MIN_ROBUST_Z", "3.5"))
MIN_MOVED_METRICS = max(1, int(os.getenv("ANOMALY_MIN_MOVED_METRICS", "2")))

# Cost controls (2026-10-03). PROD measured this job at ~69 s per run for 70 resources, every 15 min,
# to produce alerts that are hidden from the UI. Defaults keep the old behaviour (enabled, every
# scheduler cycle); set these in .env to cut the cost without a code change:
#   ANOMALY_ENABLED=false               -> skip scoring entirely (open hidden anomaly alerts are resolved once)
#   ANOMALY_MIN_INTERVAL_MINUTES=60     -> run at most once per 60 min (the scheduler still ticks every 15)
_last_run_monotonic = None


def _enabled() -> bool:
    return os.getenv("ANOMALY_ENABLED", "true").strip().lower() != "false"


def _min_interval_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("ANOMALY_MIN_INTERVAL_MINUTES", "0"))) * 60.0
    except ValueError:
        return 0.0


def _too_soon() -> bool:
    """True if the previous run was less than ANOMALY_MIN_INTERVAL_MINUTES ago. A 60-second
    tolerance stops a cycle that lands a few seconds early from being skipped for a whole interval."""
    interval = _min_interval_seconds()
    if not interval or _last_run_monotonic is None:
        return False
    return (time.monotonic() - _last_run_monotonic) < (interval - 60.0)
# Bucket width for aligning metrics collected on independent schedules
# -- different metrics are rarely sampled at exactly the same instant,
# so readings are floored into shared buckets before being treated as
# one multivariate "row."
RESAMPLE_MINUTES = 5


def _pivot_to_matrix(rows):
    """rows: [{metric_name, metric_timestamp, metric_value}, ...] for
    ONE resource. Returns a pandas DataFrame indexed by time bucket,
    one column per metric, forward-filled across small gaps, with any
    row still containing a gap (e.g. the very start of history, before
    every metric has reported at least once) dropped -- IsolationForest
    requires a fully dense matrix."""
    import pandas as pd
    df = pd.DataFrame(rows)
    df["bucket"] = pd.to_datetime(df["metric_timestamp"]).dt.floor(f"{RESAMPLE_MINUTES}min")
    pivoted = df.pivot_table(index="bucket", columns="metric_name", values="metric_value", aggfunc="mean")
    pivoted = pivoted.sort_index().ffill()
    pivoted = pivoted.dropna(axis=0, how="any")
    return pivoted


def _add_time_features(matrix):
    """Appends hour-of-day sin/cos (UTC) so the model can learn daily seasonality.
    The two columns are model features only -- never counted as metrics, never explained."""
    import numpy as np
    hours = matrix.index.hour + matrix.index.minute / 60.0
    out = matrix.copy()
    out["_hod_sin"] = np.sin(2 * np.pi * hours / 24.0)
    out["_hod_cos"] = np.cos(2 * np.pi * hours / 24.0)
    return out


def _deviation_report(history, recent, metric_cols):
    """Robust per-metric z-scores of the recent buckets versus the training history.
    Returns [(metric, z), ...] sorted by |z| descending. scale = the larger of 1.4826*MAD
    and the std (MAD collapses to 0 on sparse count metrics that are mostly zero, and
    std alone is inflated by the odd spike; the larger of the two is the conservative
    choice), with a 5%-of-median floor so a near-constant metric is not infinitely
    sensitive. Display z is capped at +/-99."""
    med = history[metric_cols].median()
    mad = (history[metric_cols] - med).abs().median() * 1.4826
    std = history[metric_cols].std(ddof=0)
    recent_med = recent[metric_cols].median()
    out = []
    for m in metric_cols:
        scale = max(float(mad[m]), float(std[m]), 0.05 * abs(float(med[m])), 1e-9)
        z = (float(recent_med[m]) - float(med[m])) / scale
        out.append((m, max(-99.0, min(99.0, z))))
    out.sort(key=lambda t: abs(t[1]), reverse=True)
    return out


def _format_why(deviations, limit=3):
    return ", ".join(f"{m} {z:+.1f}\u03c3" for m, z in deviations[:limit])


def _resource_ids_with_enough_metrics(cursor):
    cursor.execute("""
        SELECT r.resource_id, r.aws_account_id, COUNT(DISTINCT h.metric_name) AS metric_count
        FROM metric_history h
        JOIN resources r ON r.id = h.resource_id
        WHERE h.metric_timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY)
        GROUP BY r.resource_id, r.aws_account_id
        HAVING metric_count >= %s
    """, (LOOKBACK_DAYS, MIN_METRICS_PER_RESOURCE))
    return cursor.fetchall()


def detect_multivariate_anomalies() -> int:
    """
    For every resource with enough recent multi-metric history, fits an
    IsolationForest on its own history and scores the most recent
    reading. Anomalous resources get a `multivariate_anomaly` alert
    (created if not already active, refreshed if already active);
    resources previously flagged but no longer anomalous have that
    alert auto-resolved. Returns the number of resources found
    anomalous THIS cycle.
    """
    global _last_run_monotonic
    if not _enabled():
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            resolved = _resolve_cleared_anomalies(cursor, set())
            conn.commit()
            if resolved:
                logger.info(f"[multivariate_anomaly] disabled (ANOMALY_ENABLED=false); "
                            f"resolved {resolved} previously-flagged alert(s)")
        finally:
            cursor.close()
            conn.close()
        return 0
    if _too_soon():
        return 0
    _last_run_monotonic = time.monotonic()

    from sklearn.ensemble import IsolationForest

    started = time.time()
    scored = raw_flagged = sustained = 0
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        candidates = _resource_ids_with_enough_metrics(cursor)
        anomalous_resource_ids = set()
        k = MIN_CONSECUTIVE_BUCKETS

        for candidate in candidates:
            aws_resource_id = candidate["resource_id"]
            # ACCOUNT-scoped: resource_id alone is only unique within one
            # account (migrations 045-048); without this two accounts'
            # same-named resources were pooled into one training set.
            cursor.execute("""
                SELECT h.metric_name, h.metric_timestamp, h.metric_value
                FROM metric_history h
                JOIN resources r ON r.id = h.resource_id
                WHERE r.resource_id = %s AND r.aws_account_id = %s
                  AND h.metric_timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY)
                  AND h.metric_value IS NOT NULL
            """, (aws_resource_id, candidate["aws_account_id"], LOOKBACK_DAYS))
            rows = cursor.fetchall()

            matrix = _pivot_to_matrix(rows)
            metric_cols = list(matrix.columns)
            # The last k buckets are the ones under test, so training excludes them.
            if len(matrix) < MIN_SAMPLES_FOR_TRAINING + k or len(metric_cols) < MIN_METRICS_PER_RESOURCE:
                continue
            scored += 1

            features = _add_time_features(matrix)
            history = features.iloc[:-k]
            recent = features.iloc[-k:]

            model = IsolationForest(n_estimators=100, contamination=CONTAMINATION, random_state=42)
            model.fit(history.values)
            preds = model.predict(recent.values)
            if preds[-1] != -1:
                continue
            raw_flagged += 1                      # newest bucket is an outlier
            if (preds != -1).any():
                continue                          # ... but not for the whole sustained window
            sustained += 1

            deviations = _deviation_report(matrix.iloc[:-k], matrix.iloc[-k:], metric_cols)
            moved = [d for d in deviations if abs(d[1]) >= MIN_ROBUST_Z]
            if len(moved) < MIN_MOVED_METRICS:
                continue                          # outlier by shape, but nothing moved materially

            score = float(model.decision_function(recent.values).mean())  # more negative = more anomalous
            anomalous_resource_ids.add((candidate["aws_account_id"], aws_resource_id))
            _upsert_anomaly_alert(cursor, aws_resource_id, candidate["aws_account_id"],
                                   score, metric_cols, _format_why(deviations))

        resolved = _resolve_cleared_anomalies(cursor, anomalous_resource_ids)
        conn.commit()
        logger.info(f"[multivariate_anomaly] scored {scored} resource(s) in {time.time() - started:.1f}s: "
                    f"{raw_flagged} outlier now, {sustained} sustained {k * RESAMPLE_MINUTES}min, "
                    f"{len(anomalous_resource_ids)} confirmed; {resolved} previously-flagged recovered")
        return len(anomalous_resource_ids)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def _upsert_anomaly_alert(cursor, aws_resource_id, aws_account_id, score, metric_names, why=""):
    # Every alert writer MUST set aws_account_id: since migration 048 all
    # readers join on it, so a row without it is invisible (this file's INSERT
    # used to omit it, so anomaly alerts were created, never shown, never
    # counted in health scores, and never matched by the resolve pass).
    cursor.execute("""
        SELECT id FROM alerts
        WHERE aws_account_id = %s AND resource_id = %s
          AND metric_name = 'multivariate_anomaly' AND status IN ('active', 'acknowledged')
        LIMIT 1
    """, (aws_account_id, aws_resource_id))
    existing = cursor.fetchone()

    if existing:
        cursor.execute("""
            UPDATE alerts SET current_value = %s, last_seen_at = UTC_TIMESTAMP()
            WHERE id = %s
        """, (score, existing["id"]))
        return

    cursor.execute("""
        SELECT resource_type, tags FROM resources
        WHERE resource_id = %s AND aws_account_id = %s
    """, (aws_resource_id, aws_account_id))
    resource = cursor.fetchone() or {}
    resource_type = resource.get("resource_type", "unknown")
    try:
        tags = json.loads(resource.get("tags") or "{}")
    except Exception:
        tags = {}
    environment = tags.get("environment", tags.get("Environment", "prod")).lower()
    group_key = f"{aws_account_id}:{resource_type}:multivariate_anomaly"

    cursor.execute("""
        INSERT INTO alerts
            (aws_account_id, resource_id, metric_name, severity,
             environment, group_key, status, triggered_at, last_seen_at,
             healthy_streak, current_value, threshold)
        VALUES (%s, %s, 'multivariate_anomaly', 'WARNING', %s, %s, 'active',
                UTC_TIMESTAMP(), UTC_TIMESTAMP(), 0, %s, 0)
    """, (aws_account_id, aws_resource_id, environment, group_key, score))

    logger.info(f"[multivariate_anomaly] new anomaly alert on {aws_resource_id} "
                f"(score={score:.4f}; moved most: {why or 'n/a'}; metrics scored: {len(metric_names)})")


def _resolve_cleared_anomalies(cursor, currently_anomalous) -> int:
    """currently_anomalous: set of (aws_account_id, resource_id)."""
    cursor.execute("""
        SELECT id, aws_account_id, resource_id FROM alerts
        WHERE metric_name = 'multivariate_anomaly' AND status IN ('active', 'acknowledged')
    """)
    active = cursor.fetchall()
    resolved = 0
    for row in active:
        if (row["aws_account_id"], row["resource_id"]) not in currently_anomalous:
            cursor.execute("""
                UPDATE alerts
                SET status = 'resolved', resolved_at = UTC_TIMESTAMP(), last_seen_at = UTC_TIMESTAMP(),
                    resolution_reason = 'anomaly_cleared', resolved_by = 'system'
                WHERE id = %s
            """, (row["id"],))
            resolved += 1
    return resolved
