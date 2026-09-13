# app/collector/trend.py
"""
Trend detection + capacity-exhaustion forecast -- AIOps roadmap Phase 1
(2026-09-14).

Deliberately plain linear regression (numpy.polyfit, degree 1) over
each resource+metric's recent metric_history, not a seasonal/ML model:
for a monotonic capacity metric (disk %, EBS free space, RDS storage),
a straight-line "days until full" extrapolation is the right level of
sophistication -- see AI_ML_ROADMAP.md Phase 2 for when a seasonal
model (Prophet/statsmodels) actually earns its extra complexity
(traffic/cost forecasting, not a monotonic ceiling).

Only metrics with a known, meaningful ceiling are forecast -- see
CAPACITY_METRICS below, an explicit allowlist. A metric with no natural
ceiling (NetworkIn, request count) has no "days until full" to report
and is never considered.

Computed on-demand per resource-detail view (called from
app/api/insights.py), not persisted on a schedule -- a forecast is only
useful when someone is actually looking at that resource's chart, and
metric_history already holds everything needed to compute it fresh
each time cheaply (bounded by TREND_LOOKBACK_DAYS).
"""
import logging
import numpy as np
from app.db import get_connection

logger = logging.getLogger(__name__)

# metric_name -> the value that represents "exhausted." Metrics that
# count DOWN toward zero (free space) use 0.0; metrics that count UP
# toward a percentage ceiling use 100.0.
CAPACITY_METRICS = {
    "DiskSpaceUtilization": 100.0,
    "disk_used_percent":    100.0,
    "FreeStorageSpace":     0.0,
    "EBSFreeSpacePercent":  0.0,
}

TREND_LOOKBACK_DAYS = 14
MIN_POINTS_FOR_TREND = 20

# Don't report a "trend" so shallow it would technically reach the
# ceiling decades from now -- not actionable, shouldn't be presented as
# if it were.
MAX_REPORTABLE_DAYS = 365


def _linear_trend(timestamps_seconds, values):
    """Returns (slope_per_day, intercept) via least-squares fit, or
    None if there isn't enough data to fit meaningfully."""
    if len(values) < MIN_POINTS_FOR_TREND:
        return None
    x = np.array(timestamps_seconds, dtype=float)
    y = np.array(values, dtype=float)
    x = (x - x.min()) / 86400.0  # days since first point -- slope directly in units/day
    try:
        slope, intercept = np.polyfit(x, y, 1)
    except Exception:
        return None
    return float(slope), float(intercept)


def compute_capacity_forecasts(aws_resource_id: str = None) -> list:
    """
    Fits a linear trend for every (resource, metric) pair in
    CAPACITY_METRICS with enough recent history, and returns forecasts
    for any that are heading toward exhaustion within
    MAX_REPORTABLE_DAYS. Pass aws_resource_id to scope to one resource
    (the resource-detail view's normal use); omit to scan every
    resource (used by the "low" tier's own periodic scan, if wired in).
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    results = []
    try:
        for metric_name, ceiling in CAPACITY_METRICS.items():
            query = """
                SELECT r.resource_id AS aws_resource_id,
                       UNIX_TIMESTAMP(h.metric_timestamp) AS ts, h.metric_value
                FROM metric_history h
                JOIN resources r ON r.id = h.resource_id
                WHERE h.metric_name = %s
                  AND h.metric_timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY)
                  AND h.metric_value IS NOT NULL
            """
            params = [metric_name, TREND_LOOKBACK_DAYS]
            if aws_resource_id:
                query += " AND r.resource_id = %s"
                params.append(aws_resource_id)
            query += " ORDER BY r.resource_id, h.metric_timestamp"

            cursor.execute(query, params)
            rows = cursor.fetchall()

            by_resource = {}
            for row in rows:
                by_resource.setdefault(row["aws_resource_id"], []).append(row)

            for res_id, points in by_resource.items():
                fit = _linear_trend([p["ts"] for p in points], [p["metric_value"] for p in points])
                if fit is None:
                    continue
                slope, _intercept = fit
                current_value = points[-1]["metric_value"]

                heading_toward_ceiling = (
                    (ceiling >= current_value and slope > 0) or
                    (ceiling <= current_value and slope < 0)
                )
                if not heading_toward_ceiling or slope == 0:
                    continue

                days_to_exhaustion = abs((ceiling - current_value) / slope)
                if days_to_exhaustion > MAX_REPORTABLE_DAYS:
                    continue

                results.append({
                    "resource_id": res_id,
                    "metric_name": metric_name,
                    "current_value": current_value,
                    "slope_per_day": round(slope, 4),
                    "days_to_exhaustion": round(days_to_exhaustion, 1),
                })

        return results
    finally:
        cursor.close()
        conn.close()
