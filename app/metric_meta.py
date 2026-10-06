# app/metric_meta.py
"""
Per-resource chart metadata for ANY cloud/service: official title + unit,
native statistic and the stats the UI may offer, REAL polling cadence (from
polling_model.py), the CURRENT warning/critical lines (static, dynamic-baseline
or anomaly -- exactly as alert_evaluator.py resolves them) and which metrics
are firing an alert on this resource right now.

The frontend re-fetches this on every chart refresh, so a threshold edit in
Settings or a moving dynamic band reaches an open chart without a reload.
Every lookup is best-effort: a failure degrades to "no overlay", never a 500.
"""
import logging

from app import alert_rules
from app.db import get_db_cursor
from app.metric_display import (METRIC_HISTORY_RETENTION_DAYS, display_spec, fmt_interval,
                                polling_info, stats_available)
from app.threshold_defaults import (
    is_static_only_metric, is_placeholder_threshold, resolve_db_metric_name, is_integer_metric, integerize_limit,
)

logger = logging.getLogger(__name__)

_SEV_RANK = {"CRITICAL": 3, "WARNING": 2, "INFO": 1}


def _catalog_services(service):
    s = (service or "").lower()
    return ["alb", "nlb", "elb"] if s in ("alb", "nlb", "elb") else [s]


# Services whose CloudWatch metrics are published ONLY when something happens: a Lambda that was not invoked, an idle SQS queue
# (AWS stops publishing after ~6 h), an SNS topic nobody published to, a NAT gateway with no traffic. For these "no new datapoint"
# means "no activity", not "the collector is behind", so the UI says idle, never stale.
SPARSE_SERVICES = frozenset({"lambda", "sqs", "sns", "events", "apigateway", "wafv2", "cloudfront", "s3", "dynamodb", "kinesis",
                             "states", "logs", "backup", "natgateway"})
_ELB_SPARSE_PREFIXES = ("httpcode", "requestcount", "responselatency", "targetresponsetime", "errors4xx", "errors5xx",
                        "rejectedconnection", "targetconnectionerror", "newconnection", "activeconnection", "processedbytes")


def is_sparse_metric(service, metric_name) -> bool:
    svc, name = (service or "").lower(), (metric_name or "").lower()
    if svc in ("alb", "nlb", "elb"):
        return name.startswith(_ELB_SPARSE_PREFIXES)           # request-based ones only; HealthyHostCount etc. publish continuously
    return svc in SPARSE_SERVICES


def stale_after_seconds(interval_seconds):
    """Seconds a metric collected every `interval_seconds` may go without a new datapoint before it is late (None: unknown)."""
    try:
        from app.collector import polling_model as pm
        table = pm.STALE_MIN_BY_INTERVAL
        key = interval_seconds if interval_seconds in table else min(table, key=lambda k: abs(k - (interval_seconds or 0)))
        return int(table[key]) * 60
    except Exception:
        return None


_TIER_RUN_CACHE = {"at": 0.0, "rows": {}}


def tier_last_completed():
    """{tier: unix seconds of the last COMPLETED collection of that tier}. Written by the scheduler
    (scheduler._mark_tier_completed -> op_events 'scheduler_tier_<tier>_completed'). The newest datapoint's own
    timestamp says when the cloud provider measured it; this says when OUR collector last actually polled, which is the
    cadence the 'polled every N min' badge promises. Cached 20 s; best-effort ({} on any failure)."""
    import time
    now = time.time()
    if now - _TIER_RUN_CACHE["at"] < 20:
        return _TIER_RUN_CACHE["rows"]
    rows = {}
    try:
        with get_db_cursor(dictionary=True, commit=False) as (_c, cur):
            cur.execute("""SELECT event_type, UNIX_TIMESTAMP(MAX(created_at)) AS ts FROM op_events
                           WHERE event_type IN ('scheduler_tier_critical_completed','scheduler_tier_standard_completed',
                                                'scheduler_tier_low_completed','scheduler_tier_extended_completed',
                                                'scheduler_tier_slow_extended_completed')
                           GROUP BY event_type""")
            for r in cur.fetchall():
                if r["ts"] is not None:
                    rows[r["event_type"][len("scheduler_tier_"):-len("_completed")]] = float(r["ts"])
    except Exception as e:
        logger.debug(f"tier_last_completed failed: {e}")
        return _TIER_RUN_CACHE["rows"]
    _TIER_RUN_CACHE.update(at=now, rows=rows)
    return rows


def _effective_lines(cur, account_id, resource_id, row, db_name):
    """-> (warning, critical, mode) mirroring alert_evaluator's resolution."""
    warning, critical = row["warning_value"], row["critical_value"]
    comparison = row["comparison"]
    if warning is None or critical is None:
        return None, None, "none"
    try:
        from app.collector import alert_evaluator as ev
        if is_placeholder_threshold(warning, critical, comparison):
            line = ev._anomaly_only_bound(cur, account_id, resource_id, db_name, row.get("dynamic_k") or 3.0)
            if line is not None and is_integer_metric(row.get("unit"), db_name):
                line = integerize_limit(line, comparison)           # count metrics: whole-number line, same as the evaluator
            return (line, line, "anomaly") if line is not None else (None, None, "anomaly")
        if row.get("use_dynamic") and not is_static_only_metric(db_name):
            dyn = ev._dynamic_bounds(cur, account_id, resource_id, db_name, comparison,
                                     row.get("dynamic_k") or 3.0, static_warning=warning, static_critical=critical)
            if dyn is not None:
                w, c = ev.clamp_dynamic_bounds(dyn[0], dyn[1], warning, critical, comparison, row.get("unit"))
                if is_integer_metric(row.get("unit"), db_name):
                    w, c = integerize_limit(w, comparison), integerize_limit(c, comparison)
                return w, c, "dynamic"
    except Exception as e:                       # overlay only -- never fail the page
        logger.debug(f"effective threshold fallback to static [{resource_id}/{db_name}]: {e}")
    return float(warning), float(critical), "static"


def build_metric_meta(account_id, provider, service, resource_ids):
    provider = provider or "aws"
    services = _catalog_services(service)
    rids = [r for r in (resource_ids or []) if r]
    is_nlb = any("loadbalancer/net/" in r for r in rids)
    out, unmatched = {}, []
    tier_runs = tier_last_completed()
    with get_db_cursor(dictionary=True, commit=False) as (_c, cur):
        cur.execute(
            f"""SELECT mc.metric_name, mc.service, mc.unit, mc.statistic, mc.description,
                       t.resource_type, t.warning_value, t.critical_value, t.comparison,
                       t.enabled, t.use_dynamic, t.dynamic_k
                FROM metric_catalog mc
                LEFT JOIN thresholds t ON t.metric_id = mc.id AND t.aws_account_id = %s
                WHERE mc.provider = %s AND mc.service IN ({', '.join(['%s'] * len(services))})
                  AND mc.metric_name IS NOT NULL AND mc.metric_name <> ''""",
            [account_id, provider] + services)
        rows = cur.fetchall()

        # open alerts for this resource, keyed by lower db metric name
        alerts = {}
        if rids:
            try:
                for a in alert_rules.fetch_open_alert_rows(cur, [account_id]):
                    if a["resource_id"] in rids:
                        key = (a["metric_name"] or "").lower()
                        sev = (a["severity"] or "").upper()
                        cur_best = alerts.get(key)
                        firing = a["state"] == "firing"
                        rank = (1 if firing else 0, _SEV_RANK.get(sev, 0))
                        if cur_best is None or rank > cur_best["_rank"]:
                            alerts[key] = {"severity": sev, "state": a["state"], "_rank": rank}
            except Exception as e:
                logger.debug(f"metric-meta alerts lookup failed: {e}")

        matched = set()
        for r in rows:
            svc = (r["service"] or "").lower()
            if svc in ("alb", "nlb") and rids and ((svc == "nlb") != is_nlb):
                continue                          # other LB flavour's catalog row
            name = r["metric_name"]
            spec = display_spec(provider, svc, name, r.get("unit"), r.get("statistic"))
            rtype = r.get("resource_type") or ("elb" if svc in ("alb", "nlb") else svc)
            db_name = resolve_db_metric_name(rtype, name)
            poll = polling_info(provider, svc, name, db_name)
            entry = {
                "title": spec["title"], "metric_name": name, "unit": spec["unit"],
                "unit_symbol": spec["unit_symbol"], "native_stat": spec["native_stat"],
                "stats": stats_available(spec["native_stat"], spec["rate"]),
                "description": r.get("description"),
                "poll_seconds": poll["interval_seconds"], "poll_label": fmt_interval(poll["interval_seconds"]),
                # How old the newest datapoint may be before it counts as late: the SAME table the alert engine uses to
                # mark an alert's data stale (polling_model.STALE_MIN_BY_INTERVAL), so a chart and the alert engine can
                # never disagree about "fresh". The chart used to guess from the CloudWatch period alone.
                "stale_after_seconds": stale_after_seconds(poll["interval_seconds"]),
                "sparse": is_sparse_metric(svc, name),
                "period_seconds": poll.get("period_seconds"), "period_label": fmt_interval(poll.get("period_seconds")),
                "tier": poll.get("tier"), "poll_source": poll.get("source"),
                # When the collector LAST actually polled this metric's tier (unix s; None: not tracked, e.g. the 60 s describe poll).
                "last_collected_ts": tier_runs.get(poll.get("tier")),
                "threshold": None, "alert": None,
            }
            if r.get("warning_value") is not None and r.get("enabled"):
                w, c, mode = (_effective_lines(cur, account_id, rids[0], r, db_name)
                              if rids else (float(r["warning_value"]), float(r["critical_value"]), "static"))
                sc = spec["scale"]
                entry["threshold"] = {
                    "warning": None if w is None else round(w * sc, 6),
                    "critical": None if c is None else round(c * sc, 6),
                    "comparison": r["comparison"], "mode": mode,
                    "static_warning": round(float(r["warning_value"]) * sc, 6),
                    "static_critical": round(float(r["critical_value"]) * sc, 6),
                }
            al = alerts.get(db_name.lower())
            if al:
                entry["alert"] = {"severity": al["severity"], "state": al["state"]}
                matched.add(db_name.lower())
            out[name] = entry
        for k, al in alerts.items():
            base = k.split("__")[0]
            if k not in matched and base not in matched:
                unmatched.append({"metric": k, "severity": al["severity"], "state": al["state"]})
            elif k not in matched and base in matched:
                pass                              # per-mount variant: base card already flagged
    return {"metrics": out, "alerts_unmatched": unmatched,
            "retention_days": METRIC_HISTORY_RETENTION_DAYS}
