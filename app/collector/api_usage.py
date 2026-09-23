# app/collector/api_usage.py
"""
Measured cloud metric-read volume, so polling cost is a number in the DB
instead of an estimate (metric-polling audit, 2026-09-23).

Every collector calls record(provider, tier, calls=..., units=...):
  aws    units = metrics requested via GetMetricData (the billed unit,
               $0.01 / 1,000, no free tier)
  azure  units = metrics data-plane API calls (billed per 1,000 calls
               after 1,000,000 free / subscription / month)
  gcp    units = time series returned by ListTimeSeries (billed per
               million after 1,000,000 free / billing account / month)

Counters are per process and thread-safe. flush_if_due() writes one
op_events row per (provider, tier) at most once an hour:
  event_type = 'metric_api_usage', detail = {provider, tier, calls, units,
  unit, list_price_usd, window_start, window_end}
Daily totals:
  SELECT JSON_UNQUOTE(detail->'$.provider') p, JSON_UNQUOTE(detail->'$.tier') t,
         SUM(detail->'$.calls') calls, SUM(detail->'$.units') units,
         ROUND(SUM(detail->'$.list_price_usd'), 4) usd
  FROM op_events WHERE event_type = 'metric_api_usage'
    AND created_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY GROUP BY p, t;
list_price_usd ignores free tiers (a ceiling, not the invoice).
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 3600

_UNIT = {"aws": "metrics_requested", "azure": "api_calls", "gcp": "time_series_returned"}
_PRICE_PER_UNIT = {"aws": 0.01 / 1000, "azure": 0.01 / 1000, "gcp": 0.50 / 1_000_000}

_lock = threading.Lock()
_counts = {}                  # (provider, tier) -> [calls, units]
_window_start = time.time()
_last_flush = time.time()


def record(provider, tier, calls=0, units=0):
    if not calls and not units:
        return
    key = (provider, tier or "unknown")
    with _lock:
        c = _counts.setdefault(key, [0, 0])
        c[0] += int(calls)
        c[1] += int(units)


def snapshot():
    with _lock:
        return {k: tuple(v) for k, v in _counts.items()}


def _take():
    global _counts, _window_start, _last_flush
    with _lock:
        taken, start = _counts, _window_start
        _counts, _window_start, _last_flush = {}, time.time(), time.time()
    return taken, start


def flush(force=False):
    """Write accumulated counters to op_events (one row per provider/tier).
    Never raises."""
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
    for (provider, tier), (calls, units) in sorted(taken.items()):
        price = round(units * _PRICE_PER_UNIT.get(provider, 0.0), 6)
        try:
            log_event(
                "metric_api_usage",
                f"{provider}/{tier}: {calls} calls, {units} {_UNIT.get(provider, 'units')}",
                severity="INFO",
                detail={"provider": provider, "tier": tier, "calls": calls,
                        "units": units, "unit": _UNIT.get(provider, "units"),
                        "list_price_usd": price,
                        "window_start": int(start), "window_end": int(end)},
            )
            written += 1
        except Exception as e:
            logger.warning(f"api_usage: flush failed for {provider}/{tier}: {e}")
    return written


def flush_if_due():
    if time.time() - _last_flush >= FLUSH_INTERVAL_SECONDS:
        return flush()
    return 0
