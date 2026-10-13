# app/collector/metrics/waf.py
"""
AWS WAFv2 metric collection -- Phase 3 (2026-10-07). Used by extended.collect_extended_for_account ONLY when
WAF_FAST_POLLING_ENABLED is on (polling_model.waf_fast_enabled()); with it off nothing here runs.

WHY A DEDICATED COLLECTOR instead of extended.py's generic loop
----------------------------------------------------------------
1. AWS publishes WAFV2 metrics once a minute and ONLY for a nonzero value
   (https://docs.aws.amazon.com/waf/latest/developerguide/waf-metrics.html). The generic path zero-fills a Sum metric
   only when the WHOLE window is empty, so with 1-min points a window holding 3 busy minutes out of 15 would leave the other
   12 as gaps and `metrics.metric_value` pinned at the last busy minute. Here every settled minute with no datapoint is
   written as 0 (a Sum with nothing reported IS zero), and a re-poll overwrites a zero that real data later replaces.
2. Per-rule series (BlockedRequests / CountedRequests with Rule=<rule metric name>) and the derived BlockRatio do not fit
   the one-query-per-(resource, metric) shape of the generic loop.

COST (CloudWatch bills per METRIC REQUESTED, $0.01 / 1,000 -- not per API call, not per datapoint, not per Period):
  * Only WebACLs that discovery has found are queried (the caller passes the resource rows; none -> no call at all).
  * Only metrics enabled for the account (account_metric_selections) are requested.
  * Per-rule: one metric per Block/Count rule (BlockedRequests OR CountedRequests, never both, never Allow rules), only
    rules whose CloudWatch metrics are enabled, taken from the rule list discovery stored (no ListMetrics, no guessing).
  * Everything for one (account, region) goes in as few requests as the 500-query limit allows (runner._request_chunks).
  * BlockRatio is computed here from points already fetched: zero extra requests.

PER-RULE SERIES are stored in metric_history only (no `metrics` row, no catalog row, no threshold): visibility, not alerting.
They are sparse on purpose -- never zero-filled -- so a wrong dimension reads as "no data", not as "no attacks".
Stored name: waf_rule__blocked__<rule MetricName> / waf_rule__counted__<rule MetricName>, shortened with a digest when it
would exceed the VARCHAR(100) column (see rule_db_name).
"""
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone

from app.aws.metric_catalog_data import CURATED
from app.collector import api_usage, polling_model
from app.collector.metrics.runner import (
    SETTLE_GRACE_1MIN_SECONDS, _align_window_end, _billed_units, _get_metric_data_all_pages, _request_chunks,
)
from app.collector.metrics_writer import write_metric_history_batch, write_metrics_batch
from app.threshold_defaults import resolve_db_metric_name

logger = logging.getLogger(__name__)

NAMESPACE = "AWS/WAFV2"
RULE_PREFIX = "waf_rule__"
RULE_BLOCKED_PREFIX = RULE_PREFIX + "blocked__"
RULE_COUNTED_PREFIX = RULE_PREFIX + "counted__"
RATIO_DB_NAME = "blockratio"
# BlockRatio = Blocked / (Allowed + Blocked). Counted and Passed are deliberately NOT in the denominator: a request matched by
# a Count-mode rule (or passed through a rule group) continues on and is ALSO reported as Allowed or Blocked, so adding them
# double-counts and understates the ratio (PROD showed Counted up to 430/min). Both components must be enabled for the
# account, otherwise the denominator would silently shrink and the ratio overstate.
RATIO_COMPONENTS = ("AllowedRequests", "BlockedRequests")
# A ratio is meaningless on a handful of requests (1 blocked of 2 = 50%). Below this many requests in the minute no ratio
# point is written at all. Tunable without a deploy: WAF_RATIO_MIN_REQUESTS.
RATIO_MIN_REQUESTS = max(1, int(os.environ.get("WAF_RATIO_MIN_REQUESTS", "20")))

_KEY_DEFS = [
    (name, resolve_db_metric_name("wafv2", name))
    for (name, _unit, _stat, _dflt, _desc) in CURATED["wafv2"][3]
    if ("wafv2", name) not in polling_model.AWS_EXTENDED_DERIVED
]


METRIC_NAME_MAX = 100   # metric_history.metric_name is VARCHAR(100) (checked with DESCRIBE on PROD, 2026-10-12)


def rule_db_name(kind, rule_metric):
    """Stored metric_name for a per-rule series. Always <= METRIC_NAME_MAX; a rule metric name too long to fit is cut to
    60 chars plus an 8-hex digest of the full name, so it stays unique and deterministic (the API endpoint calls this
    same function to find the rows)."""
    name = (RULE_BLOCKED_PREFIX if kind == "blocked" else RULE_COUNTED_PREFIX) + rule_metric
    if len(name) <= METRIC_NAME_MAX:
        return name
    digest = hashlib.sha1(rule_metric.encode("utf-8")).hexdigest()[:8]
    prefix = RULE_BLOCKED_PREFIX if kind == "blocked" else RULE_COUNTED_PREFIX
    return f"{prefix}{rule_metric[:60]}_{digest}"


def acl_dimension_value(resource):
    """The `WebACL` dimension is the ACL's VisibilityConfig.MetricName, which usually (not always) equals its Name.
    Discovery stores the real one when it has called get_web_acl; otherwise the Name, as before this phase."""
    tags = resource.get("tags") or {}
    return (tags.get("acl_metric_name") if isinstance(tags, dict) else None) or resource["resource_id"]


def _dims(resource, rule):
    tags = resource.get("tags") if isinstance(resource.get("tags"), dict) else {}
    region = (tags.get("cw_extra_dims") or {}).get("Region") or resource.get("region")
    return [{"Name": "WebACL", "Value": acl_dimension_value(resource)},
            {"Name": "Region", "Value": region},
            {"Name": "Rule", "Value": rule}]


def rule_targets(resource, enabled_keys):
    """[(kind, rule_entry)] -- which per-rule metric to request for each remembered rule. Block rules (and rule-group
    statements not overridden to Count) -> BlockedRequests; Count rules (and groups overridden to Count) ->
    CountedRequests. Allow / CAPTCHA / Challenge rules are not queried (cost)."""
    tags = resource.get("tags") if isinstance(resource.get("tags"), dict) else {}
    out = []
    for rule in tags.get("waf_rules") or []:
        metric = rule.get("metric")
        if not metric:
            continue
        action = rule.get("action")
        if action in ("BLOCK", "NONE") and ("wafv2", "BlockedRequests") in enabled_keys:
            out.append(("blocked", rule))
        elif action in ("COUNT", "COUNT_OVERRIDE") and ("wafv2", "CountedRequests") in enabled_keys:
            out.append(("counted", rule))
    return out


def _epoch(ts):
    return int(ts.timestamp())


def fill_minutes(points, start_epoch, end_epoch, period=60):
    """{epoch: value} for every `period` bucket in [start, end): the reported value, else 0.0 (a Sum with nothing
    reported is zero). Pure function."""
    return {t: float(points.get(t, 0.0)) for t in range(start_epoch, end_epoch, period)}


def derive_block_ratio(series, min_requests=RATIO_MIN_REQUESTS):
    """series: {cw_metric_name: {epoch: value}} already zero-filled over the same buckets.
    -> {epoch: 100 * Blocked / (Allowed + Blocked)} for buckets with at least `min_requests` requests.
    Pure function."""
    if not all(c in series for c in RATIO_COMPONENTS):
        return {}
    out = {}
    for t in series["BlockedRequests"]:
        total = sum(series[c].get(t, 0.0) for c in RATIO_COMPONENTS)
        if total >= min_requests:
            out[t] = round(100.0 * series["BlockedRequests"][t] / total, 4)
    return out


def _run(cw, queries, tier, start, end):
    """-> ({query Id: {epoch: value}}, {Ids of every query in a chunk that SUCCEEDED}). A failed chunk is logged and its
    Ids are left out of the second set -- the caller must not zero-fill those (an API error is not "no traffic")."""
    results, ok = {}, set()
    for chunk in _request_chunks(queries):
        try:
            got, pages = _get_metric_data_all_pages(cw, chunk, start, end)
        except Exception as e:
            logger.error(f"WAF GMD call failed ({len(chunk)} queries): {e}")
            api_usage.record("aws", tier, calls=1, units=_billed_units(chunk))
            continue
        api_usage.record("aws", tier, calls=pages, units=_billed_units(chunk))
        ok.update(q["Id"] for q in chunk)
        for qid, (timestamps, values) in got.items():
            results[qid] = {_epoch(t): float(v) for t, v in zip(timestamps, values)}
    return results, ok


def collect_waf_resources(cw, resources, enabled_keys, tier):
    """One (account, region) group of WebACL rows -> GetMetricData -> metrics / metric_history. Returns series written.
    resources: dicts (id, resource_id, name, region, tags). enabled_keys: {(service, CloudWatch metric name)}."""
    if not resources:
        return 0
    defs = [(cw_name, db) for cw_name, db in _KEY_DEFS
            if ("wafv2", cw_name) in enabled_keys and polling_model.aws_extended_tier("wafv2", cw_name) == tier]
    ratio_on = (tier == "standard" and ("wafv2", "BlockRatio") in enabled_keys
                and all(("wafv2", c) in enabled_keys for c in RATIO_COMPONENTS))
    per_rule_on = tier == "standard" and polling_model.waf_per_rule_enabled()
    if not defs and not per_rule_on:
        return 0

    now = datetime.now(timezone.utc)
    lookback = timedelta(minutes=polling_model.AWS_EXTENDED_LOOKBACK_MIN.get(tier, 15))
    end1 = _align_window_end(now, polling_model.WAF_KEY_PERIOD_SEC, SETTLE_GRACE_1MIN_SECONDS)
    start1 = end1 - lookback

    key_q, key_map = [], {}
    for r in resources:
        for cw_name, db in defs:
            qid = f"w{len(key_q)}"
            key_q.append({"Id": qid, "ReturnData": True, "MetricStat": {
                "Metric": {"Namespace": NAMESPACE, "MetricName": cw_name, "Dimensions": _dims(r, "ALL")},
                "Period": polling_model.WAF_KEY_PERIOD_SEC, "Stat": "Sum"}})
            key_map[qid] = (r["id"], cw_name, db)

    written = 0
    latest_rows, history_rows = [], []
    if key_q:
        got, ok = _run(cw, key_q, tier, start1, end1)
        per_res = {}
        for qid, (res_id, cw_name, db) in key_map.items():
            if qid not in ok:
                continue
            filled = fill_minutes(got.get(qid, {}), _epoch(start1), _epoch(end1))
            per_res.setdefault(res_id, {})[cw_name] = filled
            history_rows += [(res_id, db, v, datetime.fromtimestamp(t, tz=timezone.utc)) for t, v in filled.items()]
            latest_rows.append((res_id, db, filled[max(filled)]))
            written += 1
        if ratio_on:
            for res_id, series in per_res.items():
                ratio = derive_block_ratio(series)
                history_rows += [(res_id, RATIO_DB_NAME, v, datetime.fromtimestamp(t, tz=timezone.utc))
                                 for t, v in ratio.items()]
                newest = _epoch(end1) - polling_model.WAF_KEY_PERIOD_SEC
                if newest in ratio:     # no fresh `metrics` row when the newest minute was too quiet to judge
                    latest_rows.append((res_id, RATIO_DB_NAME, ratio[newest]))
                if ratio:
                    written += 1

    if per_rule_on:
        rp = polling_model.WAF_RULE_PERIOD_SEC
        end5 = _align_window_end(now, rp, SETTLE_GRACE_1MIN_SECONDS)
        start5 = end5 - lookback
        rq, rmap = [], {}
        for r in resources:
            for kind, rule in rule_targets(r, enabled_keys):
                qid = f"r{len(rq)}"
                cw_name = "BlockedRequests" if kind == "blocked" else "CountedRequests"
                rq.append({"Id": qid, "ReturnData": True, "MetricStat": {
                    "Metric": {"Namespace": NAMESPACE, "MetricName": cw_name, "Dimensions": _dims(r, rule["metric"])},
                    "Period": rp, "Stat": "Sum"}})
                rmap[qid] = (r["id"], rule_db_name(kind, rule["metric"]))
        if rq:
            got, _ok = _run(cw, rq, tier, start5, end5)
            for qid, (res_id, db) in rmap.items():
                pts = got.get(qid) or {}
                history_rows += [(res_id, db, v, datetime.fromtimestamp(t, tz=timezone.utc)) for t, v in pts.items()]
                written += 1 if pts else 0

    if latest_rows:
        write_metrics_batch(latest_rows)
    if history_rows:
        write_metric_history_batch(history_rows, overwrite=True)
    logger.info(f"    WAF[{tier}]: {len(resources)} web ACLs, {written} series, {len(history_rows)} points")
    return written
