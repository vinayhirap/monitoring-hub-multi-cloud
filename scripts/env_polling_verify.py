#!/usr/bin/env python3
"""
env_polling_verify.py -- READ-ONLY check that environment-aware polling (ENV_POLLING_ENABLED, phase 4c)
does what app/collector/env_polling.py says it does. Safe on PROD: it only runs SELECTs (the session is
opened read-only), writes nothing, and never changes a flag. It does not call AWS.

    cd /opt/monitoring-hub/app
    /opt/monitoring-hub/venv/bin/python3 scripts/env_polling_verify.py                  # flag state from .env
    ... --expect off                         # BEFORE the rollout: everything must be on its normal cadence
    ... --expect on --uptime-min 190         # AFTER: minutes since the service restart (see the runbook)
    ... --config-only                        # no database: flags, POLL_* values and the interval rules only
    ... --only alerts                        # one section: config | series | alerts | unknown
    ... --account-id 7  --json  --csv-unknown /tmp/unknown_5min.csv

SECTIONS
  1 config   flags and POLL_* values (raw vs the value the code really uses, clamps, typos, duplicate .env
             lines), then the interval rules checked against every scheduled AWS metric: never faster than the
             base interval, flag OFF == the prod answer, PROD / wafv2 / daily / Describe-API metrics never
             slowed, NON-PROD critical >= POLL_NONPROD_CRITICAL_INTERVAL_SECONDS.
  2 series   per resource type x environment x tier: configured interval versus the age of the last
             collection (metrics.metric_timestamp is the WRITE time, so now - it = time since the collector
             last stored a value for that series). Flags SLOWER, FASTER (group test), STILL_COLLECTED.
  3 alerts   every open NON-PROD alert: will it go falsely stale, or is its stale limit too tight?
  4 unknown  the unplaced EC2 / EBS resources (env_source='unknown', treated as PROD) on the 5-minute tier
             and the tag / name that would classify each (nothing is changed).

HOW TO READ THE SERIES VERDICTS (what this can and cannot prove)
  * A single series cannot show it was polled FASTER than configured. FASTER is a group test: it needs 6+
    resources in the group, a slowed (expected > base) interval and --uptime-min >= expected + slack, and it
    fires when even the OLDEST series is younger than half the expected interval (with a uniform cadence the
    oldest of 6 would be older than that 98.4% of the time). The definitive proof is the call count: compare
    op_events metric_api_usage over 24 h with the 7-day baseline (docs/ENV_POLLING_ROLLOUT.md section 6).
  * SLOWER: age > expected + slack, slack = min(base, 600 s) + 210 s (one missed tick plus tick jitter plus
    run time). Sparse / activity-driven series (Lambda, SQS, S3, ... see metric_meta.is_sparse_metric)
    legitimately go quiet, so they are counted but NOT judged.
  * TOO_EARLY: the service has not been up for one expected interval yet, nothing can be concluded.
  * After a restart every slowed resource is collected once and then spread over the interval
    (env_polling.stagger_seconds), so judge only after --uptime-min >= the longest expected interval.

EXIT CODE  0 = no hard finding   1 = at least one FAIL (an invariant broken, SLOWER / FASTER /
STILL_COLLECTED group, an alert whose stale limit is shorter than its polling gap)   2 = could not run
(migration 083 missing, no database). WARN findings (e.g. NON-PROD alerts that will stop being evaluated) are
expected consequences of the design and do not change the exit code.
"""
import argparse
import csv
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# Pure modules only at import time (no app.db): the pure functions below are unit-tested without a database.
from app import environment_classifier as ec  # noqa: E402
from app.collector import env_polling as ep  # noqa: E402
from app.collector import polling_model as pm  # noqa: E402

OPEN_STATUSES = ("active", "acknowledged")
ENVS = ("prod", "nonprod", None)                # None = a NULL env_resolved, handled as PROD
PROD_LIKE = ("prod", None)
STALE_RESOURCE_HOURS = 48                  # same freshness rule as runner.STALE_RESOURCE_HOURS
MIN_RESOURCES_FOR_FASTER = 6
FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
HARD_VERDICTS = ("SLOWER", "FASTER", "STILL_COLLECTED")

FLAG_VARS = ("ENV_POLLING_ENABLED", "ENV_AWARE_CLASSIFICATION", "POLL_NONPROD_EVAL_CRITICAL_ONLY")
INT_VARS = (
    ("POLL_NONPROD_INTERVAL_SECONDS", ep.MIN_INTERVAL_SECONDS, ep.MAX_INTERVAL_SECONDS,
     ep.DEFAULT_NONPROD_INTERVAL_SECONDS),
    ("POLL_NONPROD_CRITICAL_INTERVAL_SECONDS", ep.MIN_INTERVAL_SECONDS, ep.MAX_INTERVAL_SECONDS,
     ep.DEFAULT_NONPROD_CRITICAL_INTERVAL_SECONDS),
)
# New POLL_NONPROD_* names and the POLL_DEV_* names they replaced (still honoured by env_polling as a fallback).
LEGACY_NAMES = {
    "POLL_NONPROD_INTERVAL_SECONDS": "POLL_DEV_INTERVAL_SECONDS",
    "POLL_NONPROD_CRITICAL_INTERVAL_SECONDS": "POLL_DEV_CRITICAL_INTERVAL_SECONDS",
    "POLL_NONPROD_NONCRITICAL_MODE": "POLL_DEV_NONCRITICAL_MODE",
    "POLL_NONPROD_EVAL_CRITICAL_ONLY": "POLL_DEV_EVAL_CRITICAL_ONLY",
}
KNOWN_VARS = {"ENV_POLLING_ENABLED", "ENV_AWARE_CLASSIFICATION"} | set(LEGACY_NAMES) | set(LEGACY_NAMES.values())


def _lookup(env, name):
    """(raw, name_used): the new name, else its legacy POLL_DEV_* name when the new one is unset/blank."""
    raw = env.get(name)
    if raw is not None and raw.strip():
        return raw, name
    legacy = LEGACY_NAMES.get(name)
    if legacy:
        lraw = env.get(legacy)
        if lraw is not None and lraw.strip():
            return lraw, legacy
    return raw, name
_FALSE_WORDS = ("", "0", "false", "no", "off")


# ======================================================================================
# 1. settings and configuration invariants (pure)
# ======================================================================================
def scan_env_file(path):
    """[(line_no, name, value)] for the variables this feature reads, in file order. Quotes stripped the way
    python-dotenv and systemd strip them. Missing file -> []."""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for no, line in enumerate(fh, 1):
                m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
                if not m:
                    continue
                name, val = m.group(1), m.group(2)
                if (name in KNOWN_VARS or name.startswith(("POLL_", "ENV_POLL", "ENV_AWARE"))):
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                        val = val[1:-1]
                    out.append((no, name, val))
    except OSError:
        return []
    return out


def settings_report(env=None, env_file_entries=None):
    """-> {'flags': {...}, 'rows': [...], 'warnings': [...]}. `env` defaults to os.environ.
    Every row: name, raw, effective, status (default | ok | clamped | invalid | unrecognised)."""
    env = os.environ if env is None else env
    rows, warnings = [], []
    for name in FLAG_VARS:
        raw, _used = _lookup(env, name)
        default = name == "POLL_NONPROD_EVAL_CRITICAL_ONLY"        # the only flag that defaults to true
        eff = (raw if raw is not None else ("true" if default else "false")).strip().lower() in ep._TRUE
        status = "default" if raw is None else "ok"
        if raw is not None and raw.strip().lower() not in ep._TRUE and raw.strip().lower() not in _FALSE_WORDS:
            status = "unrecognised"
            warnings.append(f"{name}={raw!r} is not a recognised true/false word, so it counts as FALSE "
                            f"(use true or false)")
        rows.append({"name": name, "raw": raw, "effective": eff, "default": default, "status": status})
    for name, lo, hi, default in INT_VARS:
        raw, _used = _lookup(env, name)
        try:
            parsed = int(float((raw or "").strip()))
            eff = max(lo, min(hi, parsed))
            status = "ok" if eff == parsed else "clamped"
            if status == "clamped":
                warnings.append(f"{name}={raw} is outside {lo}..{hi}; the code uses {eff}")
        except (TypeError, ValueError):
            eff, status = default, ("default" if raw is None else "invalid")
            if raw is not None and raw.strip() != "":
                warnings.append(f"{name}={raw!r} is not a number; the code uses the default {default}")
        rows.append({"name": name, "raw": raw, "effective": eff, "default": default, "status": status})
    raw, _used = _lookup(env, "POLL_NONPROD_NONCRITICAL_MODE")
    mode = (raw if raw is not None else ep.DEFAULT_NONPROD_NONCRITICAL_MODE).strip().lower()
    eff = mode if mode in ("slow", "off") else ep.DEFAULT_NONPROD_NONCRITICAL_MODE
    status = "default" if raw is None else ("ok" if eff == mode else "invalid")
    if status == "invalid":
        warnings.append(f"{_used}={raw!r} is not slow|off; the code uses {eff!r}")
    rows.append({"name": "POLL_NONPROD_NONCRITICAL_MODE", "raw": raw, "effective": eff,
                 "default": ep.DEFAULT_NONPROD_NONCRITICAL_MODE, "status": status})

    requested = (env.get("ENV_POLLING_ENABLED") or "false").strip().lower() in ep._TRUE
    classification = (env.get("ENV_AWARE_CLASSIFICATION") or "false").strip().lower() in ep._TRUE
    flags = {"requested": requested, "classification": classification, "enabled": requested and classification}
    if requested and not classification:
        warnings.append("ENV_POLLING_ENABLED is true but ENV_AWARE_CLASSIFICATION is not: env_polling.enabled() "
                        "is FALSE, nothing is slowed (the deliberate safe answer)")
    for name in sorted(k for k in env if k.startswith(("POLL_", "ENV_POLL", "ENV_AWARE")) and k not in KNOWN_VARS):
        warnings.append(f"{name} is set but nothing reads it (typo? known: {', '.join(sorted(KNOWN_VARS))})")
    by_name = defaultdict(list)
    for no, name, val in (env_file_entries or []):
        by_name[name].append((no, val))
    for name, defs in sorted(by_name.items()):
        if len(defs) > 1:
            vals = ", ".join(f"line {no}={v!r}" for no, v in defs)
            level = "conflicting values" if len({v for _, v in defs}) > 1 else "defined twice"
            warnings.append(f".env has {name} {level} ({vals}); the LAST one wins")
    return {"flags": flags, "rows": rows, "warnings": warnings}


def slack_seconds(base):
    """Tolerance on top of an interval before a series counts as late: one missed tick (capped at 10 min)
    plus tick jitter and run time (210 s)."""
    return min(int(base), 600) + 210


def _p1():
    return pm.p1_metric_keys()


def check_config(rows, on=True):
    """Invariants of env_polling.effective_interval over the scheduled metric inventory.

    rows   scripts/polling_audit.inventory()-style dicts (provider, service, db_metric, tier, interval_s)
    on     evaluate the rules with the flag forced ON (the OFF answer is always checked too)
    -> (violations [str], matrix [dict])"""
    violations, seen = [], {}
    p1 = _p1()
    for r in rows:
        if r.get("provider") != "aws":
            continue
        rtype, db = r["service"], (r["db_metric"] or "").lower()
        base = int(r["interval_s"])
        critical = (rtype, db) in p1
        describe = pm.base_interval_seconds("aws", rtype, db) is None
        label = f"{rtype}/{db}"
        if describe != (r.get("tier") == "describe"):
            violations.append(f"{label}: base_interval_seconds is {'None' if describe else 'set'} but the "
                              f"inventory tier is {r.get('tier')}")
        if describe:
            continue          # callers (collector, evaluator, metric API) skip a Describe-API metric before asking
        for env in ENVS:
            eff_off = ep.effective_interval(env, base, critical, rtype, force_enabled=False)
            eff = ep.effective_interval(env, base, critical, rtype, force_enabled=on)
            e = ep.normalize_env(env)
            where = f"{label} env={env} base={base}"
            if eff_off != base:
                violations.append(f"{where}: flag OFF gives {eff_off}, must equal base")
            if eff is not None and eff < base:
                violations.append(f"{where}: effective {eff} is FASTER than base")
            if eff is None and not (e == ep.NONPROD and not critical):
                violations.append(f"{where}: not collected, only NON-PROD non-critical may be")
            if e == ep.PROD and eff != base:
                violations.append(f"{where}: prod-like environment was slowed to {eff}")
            if rtype in ep.EXEMPT_RESOURCE_TYPES and eff != base:
                violations.append(f"{where}: exempt type was slowed to {eff}")
            if base >= ep.NEVER_SLOW_AT_OR_ABOVE_SECONDS and eff != base:
                violations.append(f"{where}: daily metric was slowed to {eff}")
            if (on and e == ep.NONPROD and critical and rtype not in ep.EXEMPT_RESOURCE_TYPES
                    and base < ep.NEVER_SLOW_AT_OR_ABOVE_SECONDS
                    and eff is not None and eff < ep.nonprod_critical_interval_seconds()):
                violations.append(f"{where}: NON-PROD critical {eff} < POLL_NONPROD_CRITICAL_INTERVAL_SECONDS")
            exempt = rtype in ep.EXEMPT_RESOURCE_TYPES or base >= ep.NEVER_SLOW_AT_OR_ABOVE_SECONDS
            if not exempt:
                seen.setdefault((base, critical), {})[e] = eff
    matrix = [{"base": b, "critical": c, "prod": v.get("prod"), "nonprod": v.get("nonprod")}
              for (b, c), v in sorted(seen.items())]
    return violations, matrix


# ======================================================================================
# 2. per-series polling cadence (pure)
# ======================================================================================
def percentile(values, p):
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return v[lo] if lo == hi else v[lo] + (v[hi] - v[lo]) * (k - lo)


def judge_group(ages, n_res, base, expected, uptime_min=None):
    """Verdict of one group of series (ages in seconds). `expected` None = not collected at all."""
    if expected is None:
        if uptime_min is None:
            return "UNPROVEN"
        slack = slack_seconds(base)
        horizon = min(uptime_min * 60, base + slack)
        return "STILL_COLLECTED" if any(a < horizon for a in ages) else "NOT_COLLECTED"
    if not ages:
        return "NO_DATA"
    slack = slack_seconds(base)
    if any(a > expected + slack for a in ages):
        return "SLOWER"
    if expected > base:
        if uptime_min is None:
            return "UNPROVEN"
        if uptime_min * 60 < expected + slack:
            return "TOO_EARLY"
        if n_res < MIN_RESOURCES_FOR_FASTER:
            return "FEW"
        if max(ages) < 0.5 * expected:
            return "FASTER"
    return "OK"


def _sparse_default(service, metric):
    try:
        from app.metric_meta import is_sparse_metric
        return is_sparse_metric(service, metric)
    except Exception:
        return False


def analyse_series(rows, on, uptime_min=None, sparse_fn=None):
    """rows: dicts resource_pk, resource_id, resource_type, name, account, env (label or None), metric_name,
    age_s. -> (groups, late) where groups is a sorted list of dicts and late the individual series that are
    older than expected + slack."""
    sparse_fn = sparse_fn or _sparse_default
    groups, late = {}, []
    # A series is "NO_DATAPOINT" (not a late schedule) when it is old but the SAME resource has a fresh reading on
    # some other metric (so the resource is being collected) and the group has fresh series too (so its tier runs):
    # CloudWatch simply had no value for that metric (an idle EBS volume has no VolumeQueueLength, a non-gp2
    # volume no BurstBalance). If the whole group is old, or the whole resource, the schedule IS late.
    def _row_limit(r):
        m = (r["metric_name"] or "").lower()
        b = pm.base_interval_seconds("aws", r["resource_type"], m)
        d = b is None
        if d:
            b = pm.DESCRIBE_POLL_SECONDS
        exp = b if d else ep.effective_interval(r.get("env"), b, ep.is_critical_metric(r["resource_type"], m),
                                                r["resource_type"], force_enabled=on)
        return None if exp is None else exp + slack_seconds(b)

    def _gkey(r):
        m = (r["metric_name"] or "").lower()
        e = r.get("env")
        return (r["resource_type"], e if e in ("prod", "nonprod") else "unclassified",
                pm.base_interval_seconds("aws", r["resource_type"], m))

    alive, group_fresh = set(), set()
    for r in rows:
        if sparse_fn(r["resource_type"], (r["metric_name"] or "").lower()):
            continue
        lim = _row_limit(r)
        if lim is not None and int(r["age_s"]) <= lim:
            alive.add(r.get("resource_pk", r.get("resource_id")))
            group_fresh.add(_gkey(r))
    for r in rows:
        metric = (r["metric_name"] or "").lower()
        rtype = r["resource_type"]
        env = r.get("env")
        base = pm.base_interval_seconds("aws", rtype, metric)
        describe = base is None
        if describe:
            base = pm.DESCRIBE_POLL_SECONDS
        critical = ep.is_critical_metric(rtype, metric)
        expected = base if describe else ep.effective_interval(env, base, critical, rtype, force_enabled=on)
        label = env if env in ("prod", "nonprod") else "unclassified"
        key = (rtype, label, base, bool(critical), describe)
        g = groups.setdefault(key, {"resource_type": rtype, "env": label, "base": base, "critical": bool(critical),
                                    "describe": describe, "expected": expected, "series": 0, "sparse": 0,
                                    "resources": set(), "ages": []})
        g["series"] += 1
        if sparse_fn(rtype, metric):
            g["sparse"] += 1
            continue
        g["resources"].add(r.get("resource_pk", r.get("resource_id")))
        limit = None if expected is None else expected + slack_seconds(base)
        no_datapoint = (limit is not None and int(r["age_s"]) > limit
                        and r.get("resource_pk", r.get("resource_id")) in alive and _gkey(r) in group_fresh)
        if not no_datapoint:
            g["ages"].append(int(r["age_s"]))
        if no_datapoint:
            late.append({"kind": "NO_DATAPOINT", "account": r.get("account"), "resource_id": r.get("resource_id"),
                         "name": r.get("name"), "resource_type": rtype, "env": label, "metric": metric,
                         "age_s": int(r["age_s"]), "expected_s": expected, "base_s": base})
        elif expected is not None and int(r["age_s"]) > expected + slack_seconds(base):
            late.append({"kind": "LATE", "account": r.get("account"), "resource_id": r.get("resource_id"), "name": r.get("name"),
                         "resource_type": rtype, "env": label, "metric": metric, "age_s": int(r["age_s"]),
                         "expected_s": expected, "base_s": base})
    out = []
    for key in sorted(groups, key=lambda k: (k[0], _env_rank(k[1]), k[2], k[3])):
        g = groups[key]
        ages = g["ages"]
        n_res = len(g["resources"])
        out.append({
            "resource_type": g["resource_type"], "env": g["env"], "base_s": g["base"], "critical": g["critical"],
            "expected_s": g["expected"], "resources": n_res, "series": g["series"], "sparse_not_judged": g["sparse"],
            "age_p50_s": None if not ages else round(percentile(ages, 50)),
            "age_p90_s": None if not ages else round(percentile(ages, 90)),
            "age_max_s": None if not ages else max(ages),
            "verdict": judge_group(ages, n_res, g["base"], g["expected"], uptime_min) if (ages or g["expected"] is None)
            else ("NO_DATA" if not g["sparse"] else "SPARSE_ONLY"),
        })
    return out, sorted(late, key=lambda x: -(x["age_s"] - x["expected_s"]))


def _env_rank(label):
    return {"prod": 0, "unclassified": 1, "nonprod": 2}.get(label, 9)


# ======================================================================================
# 3. NON-PROD alerts (pure)
# ======================================================================================
def alert_findings(rows):
    """Open alerts on NON-PROD resources, judged as if the flag were ON.

    rows: id, resource_id, resource_type, name, account, env, metric_name, status, severity, age_min,
          stale_on_min, stale_off_min, hard_expiry_h (and optionally seen_at, a datetime).
    -> list of findings {level, kind, alert_id, ..., message}"""
    out = []
    p1 = _p1()
    for r in rows:
        env = ep.normalize_env(r.get("env"))
        if env != ep.NONPROD:
            continue
        rtype, metric = r["resource_type"], (r["metric_name"] or "").lower()
        critical = (rtype, metric) in p1
        base = pm.base_interval_seconds("aws", rtype, metric)
        evaluate = ep.evaluate_for_alerts(env, critical, rtype, force_enabled=True, base_interval=base)
        eff = None if base is None else ep.effective_interval(env, base, critical, rtype, force_enabled=True)
        stale_on = r.get("stale_on_min")
        head = {"alert_id": r["id"], "account": r.get("account"), "resource_id": r["resource_id"],
                "name": r.get("name"), "resource_type": rtype, "env": env, "metric": metric,
                "status": r.get("status"), "severity": r.get("severity")}
        if base is not None and (not evaluate or eff is None):
            when = f"{stale_on} min" if stale_on is not None else "its stale limit"
            exp = f", then closed as no_data_expired {r.get('hard_expiry_h')} h after it was last seen" \
                if r.get("hard_expiry_h") is not None else ""
            out.append({**head, "level": WARN, "kind": "FALSE_STALE",
                        "message": f"NON-PROD non-critical metric is no longer evaluated: this {r.get('status')} alert is not "
                                   f"refreshed, shows Stale after {when}{exp}. The condition may still hold or have "
                                   f"cleared; nothing will resolve it by recovery."})
            continue
        if base is None:
            continue
        slack = slack_seconds(base)
        if eff and stale_on is not None and stale_on * 60 < eff + slack:
            out.append({**head, "level": FAIL, "kind": "STALE_LIMIT_TIGHT",
                        "message": f"stale limit {stale_on} min is shorter than the polling gap "
                                   f"({eff // 60} min + {slack // 60} min tolerance): the alert would flip to Stale "
                                   f"between polls"})
            continue
        scale = ep.alert_for_scale(env, base, critical, rtype, force_enabled=True)
        if scale > 1:
            out.append({**head, "level": INFO, "kind": "SLOWER_LIFECYCLE",
                        "message": f"polled every {eff // 60} min: needs {scale}x the breach / healthy cycles "
                                   f"(about {scale * ep.CYCLE_SECONDS // 60} min) to open or to resolve"})
    return out


# ======================================================================================
# 4. unplaced (env_source='unknown') EC2 / EBS on the 5-minute tier (pure)
# ======================================================================================
_ENVISH = tuple(ec.TAG_KEYS)
TIER_HINT = "prod|production|prd  /  nonprod|dev|test|sit|uat|qa|staging|preprod|sandbox"


def unknown_advice(rows, parents_by_pk, siblings):
    """rows: unplaced (env_source='unknown') EC2/EBS resource dicts. parents_by_pk: {row id: [parent dicts]} (attached_to edges).
    siblings: classified resources of the same accounts (for ec.SuggestionIndex), dicts with resource_id, name,
    tags, env, source, aws_account_id. -> one dict per row, nothing is written."""
    idx_by_acct = {}
    for s in siblings:
        idx_by_acct.setdefault(s.get("aws_account_id"), []).append(s)
    indexes = {}
    out = []
    for r in rows:
        acct = r.get("aws_account_id")
        if acct not in indexes:
            indexes[acct] = ec.SuggestionIndex(idx_by_acct.get(acct, []))
        tags = ec._tags_dict(r.get("tags"))
        folded = {re.sub(r"[-.\s]+", "_", str(k).strip().lower()): (k, v) for k, v in tags.items()}
        envish = [(folded[k][0], folded[k][1]) for k in _ENVISH if k in folded]
        needs = []
        if not envish:
            needs.append("add the tag Environment=prod|nonprod (accepted keys: " + ", ".join(_ENVISH) + ")")
        else:
            k, v = envish[0]
            needs.append(f"tag {k}={v!r} holds no single tier word: use prod or nonprod (dev / test / sit / uat / qa / staging all mean nonprod)")
        name = r.get("name")
        tiers, words = ec._scan(name) if name else ([], [])
        if not name:
            needs.append("it has no Name: a name containing one tier word also classifies it")
        elif len(set(tiers)) > 1:
            needs.append(f"name {name!r} contains conflicting tier words {sorted(set(words))}: keep one")
        else:
            needs.append(f"or put one tier word in the name ({TIER_HINT}); {name!r} has none")
        parents = parents_by_pk.get(r["id"], [])
        copied = tags.get("parent_ec2") if r["resource_type"] == "ebs" else None
        if copied:
            needs.insert(0, f"this volume carries the tags of its instance {copied}: tag THAT instance with "
                            f"Environment=prod|nonprod and the volume follows after the next discovery")
        if r["resource_type"] == "ebs":
            if parents:
                p = parents[0]
                needs.append(f"or classify the attached {p['resource_type']} {p.get('name') or p['resource_id']} "
                             f"(now {p.get('env_resolved') or 'unclassified'}/{p.get('env_source') or '-'}): "
                             f"the volume follows its parent")
            else:
                needs.append("no attached_to edge: the volume is not attached or topology has not synced, so it "
                             "cannot follow an instance")
        sug = indexes[acct].suggest({"resource_id": r["resource_id"], "name": name, "tags": tags,
                                     "resource_type": r["resource_type"]})
        suggestion = None if sug is None else {"env": sug.env, "confidence": sug.confidence, "basis": sug.basis,
                                                "reason": sug.reason}
        base = pm.TIER_SECONDS["standard"]
        out.append({
            "account": r.get("account_name"), "resource_type": r["resource_type"], "resource_id": r["resource_id"],
            "name": name, "state": r.get("instance_state"), "env_source": r.get("env_source"),
            "tag_state": ec.tag_state(r["resource_type"], tags),
            "why_unknown": r.get("env_reason") or "no override, tag, name token or account default",
            "env_tags_present": [f"{k}={v}" for k, v in envish], "needs": needs, "suggestion": suggestion,
            "if_nonprod_every_s": ep.effective_interval("nonprod", base, False, r["resource_type"], True),
        })
    return out


# ======================================================================================
# database layer (SELECT only)
# ======================================================================================
def _only_select(sql):
    s = sql.lstrip().upper()
    if not s.startswith("SELECT"):
        raise RuntimeError("env_polling_verify is read-only: refusing a non-SELECT statement")
    return sql


def _q(cur, sql, params=()):
    cur.execute(_only_select(sql), params)
    return cur.fetchall()


_COLLECTED = f"""
      AND (r.instance_state IS NULL OR r.instance_state != 'terminated')
      AND (r.last_seen_at IS NULL OR r.last_seen_at >= DATE_SUB(NOW(), INTERVAL {STALE_RESOURCE_HOURS} HOUR))
      AND NOT (r.resource_type = 'ec2' AND COALESCE(r.instance_state, '') != 'running')
      AND NOT (r.resource_type = 'ebs'
               AND COALESCE(JSON_UNQUOTE(JSON_EXTRACT(r.tags, '$._ebs_state')), '') = 'available')
      AND r.resource_type NOT IN ('ecs', 'ecs_service', 'eni')
"""


def fetch_series(cur, account_id=None):
    acct = " AND r.aws_account_id = %s" if account_id else ""
    return _q(cur, f"""
        SELECT r.id AS resource_pk, r.resource_id, r.resource_type, r.name, aa.account_name AS account,
               r.env_resolved AS env, m.metric_name,
               TIMESTAMPDIFF(SECOND, m.metric_timestamp, UTC_TIMESTAMP()) AS age_s
        FROM resources r
        JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active' AND aa.provider = 'aws'
        JOIN metrics m ON m.resource_id = r.id
        WHERE m.metric_timestamp IS NOT NULL {_COLLECTED} {acct}
    """, (account_id,) if account_id else ())


@contextmanager
def _flag_state(on):
    """Build SQL / evaluate with the flag forced ON or OFF, then restore. The alert SQL builders read
    env_polling.enabled() when they are called, so this yields the SQL of each state."""
    import app.alert_rules as alert_rules
    names = ("ENV_POLLING_ENABLED", "ENV_AWARE_CLASSIFICATION")
    saved = {n: os.environ.get(n) for n in names}
    try:
        for n in names:
            os.environ[n] = "true" if on else "false"
        alert_rules._ENV_EXTRA_CACHE.clear()
        yield alert_rules
    finally:
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
        alert_rules._ENV_EXTRA_CACHE.clear()


def fetch_alerts(cur, account_id=None):
    with _flag_state(False) as ar:
        off = ar.stale_minutes_sql("r", "aa", "a.metric_name")
    with _flag_state(True) as ar:
        on = ar.stale_minutes_sql("r", "aa", "a.metric_name")
        hard = ar.hard_expiry_hours_sql("r", "aa")
    acct = " AND a.aws_account_id = %s" if account_id else ""
    if account_id:       # the builders emit LIKE '...%'; with bound parameters a bare % must be doubled
        off, on, hard = (x.replace("%", "%%") for x in (off, on, hard))
    return _q(cur, f"""
        SELECT a.id, a.resource_id, a.metric_name, a.status, a.severity, a.triggered_at, a.last_seen_at,
               COALESCE(a.last_seen_at, a.triggered_at) AS seen_at,
               TIMESTAMPDIFF(MINUTE, COALESCE(a.last_seen_at, a.triggered_at), UTC_TIMESTAMP()) AS age_min,
               a.environment AS alert_environment,
               r.resource_type, r.name, r.env_resolved AS env, aa.account_name AS account,
               ({off}) AS stale_off_min, ({on}) AS stale_on_min, ({hard}) AS hard_expiry_h
        FROM alerts a
        JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
        JOIN aws_accounts aa ON aa.id = a.aws_account_id AND aa.status = 'active' AND aa.provider = 'aws'
        WHERE a.status IN ('active', 'acknowledged') AND r.env_resolved = 'nonprod' {acct}
        ORDER BY aa.account_name, r.env_resolved, a.triggered_at
    """, (account_id,) if account_id else ())


def fetch_unknown(cur, account_id=None):
    acct = " AND r.aws_account_id = %s" if account_id else ""
    params = (account_id,) if account_id else ()
    rows = _q(cur, f"""
        SELECT r.id, r.aws_account_id, aa.account_name, r.resource_id, r.resource_type, r.name, r.tags,
               r.instance_state, r.region, r.env_resolved, r.env_source, r.env_reason
        FROM resources r
        JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active' AND aa.provider = 'aws'
        WHERE r.resource_type IN ('ec2', 'ebs')
          AND r.env_source = 'unknown' {_COLLECTED} {acct}
        ORDER BY aa.account_name, r.resource_type, r.name, r.resource_id
    """, params)
    if not rows:
        return rows, {}, []
    accts = sorted({r["aws_account_id"] for r in rows})
    ph = ", ".join(["%s"] * len(accts))
    edges = _q(cur, f"""
        SELECT aws_account_id, source_resource_id AS parent, target_resource_id AS child
        FROM resource_relationships WHERE relationship_type = 'attached_to' AND aws_account_id IN ({ph})
    """, accts)
    parents_rows = _q(cur, f"""
        SELECT r.id, r.aws_account_id, r.resource_id, r.resource_type, r.name, r.env_resolved, r.env_source
        FROM resources r WHERE r.aws_account_id IN ({ph}) AND r.resource_type = 'ec2'
    """, accts)
    by_key = {(p["aws_account_id"], p["resource_id"]): p for p in parents_rows}
    parents = defaultdict(list)
    for r in rows:
        for e in edges:
            if e["aws_account_id"] == r["aws_account_id"] and e["child"] == r["resource_id"]:
                p = by_key.get((e["aws_account_id"], e["parent"]))
                if p:
                    parents[r["id"]].append(p)
    sibs = _q(cur, f"""
        SELECT r.aws_account_id, r.resource_id, r.name, r.tags, r.env_resolved AS env, r.env_source AS source
        FROM resources r WHERE r.aws_account_id IN ({ph}) AND r.env_resolved IN ('prod', 'nonprod')
          AND COALESCE(r.env_source, '') != 'unknown'
    """, accts)
    return rows, dict(parents), sibs


def table_state(cur):
    """(migration 083 columns present?, number of open alerts)"""
    have = _q(cur, "SELECT COUNT(*) AS n FROM information_schema.columns WHERE table_schema = DATABASE() "
                   "AND table_name = 'resources' AND column_name IN ('env_resolved', 'env_source', 'env_reason')")
    return have[0]["n"] == 3


# ======================================================================================
# rendering
# ======================================================================================
def _min(s):
    return "-" if s is None else f"{s / 60.0:.1f}"


def _fmt_iv(s):
    return "off" if s is None else (f"{s // 60}m" if s % 60 == 0 and s < 7200 else
                                    (f"{s // 3600}h" if s % 3600 == 0 else f"{s}s"))


def render_config(st, violations, matrix):
    f = st["flags"]
    out = ["== 1. FLAGS AND SETTINGS"]
    out.append(f"   ENV_POLLING_ENABLED={'true' if f['requested'] else 'false/unset'}   "
               f"ENV_AWARE_CLASSIFICATION={'true' if f['classification'] else 'false/unset'}   "
               f"=> env polling is {'ON' if f['enabled'] else 'OFF'} (both must be true)")
    out.append(f"   {'name':38}{'raw (.env)':16}{'used by code':14}status")
    for r in st["rows"]:
        out.append(f"   {r['name']:38}{('(unset)' if r['raw'] is None else str(r['raw'])):16}{str(r['effective']):14}"
                   f"{r['status']}")
    for w in st["warnings"]:
        out.append(f"   ! {w}")
    out.append("")
    out.append("== 1b. INTERVAL RULES over every scheduled AWS metric  (base -> effective, flag ON)")
    out.append(f"   {'tier base':>10} {'class':10}{'prod':>14}{'nonprod':>10}")
    for m in matrix:
        out.append(f"   {_fmt_iv(m['base']):>10} {'critical' if m['critical'] else 'normal':10}"
                   f"{_fmt_iv(m['prod']):>14}{_fmt_iv(m['nonprod']):>10}")
    out.append("   (wafv2, daily metrics and the free Describe-API metrics are never slowed and are not listed)")
    if violations:
        out.append(f"   FAIL: {len(violations)} rule violation(s)")
        out += [f"     - {v}" for v in violations[:40]]
    else:
        out.append("   OK: no rule violated (never faster than base; flag OFF == prod; PROD, wafv2, daily and "
                   "Describe metrics unchanged)")
    return "\n".join(out)


def render_series(groups, late, on, uptime_min):
    out = ["== 2. POLLING CADENCE  (configured vs age of the last stored value; flag state under test: "
           + ("ON" if on else "OFF") + (f"; service up {uptime_min} min" if uptime_min is not None else "") + ")"]
    hdr = (f"   {'type':14}{'env':13}{'base':>6}{'expect':>8}{'res':>5}{'series':>7}"
           f"{'p50 min':>9}{'p90 min':>9}{'max min':>9}  verdict")
    out.append(hdr)
    for g in groups:
        out.append(f"   {g['resource_type'][:13]:14}{g['env']:13}{_fmt_iv(g['base_s']):>6}{_fmt_iv(g['expected_s']):>8}"
                   f"{g['resources']:>5}{g['series']:>7}{_min(g['age_p50_s']):>9}{_min(g['age_p90_s']):>9}"
                   f"{_min(g['age_max_s']):>9}  {g['verdict']}"
                   + (f"  ({g['sparse_not_judged']} sparse not judged)" if g["sparse_not_judged"] else "")
                   + ("  [critical-class]" if g["critical"] else ""))
    counts = Counter(g["verdict"] for g in groups)
    out.append("   verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if any(g["verdict"] == "UNPROVEN" for g in groups):
        out.append("   UNPROVEN = a slowed group cannot be judged without --uptime-min (minutes since the restart)")
    nodata = [x for x in late if x.get("kind") == "NO_DATAPOINT"]
    late = [x for x in late if x.get("kind") != "NO_DATAPOINT"]
    if nodata:
        out.append(f"   NO RECENT DATAPOINT ({len(nodata)} series): the resource was collected on time but CloudWatch "
                   f"had no value for this metric (idle volume, metric not published). Not a schedule problem:")
        for x in nodata[:15]:
            out.append(f"     {x['account']}  {x['resource_type']} {x['resource_id']}  {x['env']}  {x['metric']}  "
                       f"last value {x['age_s'] / 60:.0f} min ago")
        if len(nodata) > 15:
            out.append(f"     ... and {len(nodata) - 15} more (--json lists all)")
    if late:
        out.append(f"   SERIES OLDER THAN EXPECTED + TOLERANCE ({len(late)}), worst first:")
        for x in late[:40]:
            out.append(f"     {x['account']}  {x['resource_type']} {x['resource_id']} ({x['name'] or '-'})  "
                       f"{x['env']}  {x['metric']}  age {_min(x['age_s'])} min, expected {_min(x['expected_s'])}")
        if len(late) > 40:
            out.append(f"     ... and {len(late) - 40} more (--json lists all)")
    return "\n".join(out)


def render_alerts(findings, n_rows):
    out = [f"== 3. OPEN NON-PROD ALERTS  ({n_rows} open; judged as if ENV_POLLING_ENABLED were ON)"]
    if not findings:
        out.append("   none affected")
        return "\n".join(out)
    c = Counter((f["level"], f["kind"]) for f in findings)
    out.append("   " + ", ".join(f"{lvl} {kind}={n}" for (lvl, kind), n in sorted(c.items())))
    for f in findings[:60]:
        out.append(f"   [{f['level']}] alert {f['alert_id']} {f['account']} {f['env']} {f['resource_type']} "
                   f"{f['resource_id']} / {f['metric']} ({f['severity']}, {f['status']}): {f['message']}")
    if len(findings) > 60:
        out.append(f"   ... and {len(findings) - 60} more (--json lists all)")
    return "\n".join(out)


def render_unknown(advice):
    out = [f"== 4. UNPLACED EC2 / EBS ON THE 5-MINUTE TIER, TREATED AS PROD  ({len(advice)}; nothing is changed)"]
    if not advice:
        out.append("   none")
        return "\n".join(out)
    c = Counter(a["resource_type"] for a in advice)
    out.append("   " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    out.append("   A label changes more than polling: with ENV_AWARE_CLASSIFICATION a NON-PROD label also turns WARNING "
               "into INFO. Label a resource only when it truly is not production.")
    for a in advice:
        out.append(f"   {a['account']}  {a['resource_type']} {a['resource_id']}  name={a['name'] or '-'}  "
                   f"state={a['state'] or '-'}  source={a['env_source'] or '-'}")
        out.append(f"       why unplaced: {a['why_unknown']}")
        for n in a["needs"]:
            out.append(f"       - {n}")
        if a["suggestion"]:
            s = a["suggestion"]
            out.append(f"       suggestion: {s['env']} ({s['confidence']}, {s['basis']}): {s['reason']}")
        out.append(f"       effect: prod = unchanged (5 min); nonprod = every {_fmt_iv(a['if_nonprod_every_s'])}")
    return "\n".join(out)


# ======================================================================================
def _inventory():
    import importlib.util
    spec = importlib.util.spec_from_file_location("polling_audit", os.path.join(_ROOT, "scripts", "polling_audit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.inventory()


def run(args):
    from dotenv import load_dotenv
    env_path = os.path.join(_ROOT, ".env")
    load_dotenv(env_path)
    entries = scan_env_file(env_path)
    st = settings_report(env_file_entries=entries)
    on = {"auto": st["flags"]["enabled"], "on": True, "off": False}[args.expect]
    wanted = set(args.only) if args.only else {"config", "series", "alerts", "unknown"}
    report = {"settings": st, "expect_on": on, "uptime_min": args.uptime_min}
    texts, fails = [], 0

    if "config" in wanted:
        violations, matrix = check_config(_inventory(), on=True)
        report["config"] = {"violations": violations, "matrix": matrix}
        texts.append(render_config(st, violations, matrix))
        fails += len(violations)

    if args.config_only or not (wanted & {"series", "alerts", "unknown"}):
        return report, texts, fails, 0

    try:
        from app.db import get_connection
        conn = get_connection()
    except Exception as e:                                # no DB / bad credentials: say so, exit 2, no traceback
        print(f"Cannot connect to the database: {type(e).__name__}: {e}", file=sys.stderr)
        return report, texts, fails, 2
    try:
        try:
            conn.start_transaction(readonly=True)
        except Exception:
            pass                                          # every statement is a SELECT regardless
        cur = conn.cursor(dictionary=True)
        try:
            if not table_state(cur):
                print("Migration 083 (resources.env_resolved) is not applied on this database: "
                      "run migrate.py apply --all-pending first.")
                return report, texts, fails, 2
            if "series" in wanted:
                groups, late = analyse_series(fetch_series(cur, args.account_id), on, args.uptime_min)
                report["series"] = {"groups": groups, "late": late}
                texts.append(render_series(groups, late, on, args.uptime_min))
                fails += sum(1 for g in groups if g["verdict"] in HARD_VERDICTS)
            if "alerts" in wanted:
                arows = fetch_alerts(cur, args.account_id)
                findings = alert_findings(arows)
                report["alerts"] = {"open_nonprod": len(arows), "findings": findings}
                texts.append(render_alerts(findings, len(arows)))
                fails += sum(1 for f in findings if f["level"] == FAIL)
            if "unknown" in wanted:
                rows, parents, sibs = fetch_unknown(cur, args.account_id)
                advice = unknown_advice(rows, parents, sibs)
                report["unknown_5min"] = advice
                texts.append(render_unknown(advice))
                if args.csv_unknown:
                    write_unknown_csv(advice, args.csv_unknown)
        finally:
            cur.close()
    finally:
        conn.close()
    return report, texts, fails, 0


def write_unknown_csv(advice, path):
    cols = ["account", "resource_type", "resource_id", "name", "state", "env_source", "why_unknown",
            "env_tags_present", "needs", "suggestion_env", "suggestion_confidence", "suggestion_reason",
            "if_nonprod_every_s"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for a in advice:
            s = a["suggestion"] or {}
            w.writerow({"account": a["account"], "resource_type": a["resource_type"], "resource_id": a["resource_id"],
                        "name": a["name"], "state": a["state"], "env_source": a["env_source"],
                        "why_unknown": a["why_unknown"], "env_tags_present": "; ".join(a["env_tags_present"]),
                        "needs": " | ".join(a["needs"]), "suggestion_env": s.get("env"),
                        "suggestion_confidence": s.get("confidence"), "suggestion_reason": s.get("reason"),
                        "if_nonprod_every_s": a["if_nonprod_every_s"]})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--expect", choices=("auto", "on", "off"), default="auto",
                    help="flag state the series verdicts are judged against (auto: as set in .env)")
    ap.add_argument("--uptime-min", type=int, default=None,
                    help="minutes since monitoring-hub was last restarted (needed to judge slowed groups)")
    ap.add_argument("--account-id", type=int, default=None, help="limit to one aws_accounts.id")
    ap.add_argument("--only", action="append", choices=("config", "series", "alerts", "unknown"),
                    help="run only this section (repeatable)")
    ap.add_argument("--config-only", action="store_true", help="no database access")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--csv-unknown", default=None, help="also write the unplaced EC2/EBS list to this CSV")
    args = ap.parse_args(argv)
    try:
        report, texts, fails, code = run(args)
    except Exception as e:
        if getattr(e, "errno", None) == 1054:
            print("Migration 083 (resources.env_resolved) is not applied on this database: "
                  "run migrate.py apply --all-pending first.")
            return 2
        raise
    if code:
        return code
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("\n\n".join(texts))
        print("\nRESULT: " + ("FAIL (%d hard finding(s))" % fails if fails else "no hard finding"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
