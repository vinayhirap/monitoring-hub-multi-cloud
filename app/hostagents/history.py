# app/hostagents/history.py
"""
Host agent charts: the pure parts of GET /api/host-agents/{id}/history (no database, no FastAPI), so they are unit-tested
on their own.

The response is shaped for the SAME chart card the EC2 / RDS / Lambda pages use (frontend MetricChartCard + ChartToolbar):

  * series points are {t, v, a, mn, mx, n}: one point per bucket, the fields app.metric_display.bucketize() produces that the
    chart card reads for Average / Minimum / Maximum / SampleCount. (No `s`: Sum is never offered for a gauge, so it would
    only add payload.) The aggregation is done by the database (GROUP BY bucket), so a 1M range reads ~240 points per
    series instead of ~43,000 raw rows. `v` is the Average (every host metric is stored as an average / gauge).
  * meta[<stored metric name>] has the keys the card reads (title, unit, stats, poll / period seconds, freshness, the
    CURRENT warning / critical lines, any open alert).

Nothing here estimates or invents a value: a bucket exists only where the agent delivered at least one reading.
"""
from datetime import datetime, timezone

# The unit strings frontend/src/utils/metricFormat.js formats (CloudWatch-style). Contract units are in metric_map.DISPLAY.
CHART_UNITS = {"%": "Percent", "bytes": "Bytes", "bytes/s": "Bytes/Second", "seconds": "Seconds", "": "Count"}

# A host has at most 32 mounts / interfaces per metric (metric_map.MAX_SERIES_PER_METRIC); 15 contract metrics. The cap keeps
# the IN (...) list and the response bounded whatever the table holds.
MAX_CHART_SERIES = 96

# Average over a bucket, computed by the database. TIMESTAMPDIFF against the epoch, not UNIX_TIMESTAMP(): the latter converts
# through the session time zone, and metric_timestamp is naive UTC throughout this app.
SQL_HISTORY_BUCKETS = (
    "SELECT metric_name, "
    "FLOOR(TIMESTAMPDIFF(SECOND, '1970-01-01 00:00:00', metric_timestamp) / %s) AS bucket, "
    "AVG(metric_value) AS a, MIN(metric_value) AS mn, MAX(metric_value) AS mx, COUNT(*) AS n "
    "FROM metric_history WHERE resource_id = %s AND metric_timestamp >= %s AND metric_value IS NOT NULL "
    "AND metric_name IN ({names}) GROUP BY metric_name, bucket ORDER BY metric_name, bucket")


def history_sql(n_names: int) -> str:
    return SQL_HISTORY_BUCKETS.replace("{names}", ", ".join(["%s"] * n_names))


def chart_unit(contract_unit) -> str:
    return CHART_UNITS.get(contract_unit or "", "None")


def chart_title(info: dict) -> str:
    """'Disk used · /', 'Network receive · ens5'; the bare title for series without a label."""
    label = info.get("label")
    return f"{info['title']} · {label}" if info.get("label_key") and label else info["title"]


def _iso_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def rows_to_points(rows, bucket_secs: int) -> dict:
    """[{metric_name, bucket, a, mn, mx, n}] -> {metric_name: [{t, v, a, mn, mx, n}]}, oldest first. A row with no
    average is skipped, never turned into a zero."""
    out = {}
    for r in rows:
        if r.get("a") is None or r.get("bucket") is None:
            continue
        a = float(r["a"])
        out.setdefault(r["metric_name"], []).append({
            "t": _iso_epoch(int(r["bucket"]) * bucket_secs), "v": round(a, 4), "a": round(a, 4),
            "mn": round(float(r["mn"]), 4), "mx": round(float(r["mx"]), 4), "n": int(r["n"]),
        })
    for pts in out.values():
        pts.sort(key=lambda p: p["t"])
    return out


def chart_meta(info: dict, name: str, *, interval_seconds, stale_after, last_seen_epoch, stats,
               threshold=None, alert=None) -> dict:
    """The per-metric entry the chart card reads (same keys as app.metric_meta.build_metric_meta). `last_collected_ts` is
    when the hub last heard from the agent, so the card's 'polled Ns ago' is the agent's real last report."""
    interval = int(interval_seconds) if interval_seconds else None
    return {
        "title": chart_title(info), "metric_name": name, "unit": chart_unit(info.get("unit")),
        "unit_symbol": info.get("unit") or "", "native_stat": "Average", "stats": list(stats),
        "description": None,
        "poll_seconds": interval, "poll_label": _interval_label(interval),
        "stale_after_seconds": stale_after, "sparse": False,
        "period_seconds": interval, "period_label": _interval_label(interval),
        "tier": None, "poll_source": "host agent",
        "last_collected_ts": last_seen_epoch,
        "threshold": threshold, "alert": alert,
    }


def _interval_label(seconds):
    if not seconds:
        return None
    if seconds % 3600 == 0:
        return f"{seconds // 3600} hr"
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def threshold_entry(warning, critical, comparison, mode, static_warning, static_critical) -> dict:
    """Same shape as build_metric_meta()['threshold']; host metrics are never rate-scaled, so no scale factor."""
    def r(v):
        return None if v is None else round(float(v), 6)
    return {"warning": r(warning), "critical": r(critical), "comparison": comparison, "mode": mode,
            "static_warning": r(static_warning), "static_critical": r(static_critical)}
