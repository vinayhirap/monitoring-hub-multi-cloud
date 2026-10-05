# app/threshold_effective.py
"""
The limits that are actually IN FORCE, for the Settings page, alert text and reports.

Three modes exist in app/collector/alert_evaluator.py and only one of them is "the numbers in the box":

  static    the warning / critical typed on the card are the limits.
  dynamic   use_dynamic=1: every resource gets its own limit, learned from its own history for the current hour of the
            week (mean +/- k*stddev, blended toward the typed values until a bucket has 20 samples, then guard-railed).
            The typed numbers are only the cold-start fallback.
  anomaly   the typed numbers are the 1,000,000 / 5,000,000 PLACEHOLDER: nothing is enforced against them. The line is
            learned per resource (mean + k*stddev, at least 1.5x the mean) and capped at WARNING.

Settings showed the typed numbers for all three, so a card could read "Warn 1000000" while alerts fired at 1,116, and an RCA
report quoted a limit that appeared nowhere in Settings. This module computes, from ONE batched query and the evaluator's
own functions (no second copy of the formula), what each resource's limit is right now.

It is read-only. Nothing here changes how alerts are evaluated.
"""
import logging
import statistics
import time

from app.threshold_defaults import (
    alert_floor, integerize_limit, is_integer_metric, is_placeholder_threshold, is_static_only_metric,
    resolve_db_metric_name,
)

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 30
_cache: dict = {}            # account_id -> (expires_monotonic, payload)
_MAX_RESOURCES_LISTED = 6


def invalidate(account_id=None):
    """Drop cached results after a threshold is saved / toggled, so the page never reads its own edit back stale."""
    if account_id is None:
        _cache.clear()
    else:
        _cache.pop(account_id, None)


def threshold_mode(row: dict, db_metric_name: str) -> str:
    """'anomaly' | 'dynamic' | 'static': the same decision the evaluator makes for this row."""
    if is_placeholder_threshold(row.get("warning_value"), row.get("critical_value"), row.get("comparison")):
        return "anomaly"
    if row.get("use_dynamic") and not is_static_only_metric(db_metric_name):
        return "dynamic"
    return "static"


def _spread(values):
    if not values:
        return None
    return {"min": min(values), "median": statistics.median(values), "max": max(values)}


def _apply_floor(warning, critical, comparison, db_metric_name):
    """The evaluator lifts '>' limits to the absolute size floor after choosing them (alert_floor)."""
    floor = alert_floor(db_metric_name)
    if floor and comparison in (">", ">=") and warning is not None:
        warning = max(float(warning), floor)
        critical = max(float(critical), floor) if critical is not None else None
    return warning, critical


def limits_for_resource(ev, row: dict, db_metric_name: str, bucket):
    """(warning, critical) in force for ONE resource from its current-bucket (mean, stddev, samples), or None when no limit
    applies yet (cold start, flat line, low confidence). `ev` is the alert_evaluator module (passed in so this file imports
    nothing heavy and tests can hand in the real functions)."""
    mode = threshold_mode(row, db_metric_name)
    comparison = row.get("comparison") or ">"
    k = row.get("dynamic_k") or 3.0
    whole = is_integer_metric(row.get("unit"), db_metric_name)       # count metrics: whole-number limits, as in the evaluator
    if mode == "anomaly":
        line = ev._anomaly_only_bound(None, None, None, db_metric_name, k, bucket=bucket)
        if line is None:
            return None
        if whole:
            line = integerize_limit(line, comparison)
        return _apply_floor(line, line, comparison, db_metric_name)
    if mode == "dynamic":
        dyn = ev._dynamic_bounds(None, None, None, db_metric_name, comparison, k,
                                 static_warning=row.get("warning_value"), static_critical=row.get("critical_value"),
                                 bucket=bucket)
        if dyn is None:
            return None
        w, c = ev.clamp_dynamic_bounds(dyn[0], dyn[1], row["warning_value"], row["critical_value"], comparison, row.get("unit"))
        if whole:
            w, c = integerize_limit(w, comparison), integerize_limit(c, comparison)
        return _apply_floor(w, c, comparison, db_metric_name)
    return None


def _load(cursor, account_id):
    cursor.execute("""
        SELECT t.id, t.resource_type, t.metric_id, t.warning_value, t.critical_value, t.comparison, t.enabled,
               t.use_dynamic, t.dynamic_k, mc.metric_name, mc.unit
        FROM thresholds t LEFT JOIN metric_catalog mc ON mc.id = t.metric_id
        WHERE t.aws_account_id = %s
    """, (account_id,))
    thresholds = cursor.fetchall()
    cursor.execute("SELECT resource_id, resource_type, name FROM resources WHERE aws_account_id = %s", (account_id,))
    resources = {r["resource_id"]: r for r in cursor.fetchall()}
    cursor.execute("""
        SELECT resource_id, metric_name, mean_value, stddev_value, sample_count, updated_at
        FROM metric_baseline
        WHERE aws_account_id = %s AND hour_of_day = HOUR(UTC_TIMESTAMP()) AND day_of_week = WEEKDAY(UTC_TIMESTAMP())
    """, (account_id,))
    baselines = {}
    for b in cursor.fetchall():
        baselines[(b["resource_id"], (b["metric_name"] or "").lower())] = b
    cursor.execute("SELECT HOUR(UTC_TIMESTAMP()) AS h, WEEKDAY(UTC_TIMESTAMP()) AS d")
    now = cursor.fetchone() or {}
    return thresholds, resources, baselines, now


def compute_effective(cursor, account_id, ev) -> dict:
    """{"as_of": {hour, weekday}, "limits": {threshold_id: {...}}} for every threshold row of the account."""
    thresholds, resources, baselines, now = _load(cursor, account_id)
    confident = getattr(ev, "CONFIDENT_SAMPLES", 20)
    out = {}
    for t in thresholds:
        if not t.get("metric_name"):
            continue
        db_name = resolve_db_metric_name(t["resource_type"], t["metric_name"])
        mode = threshold_mode(t, db_name)
        entry = {"mode": mode, "use_dynamic": bool(t.get("use_dynamic")), "dynamic_k": float(t.get("dynamic_k") or 3.0),
                 "enabled": bool(t.get("enabled")), "comparison": t.get("comparison") or ">",
                 "warning_value": t.get("warning_value"), "critical_value": t.get("critical_value"), "unit": t.get("unit")}
        if mode == "static":
            out[t["id"]] = entry
            continue
        of_type = [r for r in resources.values() if r["resource_type"] == t["resource_type"]]
        warns, crits, listed, learning, newest = [], [], [], 0, None
        for r in of_type:
            b = baselines.get((r["resource_id"], db_name.lower()))
            if not b:
                continue
            bucket = (b["mean_value"], b["stddev_value"] or 0, b.get("sample_count") or 0)
            if bucket[2] < confident:
                learning += 1
            lim = limits_for_resource(ev, t, db_name, bucket)
            if lim is None:
                continue
            warns.append(float(lim[0]))
            crits.append(float(lim[1]))
            if len(listed) < _MAX_RESOURCES_LISTED:
                listed.append({"name": r.get("name") or r["resource_id"], "warning": float(lim[0]),
                               "critical": float(lim[1]), "samples": int(bucket[2])})
            if b.get("updated_at") is not None and (newest is None or b["updated_at"] > newest):
                newest = b["updated_at"]
        entry.update({
            "resources_total": len(of_type), "with_limit": len(warns), "learning": learning,
            "warning": _spread(warns), "critical": _spread(crits), "resources": listed,
            "baseline_updated_at": newest.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(newest, "strftime") else None,
        })
        out[t["id"]] = entry
    return {"as_of": {"hour_utc": now.get("h"), "weekday": now.get("d")}, "limits": out}


def get_effective(cursor, account_id, ev) -> dict:
    hit = _cache.get(account_id)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    payload = compute_effective(cursor, account_id, ev)
    _cache[account_id] = (now + _CACHE_TTL_SECONDS, payload)
    return payload


def limit_kind_for(cursor, account_id, resource_type, metric_name):
    """'learned' | 'configured' | None for one alert's metric: how its limit is decided, for the RCA report wording."""
    try:
        cursor.execute("""
            SELECT t.warning_value, t.critical_value, t.comparison, t.use_dynamic, mc.metric_name
            FROM thresholds t JOIN metric_catalog mc ON mc.id = t.metric_id
            WHERE t.aws_account_id = %s AND t.resource_type = %s AND t.enabled = 1
        """, (account_id, resource_type))
        wanted = (metric_name or "").lower()
        for row in cursor.fetchall():
            db_name = resolve_db_metric_name(resource_type, row["metric_name"])
            if (db_name or "").lower() == wanted:
                return "configured" if threshold_mode(row, db_name) == "static" else "learned"
    except Exception as exc:
        logger.warning(f"[threshold_effective] limit kind lookup failed: {exc}")
    return None
