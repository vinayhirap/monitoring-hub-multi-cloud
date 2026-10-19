# app/collector/api_usage.py
"""
Measured cloud metric-read volume, so polling cost is a number in the DB
instead of an estimate (metric-polling audit, 2026-09-23; extended with the
source/operation dimensions in Phase A, 2026-10-19).

Every paid call site calls
    record(provider, tier, calls=..., units=..., source=..., operation=...)

DIMENSIONS
  provider   aws | azure | gcp
  source     collector (scheduled polling, the default) | ui (an API request
             a person triggered, e.g. a detail page) | discovery (the
             user-triggered metric-catalog discovery). For source != collector
             the stored `tier` label IS the source name ("ui", "discovery"),
             because there is no polling tier for those calls; the existing
             (provider, tier) grouping therefore keeps working unchanged.
  operation  the billed API: GetMetricData | ListMetrics | GetMetricStatistics
             (aws), plus the legacy defaults for rows recorded without one
             (aws -> GetMetricData, tag_sync -> GetResources, azure ->
             MonitorMetrics, gcp -> ListTimeSeries).

BILLED UNITS (`units`)
  aws GetMetricData        metrics requested, $0.01 / 1,000, no free tier.
                           `calls` counts pages (NextToken); units are counted
                           once per request, not per page.
  aws ListMetrics          requests: one per page actually fetched.
  aws GetMetricStatistics  metrics requested (one metric per request).
  azure                    metrics data-plane API calls (billed per 1,000
                           calls after 1,000,000 free / subscription / month)
  gcp                      time series returned by ListTimeSeries (billed per
                           million after 1,000,000 free / billing account /
                           month)
An attempted request is counted even when it raises (a ceiling, matching the
GetMetricData collector convention). SEARCH() expressions are counted as 1
(a floor). ListMetrics / GetMetricStatistics carry NO list price in the row
(list_price_usd is null): their per-request rate has not been verified against
the bill, so read `units` and multiply by the rate on your invoice.

Counters are per process and thread-safe. flush_if_due() writes one
op_events row per (provider, tier, source, operation) at most once an hour:
  event_type = 'metric_api_usage', detail = {provider, tier, source,
  operation, calls, units, unit, list_price_usd, window_start, window_end}
Rows written before Phase A have no source/operation: COALESCE them to
'collector' / the legacy default as the queries below do.

READ-ONLY QUERIES (MySQL 8 and MariaDB; JSON_UNQUOTE(JSON_EXTRACT()) rather
than the -> operator, which MariaDB lacks)

1) Billed units by provider / tier / source / operation, last 24 h:
  SELECT JSON_UNQUOTE(JSON_EXTRACT(detail,'$.provider')) AS provider,
         JSON_UNQUOTE(JSON_EXTRACT(detail,'$.tier')) AS tier,
         COALESCE(JSON_UNQUOTE(JSON_EXTRACT(detail,'$.source')),'collector') AS source,
         COALESCE(JSON_UNQUOTE(JSON_EXTRACT(detail,'$.operation')),'GetMetricData') AS operation,
         SUM(CAST(JSON_UNQUOTE(JSON_EXTRACT(detail,'$.calls')) AS UNSIGNED)) AS calls,
         SUM(CAST(JSON_UNQUOTE(JSON_EXTRACT(detail,'$.units')) AS UNSIGNED)) AS units,
         ROUND(SUM(CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(detail,'$.list_price_usd')),'null') AS DECIMAL(14,6))),4) AS usd
  FROM op_events
  WHERE event_type = 'metric_api_usage'
    AND created_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY
  GROUP BY provider, tier, source, operation
  ORDER BY provider, source, units DESC;

2) The original daily-totals shape still works (grouped by provider/tier):
  SELECT JSON_UNQUOTE(JSON_EXTRACT(detail,'$.provider')) p,
         JSON_UNQUOTE(JSON_EXTRACT(detail,'$.tier')) t,
         SUM(CAST(JSON_UNQUOTE(JSON_EXTRACT(detail,'$.calls')) AS UNSIGNED)) calls,
         SUM(CAST(JSON_UNQUOTE(JSON_EXTRACT(detail,'$.units')) AS UNSIGNED)) units
  FROM op_events WHERE event_type = 'metric_api_usage'
    AND created_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY GROUP BY p, t;
Going forward a collector tier row can also hold ListMetrics requests, so the
GetMetricData-only baseline needs the operation filter in query 1.

list_price_usd ignores free tiers (a ceiling, not the invoice).
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 3600

SOURCE_COLLECTOR = "collector"
SOURCE_UI = "ui"
SOURCE_DISCOVERY = "discovery"

OP_GET_METRIC_DATA = "GetMetricData"
OP_LIST_METRICS = "ListMetrics"
OP_GET_METRIC_STATISTICS = "GetMetricStatistics"

# Legacy default for callers that pass no operation (Azure/GCP collectors,
# waf, tag_sync, the GetMetricData runner), so their rows keep their meaning.
_DEFAULT_OPERATION = {"aws": OP_GET_METRIC_DATA, "azure": "MonitorMetrics", "gcp": "ListTimeSeries"}
_TAG_SYNC_TIER = "tag_sync"
_TAG_SYNC_OPERATION = "GetResources"

# What one `units` is, per operation (falls back to per provider).
_UNIT_BY_OPERATION = {
    OP_GET_METRIC_DATA: "metrics_requested",
    OP_LIST_METRICS: "requests",
    OP_GET_METRIC_STATISTICS: "metrics_requested",
    "MonitorMetrics": "api_calls",
    "ListTimeSeries": "time_series_returned",
}
_UNIT = {"aws": "metrics_requested", "azure": "api_calls", "gcp": "time_series_returned"}

# List price per unit. Only rates already relied on elsewhere in this repo are
# listed; AWS ListMetrics / GetMetricStatistics are deliberately absent
# (unverified) so list_price_usd is null for them -- see the module docstring.
_PRICE_PER_UNIT = {
    ("aws", OP_GET_METRIC_DATA): 0.01 / 1000,
    ("azure", "MonitorMetrics"): 0.01 / 1000,
    ("gcp", "ListTimeSeries"): 0.50 / 1_000_000,
}

_lock = threading.Lock()
_counts = {}                  # (provider, tier, source, operation) -> [calls, units]
_window_start = time.time()
_last_flush = time.time()


def billed_metric_queries(queries):
    """GetMetricData billed units for one request's MetricDataQueries:
    MetricStat queries count 1 each and a SEARCH() expression counts 1 (a
    floor: it is billed per metric it matches, unknown up front). Pure
    expressions (e.g. 100 - raw) add nothing. Same rule as the collector's
    runner._billed_units. Never raises."""
    try:
        n = 0
        for q in queries or ():
            if "MetricStat" in q or "SEARCH(" in (q.get("Expression") or ""):
                n += 1
        return n
    except Exception:
        return 0


def _resolve(provider, tier, source, operation):
    source = str(source or SOURCE_COLLECTOR)
    if operation is None:
        operation = (_TAG_SYNC_OPERATION if tier == _TAG_SYNC_TIER
                     else _DEFAULT_OPERATION.get(provider, "unknown"))
    # ui / discovery have no polling tier: the label is the source name.
    label = (tier or "unknown") if source == SOURCE_COLLECTOR else source
    return (provider, label, source, str(operation))


def record(provider, tier, calls=0, units=0, source=SOURCE_COLLECTOR, operation=None):
    """Add to the in-memory counters. NEVER raises and does no I/O: it is
    safe to call inline next to any paid cloud call."""
    try:
        if not calls and not units:
            return
        key = _resolve(provider, tier, source, operation)
        calls, units = int(calls), int(units)
        with _lock:
            c = _counts.setdefault(key, [0, 0])
            c[0] += calls
            c[1] += units
    except Exception as e:  # a metering bug must never break a cloud call
        try:
            logger.debug(f"api_usage.record ignored: {e}")
        except Exception:
            pass


def snapshot():
    """{(provider, tier, source, operation): (calls, units)}"""
    with _lock:
        return {k: tuple(v) for k, v in _counts.items()}


def _take():
    global _counts, _window_start, _last_flush
    with _lock:
        taken, start = _counts, _window_start
        _counts, _window_start, _last_flush = {}, time.time(), time.time()
    return taken, start


def _unit_label(provider, operation):
    return _UNIT_BY_OPERATION.get(operation) or _UNIT.get(provider, "units")


def _list_price(provider, operation, units):
    rate = _PRICE_PER_UNIT.get((provider, operation))
    if rate is not None:
        return round(units * rate, 6)
    # tag_sync etc. record 0 units: price 0, not unknown
    return 0.0 if not units else None


def flush(force=False):
    """Write accumulated counters to op_events (one row per provider / tier /
    source / operation). Never raises."""
    taken, start = _take()
    if not taken:
        return 0
    try:
        from app.collector.op_log import log_event
    except Exception as e:  # pragma: no cover
        logger.warning(f"api_usage: op_log unavailable, dropping counters: {e}")
        return 0
    end = time.time()
    written = 0
    for (provider, tier, source, operation), (calls, units) in sorted(taken.items()):
        unit = _unit_label(provider, operation)
        try:
            log_event(
                "metric_api_usage",
                f"{provider}/{tier} [{source}] {operation}: {calls} calls, {units} {unit}",
                severity="INFO",
                detail={"provider": provider, "tier": tier, "source": source,
                        "operation": operation, "calls": calls,
                        "units": units, "unit": unit,
                        "list_price_usd": _list_price(provider, operation, units),
                        "window_start": int(start), "window_end": int(end)},
            )
            written += 1
        except Exception as e:
            logger.warning(f"api_usage: flush failed for {provider}/{tier}/{source}/{operation}: {e}")
    return written


def flush_if_due():
    if time.time() - _last_flush >= FLUSH_INTERVAL_SECONDS:
        return flush()
    return 0
