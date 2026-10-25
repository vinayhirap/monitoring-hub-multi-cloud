#!/usr/bin/env python3
"""
READ-ONLY CloudWatch polling plan: how many resources, GetMetricData calls and billed metrics
each tier makes per day per environment, with ENV_POLLING_ENABLED off versus on.

It does not re-implement the collectors. It runs the REAL ones with the CloudWatch call
replaced by a recorder:
    runner._get_resources_for_account   the resource selection
    runner._collect_core                core query building + look-back grouping
    extended._get_extended_resources_for_account / _collect_extended_service
    runner._request_chunks / _billed_units   the exact chunking and billing unit
so the numbers cannot drift from what a tick would really request. Nothing is written to the
database and nothing is sent to AWS.

Usage (in /opt/monitoring-hub/app, venv python):
    /opt/monitoring-hub/venv/bin/python3 scripts/polling_plan_report.py
    ... --scenario defaults-cleared     # account-default labels treated as unplaced (PROD)
    ... --json                          # machine readable

Scenarios
    current           environment labels exactly as stored (resources.env_resolved)
    defaults-cleared  labels that only came from an account default (env_source='account')
                      are treated as unplaced, i.e. PROD (the fail-safe tier). This is the SAFE LOWER BOUND of the
                      savings: it is what you get if the defaults are removed and only real tags,
                      names and manual choices remain.

Model (see docs/POLLING_COST_ESTIMATE.md)
    collections/day of a group = 86400 / effective_interval
    calls  per collection      = number of GetMetricData requests (<=500 queries each, SEARCH <=5)
    units  per collection      = metrics requested (the billed unit)
ASSUMED, not measurable without CloudWatch: EC2 CWAgent memory and disk query one metric per
instance each (reported on their own line, "assumed"); a page of GetMetricData results (more than
100,800 datapoints) is not modelled, so calls are a floor.
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Both flags must be on for the real selection code to read env_resolved; the report evaluates the
# flag-OFF answer itself with force_enabled=False, so setting them here changes nothing else.
os.environ["ENV_AWARE_CLASSIFICATION"] = "true"
os.environ["ENV_POLLING_ENABLED"] = "true"

from app.collector import env_polling as ep  # noqa: E402
from app.collector import polling_model as pm  # noqa: E402
from app.collector.metrics import runner, extended  # noqa: E402

DAY = 86400
CORE_TIERS = ("critical", "standard", "low")
EXT_TIERS = ("standard", "low", "extended", "slow_extended")


def _scenario_env(res, scenario):
    env = res.get("env_resolved")
    if scenario == "defaults-cleared" and res.get("env_source") == "account":
        return None
    return env


def _record(queries_by_call):
    """(calls, units) of a captured query list, chunked exactly as _execute_gmd does."""
    calls = units = 0
    for chunk in runner._request_chunks(queries_by_call):
        calls += 1
        units += runner._billed_units(chunk)
    return calls, units


class _Capture:
    """Replaces runner._execute_gmd / _run_gmd for the duration of a plan."""

    def __init__(self):
        self.queries = []

    def execute(self, cw, queries, id_map, minutes=5, align_period=None):
        self.queries.extend(queries)
        return 0


def _core_group_cost(resources, rtype, tier, extra):
    """Queries one _collect_core call would send for these resources."""
    cap = _Capture()
    orig = runner._execute_gmd
    runner._execute_gmd = cap.execute
    try:
        runner._collect_core(None, resources, rtype, tier, extra)
    finally:
        runner._execute_gmd = orig
    return _record(cap.queries)


def _extended_group_cost(resources, rtype, tier, enabled_keys, extra):
    cap = _Capture()
    orig = extended._execute_gmd
    extended._execute_gmd = cap.execute
    try:
        extended._collect_extended_service(None, resources, rtype, enabled_keys, minutes=16 + extra, tier=tier)
    finally:
        extended._execute_gmd = orig
    return _record(cap.queries)


def _accounts(conn):
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, account_name FROM aws_accounts WHERE status = 'active' "
                "AND COALESCE(provider, 'aws') = 'aws' ORDER BY id")
    rows = cur.fetchall()
    cur.close()
    return rows


def _fetch_env_source(conn, ids):
    """env_source is not part of the collectors' SELECT; read it separately (read-only)."""
    if not ids:
        return {}
    cur = conn.cursor(dictionary=True)
    out = {}
    try:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            cur.execute(f"SELECT id, env_source FROM resources WHERE id IN ({','.join(['%s'] * len(chunk))})", chunk)
            out.update({r["id"]: r["env_source"] for r in cur.fetchall()})
    except Exception:
        pass
    finally:
        cur.close()
    return out


def build_plan(scenario="current", conn=None):
    """-> {'rows': [...], 'totals': {...}, 'scenario': ...}. Each row is one
    (account, tier, environment, resource_type) group:
       resources, base_interval, effective_interval, off_calls_day, off_units_day,
       on_calls_day, on_units_day."""
    from app.db import get_connection
    own = conn is None
    conn = conn or get_connection()
    rows = []
    try:
        for acc in _accounts(conn):
            # core tiers
            for tier in CORE_TIERS:
                grouped = runner._get_resources_for_account(acc["id"], tier)
                _add_core(rows, conn, acc, tier, grouped, scenario)
            # extended family
            cur = conn.cursor(dictionary=True)
            enabled = extended._enabled_extended_metrics(cur, acc["id"])
            cur.close()
            ext_grouped = extended._get_extended_resources_for_account(acc["id"])
            for tier in EXT_TIERS:
                _add_extended(rows, conn, acc, tier, ext_grouped, enabled, scenario)
    finally:
        if own:
            conn.close()
    rows = _merge(rows)
    # always present, even with no rows (an empty or fresh database must still render)
    totals = dict.fromkeys(("off_calls_day", "off_units_day", "on_calls_day", "on_units_day", "resources"), 0.0)
    for r in rows:
        for k in totals:
            totals[k] += r[k]
    return {"scenario": scenario, "rows": rows, "totals": totals}


def _merge(rows):
    """One row per (tier, environment, resource type): accounts and regions are summed (they are
    separate GetMetricData calls in reality, so calls add up exactly as they would)."""
    merged = {}
    for r in rows:
        key = (r["tier"], r["env"], r["resource_type"])
        m = merged.get(key)
        if m is None:
            merged[key] = dict(r, account="all")
            continue
        for k in ("resources", "metrics_per_collection", "off_calls_day", "off_units_day",
                  "on_calls_day", "on_units_day"):
            m[k] = round(m[k] + r[k], 1)
    return list(merged.values())


def _annotate_env(conn, grouped, scenario):
    ids = [r["id"] for rs in grouped.values() for r in rs]
    src = _fetch_env_source(conn, ids) if scenario == "defaults-cleared" else {}
    for rs in grouped.values():
        for r in rs:
            r["env_source"] = src.get(r["id"])
            r["env_resolved"] = _scenario_env(r, scenario)


def _add_core(rows, conn, acc, tier, grouped, scenario):
    _annotate_env(conn, grouped, scenario)
    base = pm.TIER_SECONDS[tier]
    critical = tier == "critical"
    for (rtype, _region), resources in grouped.items():
        if rtype not in runner._CORE_TYPES:
            continue
        _emit(rows, acc, tier, rtype, resources, base, critical,
              lambda rs, extra, t=tier, rt=rtype: _core_group_cost(rs, rt, t, extra))
        if rtype == "ec2":                                    # CWAgent: assumed 1 metric / instance
            if tier == pm.CWAGENT_MEM_TIER:
                _emit(rows, acc, tier, "ec2:cwagent_mem(assumed)", resources, base, critical,
                      lambda rs, extra: (1 if rs else 0, len(rs)))
            if tier == pm.CWAGENT_DISK_TIER:
                _emit(rows, acc, tier, "ec2:cwagent_disk(assumed)", resources, base, critical,
                      lambda rs, extra: (1 if rs else 0, len(rs)))


def _add_extended(rows, conn, acc, tier, ext_grouped, enabled, scenario):
    _annotate_env(conn, ext_grouped, scenario)
    base = pm.TIER_SECONDS[tier]
    for (rtype, _region), resources in ext_grouped.items():
        if rtype not in extended.EXTENDED_METRICS:
            continue
        if not any(pm.aws_extended_tier(rtype, d[0]) == tier and (rtype, d[0]) in enabled
                   for d in extended.EXTENDED_METRICS[rtype]):
            continue
        _emit(rows, acc, tier, rtype, resources, base, False,
              lambda rs, extra, t=tier, rt=rtype: _extended_group_cost(rs, rt, t, enabled, extra))


def _emit(rows, acc, tier, rtype, resources, base, critical, cost_fn):
    """One row per environment class actually present in `resources`."""
    by_env = defaultdict(list)
    for r in resources:
        by_env[ep.normalize_env(r.get("env_resolved"))].append(r)
    per_day_off = DAY / float(base)
    # TODAY (flag off) every resource of the group goes into the same requests, so the baseline
    # call count is that of the WHOLE group, shared out to the environments by billed metrics.
    # (Costing each environment separately would count extra requests that do not exist today.)
    calls_all, units_all = cost_fn(resources, 0)
    for env, rs in sorted(by_env.items()):
        calls0, units0 = cost_fn(rs, 0)
        off_calls = (calls_all * (units0 / float(units_all)) if units_all else 0.0) * per_day_off
        off_units = units0 * per_day_off
        eff = ep.effective_interval(env, base, critical,
                                    rs[0].get("resource_type"), True)
        if eff is None:                                        # NON-PROD non-critical, mode=off
            on_calls = on_units = 0.0
            eff_label = None
        else:
            extra = ep.round_extra(ep.extra_window_minutes(env, base, critical,
                                                           rs[0].get("resource_type"), True))
            calls1, units1 = cost_fn(rs, extra)
            on_calls, on_units = calls1 * DAY / float(eff), units1 * DAY / float(eff)
            eff_label = eff
        rows.append({
            "account": acc["account_name"], "tier": tier, "env": env, "resource_type": rtype,
            "resources": len(rs), "base_interval": base, "effective_interval": eff_label,
            "metrics_per_collection": units0,
            "off_calls_day": round(off_calls, 1), "off_units_day": round(off_units, 1),
            "on_calls_day": round(on_calls, 1), "on_units_day": round(on_units, 1),
        })


def _pct(a, b):
    return "n/a" if not a else f"{100.0 * (a - b) / a:5.1f}%"


def render(plan):
    out = [f"Scenario: {plan['scenario']}  (flag OFF = today, flag ON = ENV_POLLING_ENABLED with current POLL_* settings)",
           f"NON-PROD interval {ep.nonprod_interval_seconds()} s | "
           f"NON-PROD critical {ep.nonprod_critical_interval_seconds()} s | NON-PROD non-critical mode {ep.nonprod_noncritical_mode()}", ""]
    hdr = f"{'tier':14}{'env':10}{'type':28}{'res':>5}{'every':>9}{'->':>3}{'every':>9}{'calls/d off':>13}{'calls/d on':>12}{'units/d off':>13}{'units/d on':>12}"
    out.append(hdr)
    out.append("-" * len(hdr))
    for r in sorted(plan["rows"], key=lambda r: (r["tier"], r["env"], r["resource_type"])):
        eff = "off" if r["effective_interval"] is None else f"{r['effective_interval']}s"
        out.append(f"{r['tier']:14}{r['env']:10}{r['resource_type'][:27]:28}{r['resources']:>5}{str(r['base_interval']) + 's':>9}"
                   f"{'':>3}{eff:>9}{r['off_calls_day']:>13.0f}{r['on_calls_day']:>12.0f}"
                   f"{r['off_units_day']:>13.0f}{r['on_units_day']:>12.0f}")
    t = plan["totals"]
    out += ["", "BY ENVIRONMENT (per day)"]
    by_env = defaultdict(lambda: defaultdict(float))
    for r in plan["rows"]:
        for k in ("resources", "off_calls_day", "on_calls_day", "off_units_day", "on_units_day"):
            by_env[r["env"]][k] += r[k]
    for env, v in sorted(by_env.items()):
        out.append(f"  {env:10} calls {v['off_calls_day']:10.0f} -> {v['on_calls_day']:10.0f}   "
                   f"billed metrics {v['off_units_day']:12.0f} -> {v['on_units_day']:12.0f}  ({_pct(v['off_units_day'], v['on_units_day'])} saved)")
    out += ["", f"TOTAL per day: calls {t['off_calls_day']:.0f} -> {t['on_calls_day']:.0f} ({_pct(t['off_calls_day'], t['on_calls_day'])} saved); "
                f"billed metrics {t['off_units_day']:.0f} -> {t['on_units_day']:.0f} ({_pct(t['off_units_day'], t['on_units_day'])} saved)",
            f"       per 30 days: billed metrics {t['off_units_day'] * 30:.0f} -> {t['on_units_day'] * 30:.0f}",
            "Confirm the current per-metric GetMetricData rate in AWS CloudWatch pricing and multiply; no price is assumed here."]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=("current", "defaults-cleared"), default="current")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    plan = build_plan(a.scenario)
    print(json.dumps(plan, indent=2) if a.json else render(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
