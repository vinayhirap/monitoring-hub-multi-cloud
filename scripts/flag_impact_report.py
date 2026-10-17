#!/usr/bin/env python3
"""
flag_impact_report.py -- READ-ONLY "what would change if I flip this flag" report (workstream D1).

Safe to run on production: it only runs SELECTs (the session is opened read-only) and writes
nothing except an optional --csv file on the machine you run it on. It never enables a flag.

It uses the evaluator's OWN functions, not a copy of the rules:
  * app.environment_classifier.severity_for / resolve_environment / is_prod_like
  * app.collector.alert_evaluator._classified_environment   (label with the flag ON)
  * app.collector.alert_evaluator._environment_from_tags    (label with the flag OFF, today)
The single rule that is not a function in the evaluator is its flag-OFF severity line
("prod"/"production" -> WARNING, anything else -> INFO); it is repeated here as
`severity_flag_off()` and covered by a test that runs the real evaluator and compares.

Sections
  ENV_AWARE_CLASSIFICATION   implemented: every OPEN alert (active/acknowledged), stored vs flag-on.
  PRIORITY_ENABLED           not implemented here yet: needs phase 4b (app/priority.py,
                             app/collector/priority_backfill.py) in this checkout. The report says
                             so instead of guessing.
  PRIORITY_ROUTING_ENABLED   same: needs phase 4b (notification channel min_priority, priority
                             escalation policies).

    cd /opt/monitoring-hub/app
    /opt/monitoring-hub/venv/bin/python3 scripts/flag_impact_report.py
    /opt/monitoring-hub/venv/bin/python3 scripts/flag_impact_report.py --account-id 7
    /opt/monitoring-hub/venv/bin/python3 scripts/flag_impact_report.py --csv /tmp/flag_impact.csv
"""
import argparse
import csv
import importlib.util
import json
import os
import sys
from collections import Counter, defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

OPEN_STATUSES = ("active", "acknowledged")
TOP_N = 20

CSV_FIELDS = [
    "alert_id", "account_id", "account_name", "resource_id", "resource_type", "resource_name",
    "metric_name", "status", "stored_severity", "stored_environment", "flag_off_label",
    "flag_on_environment", "env_source", "env_resolved_stored", "severity_if_raised_flag_off",
    "severity_if_raised_flag_on", "severity_change", "label_change", "has_resource_row",
]


# ----------------------------------------------------------------------------------------------
# Pure logic (no database): unit-testable
# ----------------------------------------------------------------------------------------------
def severity_flag_off(label):
    """What the evaluator assigns a WARNING-level breach with the flag OFF (alert_evaluator.py,
    the `elif environment in ("prod", "production")` branch). CRITICAL is unaffected by any
    environment logic and is never produced here."""
    return "WARNING" if label in ("prod", "production") else "INFO"


def analyse_alert(alert, ev, envc):
    """One open alert + its resource row (or None) -> a flat dict of before/after values.

    `ev` is the alert_evaluator module, `envc` is app.environment_classifier."""
    stored_sev = str(alert.get("severity") or "").upper()
    stored_env = alert.get("environment")
    res = alert.get("res")
    out = {
        "alert_id": alert["id"], "account_id": alert.get("aws_account_id"),
        "account_name": alert.get("account_name"), "resource_id": alert.get("resource_id"),
        "resource_type": (res or {}).get("resource_type") or "(no resource row)",
        "resource_name": (res or {}).get("name"), "metric_name": alert.get("metric_name"),
        "status": alert.get("status"), "stored_severity": stored_sev, "stored_environment": stored_env,
        "has_resource_row": res is not None,
        "env_resolved_stored": (res or {}).get("env_resolved"),
        "env_source": (res or {}).get("env_source"),
    }
    if res is None:
        # The evaluator joins metrics -> resources, so an alert with no resource row is never
        # re-evaluated: neither flag touches it. Reported, not guessed.
        out.update(flag_off_label=None, flag_on_environment=None, severity_if_raised_flag_off=None,
                   severity_if_raised_flag_on=None, severity_change="", label_change=False)
        return out

    row = {"env_resolved": res.get("env_resolved"), "tags": res.get("tags"),
           "aws_resource_id": alert.get("resource_id")}
    off_label = ev._environment_from_tags(res.get("tags"))
    on_env = ev._classified_environment(row)
    sev_off = severity_flag_off(off_label)
    sev_on = envc.severity_for(on_env, False)

    change = ""
    # Only WARNING/INFO alerts have an environment-dependent severity. An open CRITICAL stays
    # CRITICAL (see `critical_deescalation_differs` in the summary for the one exception).
    if stored_sev in ("WARNING", "INFO") and stored_sev != sev_on:
        change = f"{stored_sev}->{sev_on}"
    out.update(flag_off_label=off_label, flag_on_environment=on_env,
               severity_if_raised_flag_off=sev_off, severity_if_raised_flag_on=sev_on,
               severity_change=change, label_change=(stored_env != on_env))
    return out


def summarise(rows):
    """Aggregate analysed alerts into the numbers the report prints."""
    s = {
        "open_alerts": len(rows),
        "no_resource_row": sum(1 for r in rows if not r["has_resource_row"]),
        "severity_changes": Counter(r["severity_change"] for r in rows if r["severity_change"]),
        "label_changes": sum(1 for r in rows if r["label_change"]),
        "label_pairs": Counter((r["stored_environment"], r["flag_on_environment"])
                               for r in rows if r["label_change"]),
        "by_account": defaultdict(lambda: Counter()),
        "by_environment": defaultdict(lambda: Counter()),
        "by_resource_type": defaultdict(lambda: Counter()),
        "unclassified_resources": 0,
        "critical_open": sum(1 for r in rows if r["stored_severity"] == "CRITICAL"),
        "critical_deescalation_differs": 0,
    }
    seen_unclassified = set()
    for r in rows:
        if not r["has_resource_row"]:
            continue
        for bucket, key in ((s["by_account"], r["account_name"] or f"account {r['account_id']}"),
                            (s["by_environment"], r["flag_on_environment"]),
                            (s["by_resource_type"], r["resource_type"])):
            bucket[key]["alerts"] += 1
            if r["severity_change"]:
                bucket[key][r["severity_change"]] += 1
            if r["label_change"]:
                bucket[key]["label_change"] += 1
        if not r["env_resolved_stored"]:
            seen_unclassified.add((r["account_id"], r["resource_id"]))
        # An open CRITICAL that later reads below the critical line is de-escalated to the
        # environment's warning-level severity; with the flag on that target can differ.
        if r["stored_severity"] == "CRITICAL" and \
                r["severity_if_raised_flag_off"] != r["severity_if_raised_flag_on"]:
            s["critical_deescalation_differs"] += 1
    s["unclassified_resources"] = len(seen_unclassified)

    per_resource = defaultdict(lambda: {"affected": 0, "alerts": 0, "sample": None})
    for r in rows:
        if not r["has_resource_row"]:
            continue
        k = (r["account_id"], r["resource_id"])
        per_resource[k]["alerts"] += 1
        per_resource[k]["sample"] = per_resource[k]["sample"] or r
        if r["severity_change"] or r["label_change"]:
            per_resource[k]["affected"] += 1
    ranked = sorted((v for v in per_resource.values() if v["affected"]),
                    key=lambda v: (-v["affected"], str(v["sample"]["resource_id"])))
    s["top_resources"] = ranked[:TOP_N]
    return s


def render_text(s, account_id=None, in_place_note=""):
    L = []
    add = L.append
    add("FLAG IMPACT REPORT (read-only)" + (f" -- account {account_id}" if account_id else " -- all accounts"))
    add("=" * 72)
    add("")
    add("ENV_AWARE_CLASSIFICATION  (today: OFF)")
    add("-" * 72)
    add(f"open alerts (active + acknowledged)        : {s['open_alerts']}")
    add(f"  of which with no resource row            : {s['no_resource_row']}  (neither flag can affect these)")
    add(f"  open CRITICAL                            : {s['critical_open']}  (stay CRITICAL; see note 3)")
    add(f"alerts whose severity would differ         : {sum(s['severity_changes'].values())}")
    for k in ("WARNING->INFO", "INFO->WARNING"):
        add(f"    {k:<14}                        : {s['severity_changes'].get(k, 0)}")
    add(f"alerts whose environment label would differ: {s['label_changes']}")
    add(f"resources among them with no stored class. : {s['unclassified_resources']}  "
        "(classified live by the evaluator; run backfill_environments.py)")
    add("")
    if s["label_pairs"]:
        add("label change (stored -> with flag on), alerts:")
        for (a, b), n in sorted(s["label_pairs"].items(), key=lambda kv: (-kv[1], str(kv[0]))):
            add(f"    {str(a):<12} -> {str(b):<10} {n}")
        add("")
    for title, bucket in (("by account", s["by_account"]), ("by environment (flag on)", s["by_environment"]),
                          ("by resource type", s["by_resource_type"])):
        add(title + ":")
        add(f"    {'':<28}{'alerts':>7}{'W->I':>7}{'I->W':>7}{'label':>7}")
        for key, c in sorted(bucket.items(), key=lambda kv: (-kv[1]["alerts"], str(kv[0]))):
            add(f"    {str(key)[:27]:<28}{c['alerts']:>7}{c['WARNING->INFO']:>7}"
                f"{c['INFO->WARNING']:>7}{c['label_change']:>7}")
        add("")
    add(f"{TOP_N} most affected resources:")
    if not s["top_resources"]:
        add("    (none)")
    for v in s["top_resources"]:
        r = v["sample"]
        add(f"    {str(r['resource_id'])[:34]:<35}{str(r['resource_type']):<14}"
            f"{v['affected']}/{v['alerts']} alerts affected   {r['stored_environment']} -> {r['flag_on_environment']}"
            f"  ({r['env_source'] or 'unclassified'})")
    add("")
    add("When each change takes effect (read from app/collector/alert_evaluator.py):")
    add(in_place_note or "  (see notes)")
    add("  1. The numbers above are for alerts that exist NOW. New alerts raised after the flag is on get")
    add("     the flag-on severity and label straight away; this report cannot predict which metrics will")
    add("     breach in future.")
    add("  2. 'severity would differ' = what the evaluator assigns a warning-level breach of this")
    add("     resource with the flag on, versus the severity stored on the open alert.")
    add("  3. An open CRITICAL is never lowered by the flag itself. It is lowered only when its reading")
    add("     later drops below the critical line (existing de-escalation), and then to the environment's")
    add(f"     warning-level severity. {s['critical_deescalation_differs']} open CRITICAL alert(s) would land on a "
        "different level with the flag on than today.")
    add("  4. Nothing here suppresses an alert: unknown and ignore stay prod-like (WARNING).")
    add("")
    add("PRIORITY_ENABLED / PRIORITY_ROUTING_ENABLED")
    add("-" * 72)
    add(_priority_section())
    return "\n".join(L) + "\n"


def _priority_section():
    have = all(importlib.util.find_spec(m) is not None
               for m in ("app.priority", "app.collector.priority_backfill"))
    if not have:
        return ("NOT AVAILABLE in this checkout: phase 4b (app/priority.py, app/collector/priority_backfill.py,\n"
                "notification min_priority, escalation_priority_policies) is not present, so there is\n"
                "nothing to measure. Deploy patch 0014 first, then re-run this report.")
    return ("Phase 4b code is present in this checkout, but this version of the report does not measure\n"
            "it yet (no P1-P4 distribution, no channel routing or escalation comparison). Do not read the\n"
            "absence of numbers here as 'no impact'.")


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ----------------------------------------------------------------------------------------------
# Database access (SELECT only)
# ----------------------------------------------------------------------------------------------
def fetch_open_alerts(conn, account_id=None):
    """Open alerts with their resource row. Raises mysql errno 1054 if migration 083 is missing."""
    cur = conn.cursor(dictionary=True)
    params = list(OPEN_STATUSES)
    scope = ""
    if account_id is not None:
        scope = " AND a.aws_account_id = %s"
        params.append(account_id)
    cur.execute(f"""
        SELECT a.id, a.aws_account_id, a.resource_id, a.metric_name, a.severity, a.status,
               a.environment, a.group_key, aa.account_name,
               r.id AS res_pk, r.resource_type, r.name AS res_name, r.tags,
               r.env_resolved, r.env_source
        FROM alerts a
        LEFT JOIN aws_accounts aa ON aa.id = a.aws_account_id
        LEFT JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
        WHERE a.status IN (%s, %s){scope}
        ORDER BY a.id, r.id
    """, params)
    raw = cur.fetchall()
    cur.close()

    # An alert's group_key is "<account>:<resource_type>:<metric>". If the same resource id exists
    # under two resource types (an ALB and an NLB are both 'elb'), prefer the row whose type matches.
    by_alert = {}
    for r in raw:
        want = None
        parts = str(r.get("group_key") or "").split(":")
        if len(parts) >= 3:
            want = parts[1]
        cur_best = by_alert.get(r["id"])
        res = None
        if r["res_pk"] is not None:
            res = {"resource_type": r["resource_type"], "name": r["res_name"], "tags": r["tags"],
                   "env_resolved": r["env_resolved"], "env_source": r["env_source"]}
        better = cur_best is None or (res is not None and cur_best["res"] is None) or \
            (res is not None and want and res["resource_type"] == want and cur_best["res"] is not None
             and cur_best["res"]["resource_type"] != want)
        if better:
            by_alert[r["id"]] = {**r, "res": res}
    return list(by_alert.values())


def build_report(conn, ev, envc, account_id=None):
    alerts = fetch_open_alerts(conn, account_id)
    rows = [analyse_alert(a, ev, envc) for a in alerts]
    return rows, summarise(rows)


def _in_place_note(ev):
    """The evaluator source decides what we can claim about open alerts."""
    try:
        import inspect
        src = inspect.getsource(ev)
    except Exception:
        src = ""
    if "PRIORITY_ENABLED" in src:
        return ("  - New alerts: flag-on severity and label immediately.\n"
                "  - Open alerts: severity of a WARNING/INFO alert is not rewritten in place; the label is\n"
                "    re-written in place only when PRIORITY_ENABLED is also on (phase 4b), on the alert's\n"
                "    next evaluation. Confirm in the evaluator before relying on this line.")
    return ("  - New alerts: flag-on severity and label immediately (set when the alert is raised).\n"
            "  - Open alerts: in this checkout the evaluator does NOT rewrite an open alert's environment\n"
            "    label or its WARNING/INFO severity. They keep today's values until the alert resolves and\n"
            "    is raised again. Only a CRITICAL being lowered (note 3) picks up the new rule.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int, default=None, help="limit to one aws_accounts.id")
    ap.add_argument("--csv", default=None, help="also write one row per open alert to this path")
    args = ap.parse_args(argv)

    # app.db refuses to import without DB_PASSWORD; load the service's .env like the other scripts.
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))

    from app.db import get_connection
    from app import environment_classifier as envc
    from app.collector import alert_evaluator as ev

    conn = get_connection()
    try:
        try:
            conn.start_transaction(readonly=True)
        except Exception:
            pass  # older connector: the script only issues SELECTs regardless
        try:
            rows, summary = build_report(conn, ev, envc, args.account_id)
        except Exception as e:
            if getattr(e, "errno", None) == 1054:
                print("Migration 083 (environment columns) is not applied on this database: "
                      "run migrate.py apply --all-pending first.")
                return 2
            raise
    finally:
        conn.close()

    print(render_text(summary, args.account_id, _in_place_note(ev)))
    if args.csv:
        write_csv(rows, args.csv)
        print(f"wrote {len(rows)} row(s) to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
