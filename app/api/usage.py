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
from app.auth.authorization import require_org_wide
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
        note = ("ON: NON-PROD resources are polled less often (non-critical metrics hourly, critical-class metrics "
                "every 10 minutes by default). PROD, and any resource not yet placed in an environment, is "
                "unchanged, so paid calls fall only for NON-PROD resources.")
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
    require_org_wide(current_user, "Billed cost")
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


# ── cost explorer: filter, group and graph the actual bill ──────────────────
EXPLORER_MAX_DAYS = 400
GROUP_BYS = ("category", "account", "region", "usage_type")
GRANULARITIES = ("daily", "monthly")
SCOPES = ("onboarded", "all")


def _csv_set(v):
    return {x.strip() for x in str(v or "").split(",") if x.strip()}


def build_explorer(rows, start, end, granularity="daily", group_by="category", accounts=(), regions=(), categories=(),
                   scope="onboarded", names=None, onboarded=None, now=None, cloudops_only=False, adapter=None):
    """The actual AWS CloudWatch bill, sliced like Cost Explorer. Pure: no database, no AWS.
    rows: (billed_account_id, usage_type, usage_date, amount_usd, estimated, usage_quantity, usage_unit) tuples.
    start/end: inclusive dates. onboarded: {billed_account_id: {region, ...}} (regions CloudOps monitors for that account).
    scope 'onboarded' keeps only charges in an account's onboarded regions; the rest is NOT dropped, it is totalled in
    `out_of_scope` so the page still reconciles to the AWS bill. Every figure is a Cost Explorer amount, nothing is
    estimated or forecast. Groups, periods and totals are all built from the same filtered rows (see `reconciliation`)."""
    from app.aws.cost_explorer import usage_region, usage_category, classify_usage_type, CATEGORY_LABELS, CAT_CLOUDOPS
    ad = adapter or {}
    usage_region = ad.get("region_of", usage_region)             # Azure/GCP rows carry 'region|sku'; AWS reads a usage-type prefix
    usage_category = ad.get("category_of", usage_category)
    classify_usage_type = ad.get("label_of", classify_usage_type)
    CATEGORY_LABELS = ad.get("category_labels", CATEGORY_LABELS)
    now = time.time() if now is None else now
    names = {str(k): v for k, v in (names or {}).items()}
    onboarded = {str(k): set(v) for k, v in (onboarded or {}).items()}
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()
    last_settled = today.fromordinal(today.toordinal() - PROVISIONAL_DAYS)
    accounts, regions, categories = set(accounts or ()), set(regions or ()), set(categories or ())
    if cloudops_only:
        # CloudOps-caused cost only: GetMetricData. Region scoping stays as asked (an account is onboarded in one region,
        # its default_region); what is billed elsewhere is not shown, only totalled in `out_of_scope` for the footnote.
        categories = {CAT_CLOUDOPS}
    excluded = 0.0
    opt_accts, opt_regions = {}, {}

    # periods
    span = [start.fromordinal(o) for o in range(start.toordinal(), end.toordinal() + 1)]
    if granularity == "monthly":
        keys = []
        for d in span:
            k = d.strftime("%Y-%m")
            if not keys or keys[-1] != k:
                keys.append(k)
        key_of = lambda d: d.strftime("%Y-%m")
    else:
        keys = [d.isoformat() for d in span]
        key_of = lambda d: d.isoformat()
    idx = {k: i for i, k in enumerate(keys)}
    last_day = {}
    for d in span:
        last_day[key_of(d)] = d
    periods = [{"key": k, "provisional": last_day[k] > last_settled} for k in keys]

    def gkey(acct, utype, region, cat):
        return {"category": cat, "account": acct, "region": region, "usage_type": utype}[group_by]

    groups, by_cat, out_regions = {}, {}, {}
    out_total = billed = 0.0
    raw_total = 0.0
    gmd_qty = [0.0] * len(keys)
    gmd_unit = None
    gmd_usd_with_qty = 0.0
    seen_regions, seen_accounts, dates_with_data, settled_total = set(), set(), set(), 0.0
    settled_dates = set()
    for acct, utype, d, amt, est, qty, unit in rows:
        d = _as_date(d)
        if d is None or d < start or d > end:
            continue
        acct, amt = str(acct), float(amt or 0)
        region, cat = usage_region(utype), usage_category(utype)
        raw_total += amt
        if categories and cat not in categories:
            if not accounts or acct in accounts:
                excluded += amt                       # in the accounts being looked at, but not what this view is about
            continue
        in_scope = scope != "onboarded" or acct not in onboarded or region in onboarded[acct]
        if not in_scope:                                  # a region this account is not onboarded in: not shown, only totalled
            if not accounts or acct in accounts:
                out_total += amt
                out_regions[region] = out_regions.get(region, 0.0) + amt
            continue
        seen_accounts.add(acct)
        seen_regions.add(region)
        opt_accts[acct] = opt_accts.get(acct, 0.0) + amt
        opt_regions[region] = opt_regions.get(region, 0.0) + amt
        dates_with_data.add(d)
        if accounts and acct not in accounts:
            continue
        if regions and region not in regions:
            continue
        i = idx[key_of(d)]
        g = groups.setdefault(gkey(acct, utype, region, cat), {"values": [0.0] * len(keys), "total": 0.0, "qty": 0.0,
                                                              "unit": None, "units": set(), "accounts": set(), "regions": set(), "categories": set()})
        g["values"][i] += amt
        g["total"] += amt
        g["accounts"].add(acct)
        g["regions"].add(region)
        g["categories"].add(cat)
        if qty is not None:
            g["qty"] += float(qty)
            g["unit"] = g["unit"] or unit
            g["units"].add(unit)
        billed += amt
        by_cat[cat] = by_cat.get(cat, 0.0) + amt
        if cat == CAT_CLOUDOPS and qty is not None:
            gmd_qty[i] += float(qty)
            gmd_unit = gmd_unit or unit
            gmd_usd_with_qty += amt
        if d <= last_settled:
            settled_total += amt
            settled_dates.add(d)

    def label(k):
        if group_by == "category":
            return CATEGORY_LABELS.get(k, k)
        if group_by == "account":
            return names.get(k) or f"AWS account {k}"
        if group_by == "usage_type":
            return f"{classify_usage_type(k)} \u00b7 {usage_region(k)}"      # one charge per region: name it, or they look identical
        return k

    out_groups = []
    for k, g in sorted(groups.items(), key=lambda kv: -kv[1]["total"]):
        out_groups.append({"key": k, "label": label(k), "total": round(g["total"], 4),
                           "share_pct": round(100.0 * g["total"] / billed, 1) if billed else 0.0,
                           "values": [round(v, 4) for v in g["values"]],
                           "quantity": round(g["qty"], 2) if g["unit"] and len(g["units"]) == 1 else None,     # never add Metrics to Requests to GB
                           "unit": g["unit"] if len(g["units"]) == 1 else None,
                           "accounts": sorted(g["accounts"]), "regions": sorted(g["regions"]), "categories": sorted(g["categories"])})
    metrics_requested = sum(gmd_qty)
    sums = {"groups": sum(g["total"] for g in groups.values()),
            "periods": sum(sum(g["values"]) for g in groups.values()),
            "categories": sum(by_cat.values())}
    drift = max(list(sums.values()) + [billed]) - min(list(sums.values()) + [billed])
    all_regions = sorted(seen_regions | {r for v in onboarded.values() for r in v})
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()}, "granularity": granularity, "group_by": group_by,
        "scope": scope, "periods": periods, "groups": out_groups,
        "totals": {"billed_usd": round(billed, 4),
                   "cloudops_usd": round(by_cat.get(CAT_CLOUDOPS, 0.0), 4),
                   "other_usd": round(billed - by_cat.get(CAT_CLOUDOPS, 0.0), 4),
                   "avg_per_settled_day_usd": round(settled_total / len(settled_dates), 4) if settled_dates else None,
                   "settled_days": len(settled_dates),
                   "metrics_requested": round(metrics_requested) if metrics_requested else None,
                   "usd_per_1000_metrics": round(1000.0 * gmd_usd_with_qty / metrics_requested, 5) if metrics_requested else None,
                   "usd_per_unit": round(gmd_usd_with_qty / metrics_requested, 8) if metrics_requested else None,
                   "unit": gmd_unit},
        "excluded": {"usd": round(excluded, 4), "applies": bool(cloudops_only)},
        "by_category": [{"key": k, "label": CATEGORY_LABELS.get(k, k), "usd": round(v, 4),
                         "share_pct": round(100.0 * v / billed, 1) if billed else 0.0}
                        for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])],
        "gmd_usage": {"unit": gmd_unit, "values": [round(v, 2) for v in gmd_qty]},
        "out_of_scope": {"usd": round(out_total, 4), "hidden": scope == "onboarded",
                         "by_region": [{"region": k, "usd": round(v, 4)} for k, v in sorted(out_regions.items(), key=lambda kv: -kv[1])]},
        "through_date": max(dates_with_data).isoformat() if dates_with_data else None,
        "complete_through": (min(max(dates_with_data), last_settled).isoformat() if dates_with_data else None),
        "options": {"accounts": [{"id": a, "name": names.get(a), "regions": sorted(onboarded.get(a, ())), "usd": round(opt_accts.get(a, 0.0), 4)}
                                 for a in sorted(seen_accounts | (set() if cloudops_only else set(onboarded)))],
                    "regions": all_regions if not cloudops_only else sorted(seen_regions),
                    "region_usd": {k: round(v, 4) for k, v in opt_regions.items()}, "categories": [{"key": k, "label": v} for k, v in CATEGORY_LABELS.items()]},
        "reconciliation": {"ok": drift <= 0.005, "max_difference_usd": round(drift, 6),
                           "in_scope_usd": round(billed, 4), "out_of_scope_usd": round(out_total, 4),
                           "selected_usd": round(billed + out_total, 4), "all_rows_usd": round(raw_total, 4)},
    }


def _fetch_state(log_rows, enabled, now, days=31):
    """Per-account refresh status + overall state + what the lookups cost (same rules as build_actual_report)."""
    from app.aws.cost_explorer import CE_PRICE_PER_REQUEST
    accounts, seen, reqs_total = [], set(), 0
    for pk, name, status, reqs, err, at in log_rows:
        if at is not None and at >= now - days * 86400:
            reqs_total += int(reqs or 0)
        if pk in seen:
            continue
        seen.add(pk)
        accounts.append({"account": name, "status": status, "ce_requests": int(reqs or 0), "error": err,
                         "fetched_at": datetime.fromtimestamp(at, tz=timezone.utc).isoformat() if at else None})
    states = {a["status"] for a in accounts}
    if not enabled:
        state = "disabled"
    elif not accounts:
        state = "waiting"
    elif "ok" in states:
        state = "ok"
    elif "permission_missing" in states:
        state = "permission_missing"
    elif "not_enabled" in states:
        state = "not_enabled"
    else:
        state = "error"
    return accounts, state, round(reqs_total * CE_PRICE_PER_REQUEST, 2), reqs_total


def _parse_day(v, default):
    if not v:
        return default
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid date: {v!r} (use YYYY-MM-DD)")


PROVIDER_NAMES = {"aws": "AWS", "azure": "Azure", "gcp": "GCP"}


def _active_providers(cur):
    """Providers that have at least one active account, AWS first. AWS is always offered (the page is built around it)."""
    cur.execute("SELECT DISTINCT provider FROM aws_accounts WHERE status = 'active'")
    have = {r[0] for r in cur.fetchall()}
    return [p for p in ("aws", "azure", "gcp") if p == "aws" or p in have]


@router.get("/cost-explorer")
def cost_explorer_report(start: str = Query(None), end: str = Query(None), granularity: str = Query("daily"),
                         group_by: str = Query("category"), accounts: str = Query(""), regions: str = Query(""),
                         categories: str = Query(""), scope: str = Query("onboarded"), cloudops_only: bool = Query(True),
                         provider: str = Query("aws"),
                         current_user: dict = Depends(require_permission("usage.manage"))):
    require_org_wide(current_user, "Billed cost")
    from app.aws import cost_explorer
    from app import cloud_cost
    if granularity not in GRANULARITIES or group_by not in GROUP_BYS or scope not in SCOPES or provider not in PROVIDER_NAMES:
        raise HTTPException(status_code=400, detail="granularity, group_by, scope or provider is not one of the allowed values")
    now = time.time()
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()
    end_d = _parse_day(end, today)
    start_d = _parse_day(start, today.fromordinal(today.toordinal() - 29))
    if end_d > today:
        end_d = today
    if start_d > end_d or (end_d - start_d).days + 1 > EXPLORER_MAX_DAYS:
        raise HTTPException(status_code=400, detail=f"range must be 1 to {EXPLORER_MAX_DAYS} days and start before end")
    is_aws = provider == "aws"
    conn = get_connection()
    cur = conn.cursor()
    adapter, onboarded, scope_note = None, {}, None
    try:
        providers = _active_providers(cur)
        if is_aws:
            try:
                cur.execute("SELECT billed_account_id, usage_type, usage_date, amount_usd, estimated, usage_quantity, usage_unit "
                            "FROM aws_cost_actuals WHERE usage_date BETWEEN %s AND %s", (start_d, end_d))
                rows = [tuple(r) for r in cur.fetchall()]
            except Exception as e:
                if "1054" in str(e) or "Unknown column" in str(e):          # migration 125 pending: cost only, no usage quantity
                    cur.execute("SELECT billed_account_id, usage_type, usage_date, amount_usd, estimated FROM aws_cost_actuals "
                                "WHERE usage_date BETWEEN %s AND %s", (start_d, end_d))
                    rows = [tuple(r) + (None, None) for r in cur.fetchall()]
                else:
                    raise
            cur.execute("SELECT aws_account_id, account_name, status, ce_requests, error, UNIX_TIMESTAMP(fetched_at) "
                        "FROM aws_cost_fetch_log WHERE fetched_at >= UTC_TIMESTAMP() - INTERVAL 31 DAY ORDER BY id DESC LIMIT 2000")
            log = [tuple(r) for r in cur.fetchall()]
            cur.execute("SELECT account_id, account_name FROM aws_accounts WHERE provider = 'aws'")
            names = {str(r[0]): r[1] for r in cur.fetchall()}
            try:
                cur.execute("SELECT account_id, default_region FROM aws_accounts WHERE provider = 'aws' AND status = 'active'")
                for acct, default_region in cur.fetchall():
                    if default_region:                    # discovery and polling use only this region, so this is where CloudOps is onboarded
                        onboarded.setdefault(str(acct), set()).add(default_region)
            except Exception:
                logger.exception("cost-explorer: could not read onboarded regions")
                onboarded, scope_note = {}, "Onboarded regions could not be read, so every region is shown."
            lookup_per_request = cost_explorer.CE_PRICE_PER_REQUEST
        else:
            cur.execute("SELECT billed_account, region, sku, usage_date, amount_usd, quantity, unit FROM cloud_cost_actuals "
                        "WHERE provider = %s AND usage_date BETWEEN %s AND %s", (provider, start_d, end_d))
            rows = [(r[0], cloud_cost.encode_usage_type(r[1], r[2]), r[3], r[4], 0, r[5], r[6]) for r in cur.fetchall()]
            cur.execute("SELECT aws_account_id, account_name, status, requests, error, UNIX_TIMESTAMP(fetched_at) "
                        "FROM cloud_cost_fetch_log WHERE provider = %s AND fetched_at >= UTC_TIMESTAMP() - INTERVAL 31 DAY "
                        "ORDER BY id DESC LIMIT 2000", (provider,))
            log = [tuple(r) for r in cur.fetchall()]
            cur.execute("SELECT COALESCE(subscription_id, project_id, account_id), account_name FROM aws_accounts WHERE provider = %s", (provider,))
            names = {str(r[0]): r[1] for r in cur.fetchall()}
            adapter = cloud_cost.explorer_adapter(provider)
            lookup_per_request = 0.0
            cur.execute("SELECT COALESCE(SUM(check_cost_usd), 0) FROM cloud_cost_fetch_log WHERE provider = %s "
                        "AND fetched_at >= UTC_TIMESTAMP() - INTERVAL 31 DAY", (provider,))
            check_cost_total = float(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM aws_accounts WHERE provider = %s AND status = 'active'", (provider,))
            n_accounts = int(cur.fetchone()[0] or 0)
    except Exception as e:
        if "doesn't exist" in str(e) or "1146" in str(e):
            return {"state": "migration_pending", "enabled": cost_explorer.enabled(), "providers": ["aws"],
                    "notes": ["Run the pending migrations (migrate.py apply --all-pending): 111/125 for AWS, 126 for Azure and GCP."]}
        logger.exception("cost-explorer query failed")
        raise HTTPException(status_code=500, detail="Could not read actual cost data")
    finally:
        cur.close()
        conn.close()
    rep = build_explorer(rows, start_d, end_d, granularity, group_by, _csv_set(accounts), _csv_set(regions),
                         _csv_set(categories), scope, names, onboarded, now, cloudops_only=cloudops_only, adapter=adapter)
    accts, state, lookup_usd, lookup_n = _fetch_state(log, cost_explorer.enabled(), now)
    if not is_aws:
        lookup_usd = round(check_cost_total, 4)
        if n_accounts == 0:
            state = "no_accounts"
    notes = ["Every amount is what the cloud billed for CloudOps's own paid metric calls. Nothing here is estimated or forecast.",
             "Billing data lags real usage by up to about 48 hours: today and yesterday are provisional and can still grow.",
             "Tax is billed separately and is not included."]
    if provider == "gcp":
        notes.append("GCP cost comes from the BigQuery billing export (GCP_BILLING_EXPORT_TABLE). It only covers days after the export was switched on.")
    rep.update({"state": state, "enabled": cost_explorer.enabled(), "provider": provider, "providers": providers,
                "provider_name": PROVIDER_NAMES[provider], "accounts": accts, "lookup_cost_usd": lookup_usd,
                "lookup_requests": lookup_n, "scope_note": scope_note, "notes": notes})
    if not rep["reconciliation"]["ok"]:
        rep["notes"].append(f"The breakdowns do not add up to the same total (differ by {rep['reconciliation']['max_difference_usd']} USD). Do not rely on them until this is checked.")
    return rep


@router.post("/actual-cost/refresh")
def actual_cost_refresh(current_user: dict = Depends(require_permission("usage.manage"))):
    require_org_wide(current_user, "Billed cost")
    """Ask AWS now (Cost Explorer bills every request). At most one refresh per account per hour."""
    from app.aws import cost_explorer
    if not cost_explorer.enabled():
        raise HTTPException(status_code=409, detail="Actual cost is off. Set ACTUAL_COST_ENABLED=1 and restart.")
    from app import cloud_cost
    results = cost_explorer.refresh_due(force=True) + cloud_cost.refresh_due(force=True)
    return {"results": results,
            "message": None if results else "Nothing refreshed: every account was refreshed within the last hour, or a refresh is already running."}


# ── cost by polling tier (why CloudOps costs what it does, and where to cut) ─────────────────────────────────────────
# Source: the same metered op_events rows as /cloud-cost, so this is a LIST-PRICE ESTIMATE of CloudOps's own reads, not
# the invoice. The savings figure is arithmetic, not a forecast: the number of paid calls a tier makes is inversely
# proportional to its polling interval, so polling half as often removes half of that tier's calls.
USER_TRIGGERED_TIERS = ("ui", "discovery")


def _tier_label(tier):
    return {"critical": "Critical (2 min)", "standard": "Standard (5 min)", "low": "Low (15 min)",
            "extended": "Extended (hourly)", "slow_extended": "Daily", "tag_sync": "Tag sync",
            "ui": "Pages opened by users", "discovery": "Metric discovery"}.get(tier, tier)


def build_tier_cost(rows, days, env_polling=None, now=None):
    """Estimated CloudOps cost per (cloud, polling tier). Pure: no database. `env_polling` is env_polling_state()'s dict."""
    now = time.time() if now is None else now
    providers, meta = aggregate(rows)
    lines = {}
    for name, p in providers.items():
        for (tier, source, operation), t in p["tiers"].items():
            ln = lines.setdefault((name, tier), {"provider": name, "tier": tier, "usd": 0.0, "units": 0, "calls": 0,
                                                 "request_units": 0, "unpriced_units": 0, "operations": set()})
            ln["calls"] += t["calls"]
            ln["unpriced_units"] += t["unpriced_units"]
            if operation:
                ln["operations"].add(operation)
            if t["is_request"]:
                ln["request_units"] += t["units"]                 # ListMetrics: requests, never added to metrics requested
            else:
                ln["units"] += t["units"]
                ln["usd"] += t["list_price_usd"]
    total = sum(ln["usd"] for ln in lines.values())
    out = []
    for ln in sorted(lines.values(), key=lambda x: (-x["usd"], x["provider"], x["tier"])):
        free = ln["tier"] in FREE_TIERS
        polling = ln["tier"] not in USER_TRIGGERED_TIERS and not free
        usd = round(ln["usd"], 4)
        out.append({"provider": ln["provider"], "tier": ln["tier"], "label": _tier_label(ln["tier"]),
                    "usd": usd, "share_pct": round(100.0 * ln["usd"] / total, 1) if total else 0.0,
                    "units": ln["units"], "unit": UNITS.get(ln["provider"], "units"), "calls": ln["calls"],
                    "request_units": ln["request_units"], "unpriced_units": ln["unpriced_units"],
                    "free": free, "user_triggered": ln["tier"] in USER_TRIGGERED_TIERS,
                    "halve_saving_usd": round(usd / 2.0, 4) if polling and usd > 0 else None})
    levers = []
    polling_lines = [x for x in out if x["halve_saving_usd"]]
    if polling_lines:
        top = polling_lines[0]
        levers.append({"kind": "largest_tier",
                       "text": (f"{top['label']} on {PROVIDER_NAMES.get(top['provider'], top['provider'])} is the largest line, "
                                f"{top['share_pct']}% of the estimate. Polling it half as often would remove about "
                                f"{top['halve_saving_usd']:.4f} USD over the same period (arithmetic, assuming cost follows call count).")})
    user_usd = sum(x["usd"] for x in out if x["user_triggered"])
    if user_usd > 0 and total:
        levers.append({"kind": "user_triggered",
                       "text": (f"{round(100.0 * user_usd / total, 1)}% of the estimate comes from reads that people trigger "
                                f"(pages opened, metric discovery), not from scheduled polling.")})
    if env_polling is not None and not env_polling.get("enabled") and polling_lines:
        levers.append({"kind": "env_polling_off",
                       "text": "Environment polling is OFF, so NON-PROD resources are polled as often as PROD. Turning it on "
                               "lowers only NON-PROD polling; PROD and unplaced resources are unchanged."})
    notes = ["List-price estimate of CloudOps's own metered reads, not the invoice. Free tiers are ignored, so Azure and GCP "
             "figures are a ceiling. Rows with no stored rate are counted as unpriced, not as zero.",
             "A tier's savings line is arithmetic (calls scale with 1 / polling interval), not a forecast of the bill."]
    fresh = freshness(meta, days, now)
    if fresh["status"] == "delayed":
        notes.append("No usage flush for over two intervals: the newest figures are missing.")
    return {"days": days, "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "label": "List-price estimate, not the invoice", "total_usd": round(total, 4), "tiers": out,
            "levers": levers, "freshness": fresh, "notes": notes}


@router.get("/polling-tiers")
def polling_tiers(days: int = Query(7), current_user: dict = Depends(get_current_user)):
    if days not in ALLOWED_DAYS:
        raise HTTPException(status_code=400, detail=f"days must be one of {list(ALLOWED_DAYS)}")
    conn = get_connection()
    cur = conn.cursor()
    try:
        rows, truncated = _fetch(days, cur)
    except Exception:
        logger.exception("polling-tiers query failed")
        raise HTTPException(status_code=500, detail="Could not read usage data")
    finally:
        cur.close()
        conn.close()
    try:
        env_state = env_polling_state()
    except Exception:
        logger.exception("polling-tiers: environment polling state unavailable")
        env_state = None
    report = build_tier_cost(rows, days, env_state)
    if truncated:
        report["notes"].append(f"More than {MAX_ROWS:,} usage rows in range: only the newest are included, totals are too low.")
    return report


# ── this month and last month (actual billed cost, no forecast) ──────────────────────────────────────────────────────
def _month_start(d):
    return d.replace(day=1)


def _daily_totals(rep):
    """{date: usd} summed over every group of a cost-explorer report built with daily granularity."""
    keys = [p["key"] for p in rep.get("periods", [])]
    sums = [0.0] * len(keys)
    for g in rep.get("groups", []):
        for i, v in enumerate(g.get("values", [])):
            if i < len(sums):
                sums[i] += float(v or 0)
    out = {}
    for k, v in zip(keys, sums):
        d = _as_date(k)
        if d is not None:
            out[d] = v
    return out


def build_monthly_totals(reports, today, names=None):
    """This month to date vs last month, per cloud and in total. Pure.
    reports: {provider: cost-explorer report (daily, CloudOps only, onboarded scope)} - the very figures the Cost page shows.
    Nothing is forecast: last month is complete, this month is whatever the cloud has billed so far, and
    `same_period_last_month` repeats last month over the same number of days so the two can be compared like for like."""
    names = names or PROVIDER_NAMES
    this_start = _month_start(today)
    last_end = this_start.fromordinal(this_start.toordinal() - 1)
    last_start = _month_start(last_end)
    provs, total_this = [], 0.0
    total_last = total_same = 0.0
    n_ok = 0
    for p, rep in reports.items():
        state = rep.get("state")
        if state in ("disabled", "migration_pending", "no_accounts", "error") or "periods" not in rep:
            provs.append({"provider": p, "name": names.get(p, p), "available": False, "state": state})
            continue
        daily = _daily_totals(rep)
        this_usd = sum(v for d, v in daily.items() if d >= this_start)
        last_usd = sum(v for d, v in daily.items() if last_start <= d <= last_end)
        with_data = sorted(d for d, v in daily.items() if v > 0)
        first = with_data[0] if with_data else None
        through = _as_date(rep.get("through_date"))
        n = through.day if through and through >= this_start else 0
        n = min(n, last_end.day)
        same = sum(v for d, v in daily.items() if last_start <= d <= last_end and d.day <= n)
        provs.append({"provider": p, "name": names.get(p, p), "available": True, "state": state,
                      "this_month_usd": round(this_usd, 4), "last_month_usd": round(last_usd, 4),
                      "same_period_last_month_usd": round(same, 4) if n else None,
                      "through_date": rep.get("through_date"),
                      "last_month_partial": bool(first and first > last_start),
                      "data_starts": first.isoformat() if first else None})
        total_this += this_usd
        total_last += last_usd
        total_same += same
        n_ok += 1
    any_partial = any(x.get("last_month_partial") for x in provs)
    return {"as_of": today.isoformat(), "currency": "USD",
            "this_month": {"label": this_start.strftime("%Y-%m"), "start": this_start.isoformat(), "usd": round(total_this, 4),
                           "provisional_days": PROVISIONAL_DAYS},
            "last_month": {"label": last_start.strftime("%Y-%m"), "start": last_start.isoformat(), "end": last_end.isoformat(),
                           "usd": round(total_last, 4), "partial_coverage": any_partial},
            "same_period_last_month_usd": round(total_same, 4) if n_ok else None,
            "providers": provs,
            "notes": ["Actual billed cost of CloudOps's own paid metric calls, the same figures as the cost chart above. No forecast: "
                      "this month is only what has been billed so far.",
                      f"The last {PROVISIONAL_DAYS} days are provisional and can still grow, so this month's total can rise after you read it.",
                      "Tax is billed separately and is not included.",
                      "Months are UTC calendar months, as the cloud billing data is reported."]
                     + (["Billing history for at least one cloud starts after the first day of last month, so last month is understated for it."]
                        if any_partial else [])}


@router.get("/monthly-totals")
def monthly_totals(current_user: dict = Depends(require_permission("usage.manage"))):
    require_org_wide(current_user, "Billed cost")
    now = time.time()
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()
    last_end = _month_start(today).fromordinal(_month_start(today).toordinal() - 1)
    start = _month_start(last_end)
    conn = get_connection()
    cur = conn.cursor()
    try:
        provs = _active_providers(cur)
    except Exception:
        logger.exception("monthly-totals: could not list clouds")
        raise HTTPException(status_code=500, detail="Could not read actual cost data")
    finally:
        cur.close()
        conn.close()
    reports = {}
    for p in provs:
        try:                                             # exactly what the Cost page shows for this cloud, so the numbers match it
            reports[p] = cost_explorer_report(start=start.isoformat(), end=today.isoformat(), granularity="daily",
                                              group_by="category", accounts="", regions="", categories="", scope="onboarded",
                                              cloudops_only=True, provider=p, current_user=current_user)
        except HTTPException:
            logger.exception("monthly-totals: %s report failed", p)
            reports[p] = {"state": "error"}
    return build_monthly_totals(reports, today)
