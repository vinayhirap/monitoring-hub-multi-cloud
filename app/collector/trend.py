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
    finite = np.isfinite(x) & np.isfinite(y)
    x, y = x[finite], y[finite]
    # Need enough finite points spread over >= 2 distinct timestamps;
    # otherwise polyfit is rank-deficient and returns NaN/garbage.
    if len(y) < MIN_POINTS_FOR_TREND or np.unique(x).size < 2:
        return None
    x = (x - x.min()) / 86400.0  # days since first point -- slope directly in units/day
    try:
        slope, intercept = np.polyfit(x, y, 1)
    except Exception:
        return None
    if not (np.isfinite(slope) and np.isfinite(intercept)):
        return None
    return float(slope), float(intercept)


def compute_capacity_forecasts(aws_resource_id: str = None, aws_account_ids=None) -> list:
    """
    Fits a linear trend for every (resource, metric) pair in
    CAPACITY_METRICS with enough recent history, and returns forecasts
    for any that are heading toward exhaustion within
    MAX_REPORTABLE_DAYS. Pass aws_resource_id to scope to one resource
    (the resource-detail view's normal use); pass aws_account_ids (an
    iterable of ints) to scope to a set of accounts (the fleet-summary
    endpoint's use, added 2026-09-14, so a viewer with restricted
    account access never sees another account's capacity-risk count in
    the aggregate); omit both to scan every resource.
    """
    if aws_account_ids is not None:
        aws_account_ids = list(aws_account_ids)
        if not aws_account_ids:
            # An EMPTY scope means "no accounts", never "every account"
            # (the old truthiness check scanned the whole fleet).
            return []
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    results = []
    try:
        for metric_name, ceiling in CAPACITY_METRICS.items():
            # Grouped per resources.id (not the resource_id string, which
            # can repeat across accounts and used to merge two accounts'
            # series into one fit) and downsampled to hourly means in SQL:
            # the fleet-wide call used to pull every raw 14-day sample of
            # every resource into Python memory.
            query = """
                SELECT r.id AS rid, r.resource_id AS aws_resource_id, r.aws_account_id,
                       MIN(UNIX_TIMESTAMP(h.metric_timestamp)) AS ts, AVG(h.metric_value) AS metric_value
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
            if aws_account_ids:
                placeholders = ",".join(["%s"] * len(aws_account_ids))
                query += f" AND r.aws_account_id IN ({placeholders})"
                params.extend(aws_account_ids)
            query += (" GROUP BY r.id, r.resource_id, r.aws_account_id,"
                      " FLOOR(UNIX_TIMESTAMP(h.metric_timestamp) / 3600)"
                      " ORDER BY r.id, ts")

            cursor.execute(query, params)
            rows = cursor.fetchall()

            by_resource = {}
            for row in rows:
                by_resource.setdefault(row["rid"], []).append(row)

            for _rid, points in by_resource.items():
                fit = _linear_trend([p["ts"] for p in points], [p["metric_value"] for p in points])
                if fit is None:
                    continue
                slope, _intercept = fit
                current_value = float(points[-1]["metric_value"])

                # Direction is fixed by the metric, not by which side of
                # the ceiling the last sample happens to sit on. The old
                # check reported a RECOVERING series (e.g. disk at 101%
                # now falling) as "exhausting", and FreeStorageSpace
                # already at 0 but rising as a capacity risk.
                if ceiling > 0:   # counts UP toward the ceiling (percent used)
                    if slope <= 0:
                        continue
                    days_to_exhaustion = max(0.0, (ceiling - current_value) / slope)
                else:             # counts DOWN toward zero (free space)
                    if slope >= 0:
                        continue
                    days_to_exhaustion = max(0.0, (current_value - ceiling) / -slope)
                if days_to_exhaustion > MAX_REPORTABLE_DAYS:
                    continue

                res_id = points[0]["aws_resource_id"]
                results.append({
                    "resource_id": res_id,
                    "aws_account_id": points[0]["aws_account_id"],
                    "metric_name": metric_name,
                    "current_value": current_value,
                    "slope_per_day": round(slope, 4),
                    "days_to_exhaustion": round(days_to_exhaustion, 1),
                })

        return results
    finally:
        cursor.close()
        conn.close()
