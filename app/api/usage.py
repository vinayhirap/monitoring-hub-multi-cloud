# app/api/usage.py
"""
Cloud API cost panel: read-only summary of the metered paid metric reads.

Data source: op_events rows with event_type='metric_api_usage', one per (provider, tier) per flush, written by
app/collector/api_usage.py once an hour per process:
    detail = {provider, tier, calls, units, unit, list_price_usd, window_start, window_end[, source]}
`source` is optional: the collectors that exist today do not send it. Sources that are not metered yet
(CloudWatch ListMetrics, GetMetricStatistics, UI/discovery reads) are reported as "not metered yet", never as zero.

list_price_usd is a CEILING: it ignores free tiers and is not the invoice. No price is hard-coded here: the
figure is whatever the collector stored when it flushed.

Counters are per process and are summed here. Each row is a delta for one flush window, so summing is correct,
but counts held in memory when a process restarts before its hourly flush are lost (an undercount), and rows
carry no process id, so overlapping windows from two processes cannot be told apart. The response says so.

Auth: this router only requires a signed-in user (Depends(get_current_user)). Phase E adds
require_permission("usage.view") when it registers the router in app/main.py.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import get_current_user
from app.db import get_connection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/usage", tags=["Usage"])

ALLOWED_DAYS = (1, 7, 30)
MAX_ROWS = 100000
FLUSH_INTERVAL_MINUTES = 60              # api_usage.FLUSH_INTERVAL_SECONDS
FLUSH_GAP_FACTOR = 2                     # no flush for 2 intervals = delayed
FREE_TIERS = {"tag_sync": "Resource Groups Tagging API: free"}
UNITS = {"aws": "metrics_requested", "azure": "api_calls", "gcp": "time_series_returned"}
# Monthly free allowance, per the project's cost decisions (see api_usage.py): Azure counts calls per subscription,
# GCP counts series per billing account. Both are monthly (calendar month).
FREE_ALLOWANCE = {
    "azure": {"per": "subscription", "unit": "api_calls", "limit": 1_000_000},
    "gcp": {"per": "billing account", "unit": "time_series_returned", "limit": 1_000_000},
}
# Paid reads and how a metered row proves each is metered. A call kind is "metered" once any row carries a
# `source` containing the key (case and punctuation ignored). Only the first group is metered by today's collectors.
PAID_CALL_KINDS = [
    {"provider": "aws", "call": "CloudWatch GetMetricData", "key": None, "metered_today": True},
    {"provider": "aws", "call": "CloudWatch ListMetrics", "key": "listmetrics", "metered_today": False},
    {"provider": "aws", "call": "CloudWatch GetMetricStatistics", "key": "getmetricstatistics", "metered_today": False},
    {"provider": "azure", "call": "Azure Monitor metrics", "key": None, "metered_today": True},
    {"provider": "gcp", "call": "Cloud Monitoring ListTimeSeries", "key": None, "metered_today": True},
]


def _norm(s):
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f and f not in (float("inf"), float("-inf")) and f > 0 else 0.0


def parse_detail(raw):
    """op_events.detail (JSON string, dict, bytes or None) -> dict or None. Never raises."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def aggregate(rows, now=None):
    """rows: iterable of op_events.detail values. Returns (providers, meta).
    providers: {provider: {"calls","units","list_price_usd","tiers": {(tier, source): {...}}}}.
    meta: row counts, last window_end, distinct flush hours. Rows that are not usable are counted, not dropped silently."""
    providers = {}
    used = skipped = 0
    last_end = None
    hours = set()
    sources = set()
    for raw in rows:
        d = parse_detail(raw)
        if not d or not d.get("provider"):
            skipped += 1
            continue
        provider = str(d["provider"]).strip().lower()
        tier = str(d.get("tier") or "unknown")
        source = d.get("source")
        source = str(source) if source else None
        calls, units, price = int(_num(d.get("calls"))), int(_num(d.get("units"))), _num(d.get("list_price_usd"))
        p = providers.setdefault(provider, {"calls": 0, "units": 0, "list_price_usd": 0.0, "tiers": {}})
        t = p["tiers"].setdefault((tier, source), {"calls": 0, "units": 0, "list_price_usd": 0.0, "rows": 0})
        for bucket in (p, t):
            bucket["calls"] += calls
            bucket["units"] += units
            bucket["list_price_usd"] += price
        t["rows"] += 1
        used += 1
        if source:
            sources.add(source)
        end = d.get("window_end")
        if isinstance(end, (int, float)) and end > 0:
            last_end = end if last_end is None else max(last_end, end)
            hours.add(int(end) // 3600)
    return providers, {"rows_used": used, "rows_skipped": skipped, "last_window_end": last_end,
                       "flush_hours": hours, "sources": sources}


def freshness(meta, days, now=None):
    now = time.time() if now is None else now
    last = meta["last_window_end"]
    if last is None:
        return {"status": "none", "last_flush_at": None, "minutes_since": None,
                "expected_interval_minutes": FLUSH_INTERVAL_MINUTES, "hours_with_flush": 0,
                "hours_in_window": days * 24, "rows_used": meta["rows_used"], "rows_skipped": meta["rows_skipped"]}
    minutes = max(0, int((now - last) // 60))
    status = "delayed" if minutes > FLUSH_INTERVAL_MINUTES * FLUSH_GAP_FACTOR else "ok"
    first_hour = min(meta["flush_hours"]) if meta["flush_hours"] else int(last) // 3600
    span = min(days * 24, int(now) // 3600 - first_hour + 1)
    in_window = {h for h in meta["flush_hours"] if h > int(now) // 3600 - days * 24}
    return {"status": status, "last_flush_at": datetime.fromtimestamp(last, tz=timezone.utc).isoformat(),
            "minutes_since": minutes, "expected_interval_minutes": FLUSH_INTERVAL_MINUTES,
            "hours_with_flush": len(in_window), "hours_in_window": max(1, span),
            "rows_used": meta["rows_used"], "rows_skipped": meta["rows_skipped"]}


def free_allowance_position(provider, month_units, now=None):
    """Position of month-to-date usage against ONE free allowance. None for providers without one.
    Usage is summed over all accounts: with N subscriptions / billing accounts the real allowance is N x the limit."""
    spec = FREE_ALLOWANCE.get(provider)
    if not spec:
        return None
    now = time.time() if now is None else now
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = start.replace(year=start.year + (start.month == 12), month=1 if start.month == 12 else start.month + 1)
    elapsed = max(1.0, (dt - start).total_seconds())
    total = (nxt - start).total_seconds()
    units = int(month_units)
    projected = int(units * total / elapsed)
    limit = spec["limit"]
    return {"per": spec["per"], "unit": spec["unit"], "limit": limit, "month_to_date": units,
            "percent_used": round(100.0 * units / limit, 2), "projected_month_end": projected,
            "projected_percent": round(100.0 * projected / limit, 2),
            "over_single_allowance": units > limit,
            "month_start": start.date().isoformat(),
            "caveat": f"Summed across all accounts. The allowance is per {spec['per']}, so with N of them it is N x {limit:,}."}


def env_polling_state():
    from app.collector import env_polling as ep
    on = ep.enabled()
    requested, classification = ep.requested(), ep.classification_enabled()
    if on:
        note = ("ON: UAT non-critical metrics are polled at a multiple of their base interval and Dev non-critical "
                "metrics hourly (critical-class metrics are slowed less or not at all). Prod, unknown and ignore "
                "are unchanged, so paid calls fall only for UAT and Dev resources.")
    elif requested and not classification:
        note = ("ENV_POLLING_ENABLED is set but ENV_AWARE_CLASSIFICATION is off, so it has no effect: every "
                "resource is polled at its base interval.")
    else:
        note = "OFF: every resource is polled at its base interval, whatever its environment."
    return {"enabled": on, "requested": requested, "classification_enabled": classification, "note": note,
            "scope": "Read from the API process's environment. The collector reads its own; they match when both "
                     "load the same .env."}


def build_report(rows, days, month_rows=(), now=None):
    now = time.time() if now is None else now
    providers, meta = aggregate(rows)
    month_providers, _ = aggregate(month_rows)
    out = []
    all_calls = 0
    all_price = 0.0
    for name in sorted(providers):
        p = providers[name]
        tiers = []
        for (tier, source), t in sorted(p["tiers"].items(), key=lambda kv: -kv[1]["units"]):
            tiers.append({"tier": tier, "source": source, "calls": t["calls"], "units": t["units"],
                          "list_price_usd": round(t["list_price_usd"], 4), "rows": t["rows"],
                          "free": tier in FREE_TIERS, "free_reason": FREE_TIERS.get(tier),
                          "share_of_units_pct": round(100.0 * t["units"] / p["units"], 1) if p["units"] else 0.0})
        allowance = free_allowance_position(name, month_providers.get(name, {}).get("units", 0), now)
        out.append({"provider": name, "known_provider": name in UNITS, "unit": UNITS.get(name, "units"),
                    "calls": p["calls"], "units": p["units"], "list_price_usd": round(p["list_price_usd"], 4),
                    "tiers": tiers, "free_allowance": allowance})
        all_calls += p["calls"]
        all_price += p["list_price_usd"]
    seen = {_norm(s) for s in meta["sources"]}
    not_metered = [k["call"] for k in PAID_CALL_KINDS
                   if not k["metered_today"] and not any(k["key"] in s for s in seen)]
    fresh = freshness(meta, days, now)
    notes = [
        "List-price estimate, not the invoice: free tiers are ignored and prices are whatever the collector stored.",
        "Units differ by cloud (metrics requested, API calls, series returned), so they are never added across clouds.",
        "Counters are per process and summed here. Counts in memory when a process restarts before its hourly flush "
        "are lost, so a restart can only make a figure too low.",
    ]
    if fresh["status"] == "delayed":
        notes.append("No usage flush for over two intervals: the newest figures are missing. Check the collector.")
    if meta["rows_skipped"]:
        notes.append(f"{meta['rows_skipped']} usage row(s) could not be read and were left out.")
    return {"days": days, "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "label": "List-price estimate, not the invoice",
            "totals": {"calls": all_calls, "list_price_usd": round(all_price, 4), "providers": len(out)},
            "providers": out, "freshness": fresh, "not_metered": not_metered,
            "env_polling": env_polling_state(), "notes": notes}


def rows_since(rows, start_epoch):
    """Keep rows whose flush window ended on or after start_epoch (the SQL range can reach a day into the previous month)."""
    keep = []
    for raw in rows:
        d = parse_detail(raw)
        end = d.get("window_end") if d else None
        if isinstance(end, (int, float)) and end >= start_epoch:
            keep.append(raw)
    return keep


def _fetch(days_back, cur):
    cur.execute(
        "SELECT detail FROM op_events WHERE event_type = 'metric_api_usage' "
        "AND created_at >= UTC_TIMESTAMP() - INTERVAL %s DAY ORDER BY id DESC LIMIT %s",
        (int(days_back), MAX_ROWS + 1))
    rows = [r[0] if not isinstance(r, dict) else r.get("detail") for r in cur.fetchall()]
    return rows[:MAX_ROWS], len(rows) > MAX_ROWS


@router.get("/cloud-cost")
def cloud_cost(days: int = Query(7), current_user: dict = Depends(get_current_user)):
    if days not in ALLOWED_DAYS:
        raise HTTPException(status_code=400, detail=f"days must be one of {list(ALLOWED_DAYS)}")
    now = time.time()
    month_start = datetime.fromtimestamp(now, tz=timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_days = int((now - month_start.timestamp()) // 86400) + 1
    conn = get_connection()
    cur = conn.cursor()
    try:
        rows, truncated = _fetch(days, cur)
        month_rows, month_truncated = (rows, truncated) if month_days <= days else _fetch(month_days, cur)
    except Exception:
        logger.exception("cloud-cost query failed")
        raise HTTPException(status_code=500, detail="Could not read usage data")
    finally:
        cur.close()
        conn.close()
    month_rows = rows_since(month_rows, month_start.timestamp())
    report = build_report(rows, days, month_rows, now)
    if truncated or month_truncated:
        report["notes"].append(f"More than {MAX_ROWS:,} usage rows in range: only the newest are included, totals are too low.")
    return report
