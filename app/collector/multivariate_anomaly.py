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
"""
import json
import logging

from app.db import get_connection

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 14
# Below this many distinct metrics, there's no real "combination" to
# score -- 1-2 metrics is exactly what baseline.py's per-metric sigma
# bands already cover; this module only adds value once there's a
# genuine multivariate pattern to look for.
MIN_METRICS_PER_RESOURCE = 3
MIN_SAMPLES_FOR_TRAINING = 50
CONTAMINATION = 0.02
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
    from sklearn.ensemble import IsolationForest

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        candidates = _resource_ids_with_enough_metrics(cursor)
        anomalous_resource_ids = set()

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
            if len(matrix) < MIN_SAMPLES_FOR_TRAINING + 1 or matrix.shape[1] < MIN_METRICS_PER_RESOURCE:
                continue

            history = matrix.iloc[:-1].values
            latest = matrix.iloc[[-1]].values

            model = IsolationForest(n_estimators=100, contamination=CONTAMINATION, random_state=42)
            model.fit(history)
            is_anomaly = model.predict(latest)[0] == -1
            score = float(model.decision_function(latest)[0])  # more negative = more anomalous

            if is_anomaly:
                anomalous_resource_ids.add((candidate["aws_account_id"], aws_resource_id))
                _upsert_anomaly_alert(cursor, aws_resource_id, candidate["aws_account_id"],
                                       score, list(matrix.columns))

        resolved = _resolve_cleared_anomalies(cursor, anomalous_resource_ids)
        conn.commit()
        logger.info(f"[multivariate_anomaly] {len(anomalous_resource_ids)} resource(s) anomalous "
                    f"this cycle, {resolved} previously-flagged resource(s) recovered")
        return len(anomalous_resource_ids)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def _upsert_anomaly_alert(cursor, aws_resource_id, aws_account_id, score, metric_names):
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
                f"(metrics: {', '.join(metric_names)}, score={score:.4f})")


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
