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
require_permission("usage.view") when it registers the router in app/main.py. The two actual-charge endpoints
(GET /actual-cost, POST /actual-cost/refresh) additionally require usage.manage, which only the admin role holds
(migration 110): editors can see the estimate, only admins see or refresh what AWS actually billed.

ACTUAL COST: what AWS really billed for CloudWatch, read from Cost Explorer by app/aws/cost_explorer.py into
aws_cost_actuals. It is shown NEXT TO the estimate, never merged into it: Cost Explorer lags real usage by up to ~48h,
so the two only agree for days that are complete.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import get_current_user
from app.auth.permissions import require_permission
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
# `source` OR `operation` containing the key (case and punctuation ignored): Phase A writes the billed API in `operation`. Only the first group is metered by today's collectors.
# Operations whose `units` are REQUESTS, not metrics requested (CloudWatch ListMetrics bills per request). They are
# listed on their own rows and never added into the provider's metric units, share percentages or list-price total.
REQUEST_OPERATIONS = {"listmetrics"}

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
    providers: {provider: {"calls","units","list_price_usd","tiers": {(tier, source, operation): {...}}}}.
    meta: row counts, last window_end, distinct flush hours. Rows that are not usable are counted, not dropped silently."""
    providers = {}
    used = skipped = 0
    last_end = None
    hours = set()
    sources = set()
    operations = set()
    for raw in rows:
        d = parse_detail(raw)
        if not d or not d.get("provider"):
            skipped += 1
            continue
        provider = str(d["provider"]).strip().lower()
        tier = str(d.get("tier") or "unknown")
        source = d.get("source")
        source = str(source) if source else None
        operation = d.get("operation")
        operation = str(operation) if operation else None
        is_request = _norm(operation) in REQUEST_OPERATIONS
        calls, units, price = int(_num(d.get("calls"))), int(_num(d.get("units"))), _num(d.get("list_price_usd"))
        unpriced = d.get("list_price_usd") is None and units > 0      # no rate stored: unknown, NOT zero
        p = providers.setdefault(provider, {"calls": 0, "units": 0, "request_units": 0, "list_price_usd": 0.0, "tiers": {}})
        t = p["tiers"].setdefault((tier, source, operation), {"calls": 0, "units": 0, "list_price_usd": 0.0, "rows": 0,
                                                              "is_request": is_request, "unpriced_units": 0})
        p["calls"] += calls
        t["calls"] += calls
        t["units"] += units
        t["list_price_usd"] += price
        if unpriced:
            t["unpriced_units"] += units
        if is_request:
            p["request_units"] += units
        else:
            p["units"] += units
            p["list_price_usd"] += price
        t["rows"] += 1
        if operation:
            operations.add(operation)
        used += 1
        if source:
            sources.add(source)
        end = d.get("window_end")
        if isinstance(end, (int, float)) and end > 0:
            last_end = end if last_end is None else max(last_end, end)
            hours.add(int(end) // 3600)
    return providers, {"rows_used": used, "rows_skipped": skipped, "last_window_end": last_end,
                       "flush_hours": hours, "sources": sources, "operations": operations}


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
        for (tier, source, operation), t in sorted(p["tiers"].items(), key=lambda kv: (kv[1]["is_request"], -kv[1]["units"])):
            tiers.append({"tier": tier, "source": source, "operation": operation, "calls": t["calls"], "units": t["units"],
                          "unit": "requests" if t["is_request"] else UNITS.get(name, "units"),
                          "list_price_usd": round(t["list_price_usd"], 4), "rows": t["rows"],
                          "priced": t["unpriced_units"] == 0,
                          "free": tier in FREE_TIERS, "free_reason": FREE_TIERS.get(tier),
                          "share_of_units_pct": (None if t["is_request"] else
                                                 round(100.0 * t["units"] / p["units"], 1) if p["units"] else 0.0)})
        allowance = free_allowance_position(name, month_providers.get(name, {}).get("units", 0), now)
        out.append({"provider": name, "known_provider": name in UNITS, "unit": UNITS.get(name, "units"),
                    "calls": p["calls"], "units": p["units"], "request_units": p["request_units"],
                    "list_price_usd": round(p["list_price_usd"], 4), "tiers": tiers, "free_allowance": allowance})
        all_calls += p["calls"]
        all_price += p["list_price_usd"]
    seen = {_norm(s) for s in meta["sources"]} | {_norm(o) for o in meta["operations"]}
    # A row that carries an `operation` comes from a collector that meters ListMetrics and GetMetricStatistics itself,
    # so they are metered even when none ran in this window (GetMetricStatistics only runs when someone opens a page).
    # Only data from before per-operation metering is judged by the older source/operation keys.
    per_operation_metering = bool(meta["operations"])
    not_metered = [k["call"] for k in PAID_CALL_KINDS
                   if not k["metered_today"] and not per_operation_metering and not any(k["key"] in s for s in seen)]
    fresh = freshness(meta, days, now)
    notes = [
        "List-price estimate, not the invoice, of CloudOps's own metered reads only (list price, free tiers ignored, prices as stored by the collector). "
        "It is NOT a ceiling for the AWS bill: custom metrics stored, logs, alarms and API requests from any other caller "
        "in the account are billed too and are not in this number. Rows with no stored rate are shown as unpriced, not as zero.",
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


# ── actual AWS charge (Cost Explorer) ───────────────────────────────────────
def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if hasattr(v, "year") and hasattr(v, "month") and hasattr(v, "day"):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


PROVISIONAL_DAYS = 2        # Cost Explorer lags real usage by up to ~48 h: today and the day before may still grow


def build_actual_report(actual_rows, log_rows, days, estimate_usd, enabled, now=None, account_names=None):
    """actual_rows: (billed_account_id, usage_type, usage_date, amount_usd, estimated) tuples.
    log_rows: (aws_account_id, account_name, status, ce_requests, error, fetched_at_epoch) newest first.
    account_names: {billed_account_id: name} from aws_accounts (optional); an unknown id is shown as the bare id.
    Pure: no database, no AWS. The window is the last `days` UTC dates including today.
    Every breakdown (by usage type, account, region, day) is built from the SAME filtered rows, and `reconciliation`
    proves it: the four totals must agree to the cent or the page says so."""
    from app.aws.cost_explorer import classify_usage_type, usage_region, CE_PRICE_PER_REQUEST
    now = time.time() if now is None else now
    names = {str(k): v for k, v in (account_names or {}).items()}
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()
    first = today.fromordinal(today.toordinal() - days + 1)
    last_settled = today.fromordinal(today.toordinal() - PROVISIONAL_DAYS)
    by_type, by_account, by_region, daily = {}, {}, {}, {}
    per_acct = {}
    complete_dates = set()
    for acct, utype, d, amt, est in actual_rows:
        d = _as_date(d)
        if d is None or d < first or d > today:
            continue
        acct, amt = str(acct), float(amt or 0)
        region = usage_region(utype)
        by_type[utype] = by_type.get(utype, 0.0) + amt
        by_account[acct] = by_account.get(acct, 0.0) + amt
        by_region[region] = by_region.get(region, 0.0) + amt
        day = daily.setdefault(d, {"usd": 0.0, "estimated": False})
        day["usd"] += amt
        day["estimated"] = day["estimated"] or bool(est)
        pa = per_acct.setdefault(acct, {"types": {}, "regions": {}, "days": {}})
        pa["types"][utype] = pa["types"].get(utype, 0.0) + amt
        pa["regions"][region] = pa["regions"].get(region, 0.0) + amt
        pa["days"][d] = pa["days"].get(d, 0.0) + amt
        if d <= last_settled:
            complete_dates.add(d)
    total = sum(by_type.values())
    latest_seen = max(daily) if daily else None
    through_complete = max(complete_dates) if complete_dates else None

    accounts, seen = [], set()
    ce_requests = 0
    for pk, name, status, reqs, err, at in log_rows:
        if at is not None and at >= now - days * 86400:
            ce_requests += int(reqs or 0)
        if pk in seen:
            continue
        seen.add(pk)
        accounts.append({"account": name, "status": status, "ce_requests": int(reqs or 0),
                         "error": err, "fetched_at": datetime.fromtimestamp(at, tz=timezone.utc).isoformat() if at else None})
    states = {a["status"] for a in accounts}
    if not enabled:
        state = "disabled"
    elif not accounts:
        state = "waiting"          # enabled, nothing fetched yet
    elif "ok" in states:
        state = "ok"
    elif "permission_missing" in states:
        state = "permission_missing"
    elif "not_enabled" in states:
        state = "not_enabled"
    else:
        state = "error"
    lag = (today - latest_seen).days if latest_seen else None

    accounts_cost = []
    for acct, usd_total in sorted(by_account.items(), key=lambda kv: -kv[1]):
        pa = per_acct[acct]
        accounts_cost.append({
            "billed_account_id": acct, "name": names.get(acct), "usd": round(usd_total, 4),
            "share_pct": round(100.0 * usd_total / total, 1) if total else 0.0,
            "by_usage_type": [{"usage_type": k, "label": classify_usage_type(k), "region": usage_region(k), "usd": round(v, 4)}
                              for k, v in sorted(pa["types"].items(), key=lambda kv: -kv[1])],
            "by_region": [{"region": k, "usd": round(v, 4)} for k, v in sorted(pa["regions"].items(), key=lambda kv: -kv[1])],
            "daily": [{"date": d.isoformat(), "usd": round(v, 4), "provisional": d > last_settled}
                      for d, v in sorted(pa["days"].items())]})
    sums = {"usage_type": sum(by_type.values()), "account": sum(by_account.values()),
            "region": sum(by_region.values()), "day": sum(v["usd"] for v in daily.values())}
    drift = max(sums.values()) - min(sums.values()) if sums else 0.0
    notes = ["Actual figures come from AWS Cost Explorer (service: CloudWatch, unblended cost, USD). They are what AWS "
             "billed for the WHOLE account, from every caller: CloudOps's own reads are only part of it.",
             "Cost Explorer lags real usage by up to about 48 hours, so today and yesterday are marked provisional and "
             "can still grow. Every day shown is also not yet invoiced until the month closes.",
             "Tax (for example GST) is a separate AWS line, not part of the CloudWatch service figure, so the invoice "
             "total is higher than the sum shown here."]
    if latest_seen and lag and lag > 2:
        notes.append(f"The newest Cost Explorer data is {lag} days old. Check the account's refresh status below.")
    if drift > 0.005:
        notes.append(f"The breakdowns do not add up to the same total (differ by {drift:.4f} USD). Do not rely on them until this is investigated.")
    return {"days": days, "state": state, "enabled": bool(enabled),
            "total_usd": round(total, 4), "estimate_usd": None if estimate_usd is None else round(estimate_usd, 4),
            "through_date": latest_seen.isoformat() if latest_seen else None,
            "complete_through": through_complete.isoformat() if through_complete else None,
            "window_start": first.isoformat(), "window_end": today.isoformat(),
            "by_usage_type": [{"usage_type": k, "label": classify_usage_type(k), "region": usage_region(k), "usd": round(v, 4)}
                              for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])],
            "by_account": [{"billed_account_id": k, "name": names.get(k), "usd": round(v, 4)} for k, v in sorted(by_account.items(), key=lambda kv: -kv[1])],
            "accounts_cost": accounts_cost,
            "by_region": [{"region": k, "usd": round(v, 4)} for k, v in sorted(by_region.items(), key=lambda kv: -kv[1])],
            "daily": [{"date": d.isoformat(), "usd": round(v["usd"], 4), "estimated": v["estimated"], "provisional": d > last_settled}
                      for d, v in sorted(daily.items())],
            "reconciliation": {"ok": drift <= 0.005, "max_difference_usd": round(drift, 6),
                               "totals": {k: round(v, 4) for k, v in sums.items()}},
            "accounts": accounts,
            "lookup_cost_usd": round(ce_requests * CE_PRICE_PER_REQUEST, 2), "lookup_requests": ce_requests,
            "setup_doc": "docs/AWS_ACTUAL_COST.md", "notes": notes}


def _estimate_aws_usd(rows, days, now):
    providers, _ = aggregate(rows)
    aws = providers.get("aws")
    return None if aws is None else aws["list_price_usd"]


@router.get("/actual-cost")
def actual_cost(days: int = Query(7), current_user: dict = Depends(require_permission("usage.manage"))):
    if days not in ALLOWED_DAYS:
        raise HTTPException(status_code=400, detail=f"days must be one of {list(ALLOWED_DAYS)}")
    from app.aws import cost_explorer
    now = time.time()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT billed_account_id, usage_type, usage_date, amount_usd, estimated FROM aws_cost_actuals "
                    "WHERE usage_date >= UTC_DATE() - INTERVAL %s DAY", (int(days) - 1,))   # days incl. today, as the report counts
        actual = [tuple(r) for r in cur.fetchall()]
        cur.execute("SELECT account_id, account_name FROM aws_accounts WHERE provider = 'aws'")
        names = {str(r[0]): r[1] for r in cur.fetchall()}
        cur.execute("SELECT aws_account_id, account_name, status, ce_requests, error, UNIX_TIMESTAMP(fetched_at) "
                    "FROM aws_cost_fetch_log WHERE fetched_at >= UTC_TIMESTAMP() - INTERVAL 31 DAY ORDER BY id DESC LIMIT 2000")
        log = [tuple(r) for r in cur.fetchall()]
        est_rows, _ = _fetch(days, cur)
    except Exception as e:
        if "doesn't exist" in str(e) or "1146" in str(e):
            return {"days": days, "state": "migration_pending", "enabled": cost_explorer.enabled(),
                    "notes": ["Run migration 111_aws_actual_cost.sql (migrate.py apply --all-pending)."]}
        logger.exception("actual-cost query failed")
        raise HTTPException(status_code=500, detail="Could not read actual cost data")
    finally:
        cur.close()
        conn.close()
    return build_actual_report(actual, log, days, _estimate_aws_usd(est_rows, days, now), cost_explorer.enabled(), now,
                               account_names=names)


@router.post("/actual-cost/refresh")
def actual_cost_refresh(current_user: dict = Depends(require_permission("usage.manage"))):
    """Ask AWS now (Cost Explorer bills every request). At most one refresh per account per hour."""
    from app.aws import cost_explorer
    if not cost_explorer.enabled():
        raise HTTPException(status_code=409, detail="Actual cost is off. Set ACTUAL_COST_ENABLED=1 and restart.")
    results = cost_explorer.refresh_due(force=True)
    return {"results": results,
            "message": None if results else "Nothing refreshed: every account was refreshed within the last hour, or a refresh is already running."}
