#!/usr/bin/env python3
"""
apply_fix_threshold_resource_type_everywhere.py
========================================
Closes a real, still-live gap in the earlier ALB/NLB threshold fix
(apply_fix_alb_nlb_threshold_resource_type.py): that fix only patched
app/api/settings.py's two write sites. It missed a THIRD, completely
separate INSERT INTO thresholds in app/api/metric_catalog.py's
_sync_thresholds_for_selection() -- which onboarding auto-detection,
the "Apply Default Template" button, and every Settings -> Metrics to
Monitor checkbox change all funnel through. Confirmed by reproducing
the exact bug against the pre-fix code (writes resource_type='alb')
and confirming the fix (writes resource_type='elb') in an isolated
test -- not assumed from reading alone.

Practical impact: any account onboarded, or any account whose metric
selection changed (including the periodic auto-enable-newly-discovered-
services cycle), AFTER the original fix shipped would still get fresh
ALB/NLB threshold rows with the broken resource_type -- silently
undoing the original fix's effect for anyone except accounts that
already had their thresholds seeded and backfilled before that fix.

ALSO CLOSES THE DUPLICATION THAT CAUSED THIS
--------------------------------------------------
The original fix defined its normalization map/function locally inside
settings.py. app/threshold_defaults.py already exists specifically to
prevent this class of drift -- its own docstring says so ("Single
source of truth used by both... so the two can never drift apart") --
but the ALB/NLB fix didn't use it. This script moves
normalize_threshold_resource_type() there for real, and updates BOTH
settings.py and metric_catalog.py to import it from the one shared
place, so there's structurally only one copy to ever fix again.

A SEPARATE, ALREADY-LIVE-BUT-UNCOMMITTED FIX, HANDLED HERE TOO
--------------------------------------------------------------------
A different fix (apply_fix_has_data_case_bug.py, for a has_data
case-sensitivity bug in the same file) was applied to this server's
disk in an earlier step but never committed to git -- confirmed by
checking git history, which has no trace of it. This script's
settings.py patch is written to work correctly whether that fix is
already present on disk or not (it detects which state the file is in
and produces the same correct final result either way), so applying
this script also captures that earlier fix into git if it wasn't
already, rather than requiring a separate reconciliation step.

TESTED: reproduced the actual bug first (pre-fix code writes
resource_type='alb' when onboarding auto-detects an ALB), confirmed the
fix produces resource_type='elb' for the exact same input, using the
real _sync_thresholds_for_selection() function (not a reimplementation
of it).

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_threshold_resource_type_everywhere.py --dry-run
    python3 apply_fix_threshold_resource_type_everywhere.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

# ── app/threshold_defaults.py: add the shared normalization ──────────

TD_OLD = '''# Used when a metric_name has no explicit entry above (e.g. a "directory"
# metric discovered live via ListMetrics that isn't in the curated catalog).
FALLBACK_THRESHOLD = (1000000, 5000000, ">")'''

TD_NEW = '''# Used when a metric_name has no explicit entry above (e.g. a "directory"
# metric discovered live via ListMetrics that isn't in the curated catalog).
FALLBACK_THRESHOLD = (1000000, 5000000, ">")

# metric_catalog.service ("alb", "nlb") is the correct catalog/display value
# for those two ALB/NLB metric_catalog entries -- this map is NOT about
# changing that. It exists because app/collector/discovery/runner.py stores
# ALL Elastic Load Balancing v2 resources (both ALB and NLB; this codebase
# doesn't distinguish them at discovery time) under resources.resource_type
# = "elb" uniformly, while thresholds.resource_type needs to match THAT
# value for app/collector/alert_evaluator.py's scheduled JOIN
# (`t.resource_type = r.resource_type`) to ever succeed -- it has no
# service-name fallback, unlike check_and_write_alerts()'s own separate
# LOCAL_RESOURCE_TYPE map for the same translation.
#
# Lives HERE, not duplicated in settings.py/metric_catalog.py separately,
# for the exact reason this module's own docstring already states above:
# so the various places that write thresholds.resource_type can never
# drift apart. Originally fixed only in app/api/settings.py
# (apply_fix_alb_nlb_threshold_resource_type.py) -- that missed
# app/api/metric_catalog.py's OWN separate INSERT INTO thresholds in
# _sync_thresholds_for_selection(), which onboarding auto-detect, "Apply
# Default Template", and every Settings -> Metrics to Monitor checkbox
# change all funnel through. Moved here and both call sites updated to
# use it, closing that gap for good. See
# apply_fix_threshold_resource_type_everywhere.py.
THRESHOLD_RESOURCE_TYPE_ALIASES = {"alb": "elb", "nlb": "elb"}


def normalize_threshold_resource_type(value):
    return THRESHOLD_RESOURCE_TYPE_ALIASES.get(value, value)'''

# ── app/api/metric_catalog.py: fix the previously-missed write site ──

MC_IMPORT_OLD = "from app.threshold_defaults import DEFAULT_THRESHOLDS, FALLBACK_THRESHOLD"
MC_IMPORT_NEW = "from app.threshold_defaults import DEFAULT_THRESHOLDS, FALLBACK_THRESHOLD, normalize_threshold_resource_type"

MC_WRITE_OLD = '''                cur.execute("""
                    INSERT IGNORE INTO thresholds
                      (aws_account_id, resource_type, metric_id,
                       warning_value, critical_value, comparison, evaluation_period, enabled)
                    VALUES (%s,%s,%s,%s,%s,%s,5,1)
                """, (account_id, service, mid, warn, crit, comp))'''

MC_WRITE_NEW = '''                cur.execute("""
                    INSERT IGNORE INTO thresholds
                      (aws_account_id, resource_type, metric_id,
                       warning_value, critical_value, comparison, evaluation_period, enabled)
                    VALUES (%s,%s,%s,%s,%s,%s,5,1)
                """, (account_id, normalize_threshold_resource_type(service), mid, warn, crit, comp))'''

# ── app/api/settings.py: two possible starting states ─────────────────
# State A: git's current committed state (never got the case-sensitivity
#          fix, has its own local resource_type alias map/function).
# State B: the case-sensitivity fix was applied live but never committed
#          (has case-insensitive has_data logic, but STILL has its own
#          local resource_type alias map/function -- that fix never
#          touched the resource_type normalization).
# Both converge to the same final state: case-insensitive has_data check,
# resource_type normalization imported from the shared module.

SETTINGS_IMPORT_OLD = "from app.threshold_defaults import DEFAULT_THRESHOLDS, FALLBACK_THRESHOLD"
SETTINGS_IMPORT_NEW = "from app.threshold_defaults import DEFAULT_THRESHOLDS, FALLBACK_THRESHOLD, normalize_threshold_resource_type"

# The local alias map/function block is IDENTICAL in both states A and B
# (the case-sensitivity fix never touched this part) -- one pattern covers both.
SETTINGS_LOCAL_MAP_OLD = '''# metric_catalog.service ("alb", "nlb") is the correct catalog/display
# value and is NOT changed by this map -- but
# app/collector/discovery/runner.py stores ALL Elastic Load Balancing v2
# resources (both ALB and NLB; this codebase doesn't distinguish them at
# discovery time) under resources.resource_type = "elb" uniformly.
# alert_evaluator.py's core scheduled evaluation JOINs
# thresholds.resource_type directly against resources.resource_type with
# no service-name fallback (unlike check_and_write_alerts() /
# app/aws/collector_direct.py, which already has its own separate
# LOCAL_RESOURCE_TYPE map for this same translation -- see Phase 5,
# apply_check_thresholds_local_metrics.py). Without this normalization,
# ALB/NLB thresholds are silently unevaluable by the scheduled evaluator
# forever, no matter what value they're set to. See
# apply_fix_alb_nlb_threshold_resource_type.py for the full story.
_THRESHOLD_RESOURCE_TYPE_ALIASES = {"alb": "elb", "nlb": "elb"}


def _normalize_threshold_resource_type(value):
    return _THRESHOLD_RESOURCE_TYPE_ALIASES.get(value, value)


'''

SETTINGS_LOCAL_MAP_NEW = '''# ALB/NLB resource_type normalization now lives in app/threshold_defaults.py
# (normalize_threshold_resource_type) so every place that writes
# thresholds.resource_type -- this file's two call sites AND
# app/api/metric_catalog.py's separate _sync_thresholds_for_selection(),
# which the original fix here missed entirely -- shares one definition
# instead of drifting copies. See
# apply_fix_threshold_resource_type_everywhere.py for why this moved.

'''

# has_data helper: State A (case-sensitive, buggy) vs State B (already
# case-insensitive from the uncommitted live fix). Try A first, then B.
SETTINGS_HELPER_STATE_A_OLD = '''def _metrics_with_data_for_account(account_id: int) -> set:
    """
    {(resource_type, metric_name), ...} -- every (resource_type,
    metric_name) combination that has AT LEAST ONE row in the `metrics`
    last-value cache for a resource belonging to this account. Used to
    hide threshold rows for metrics that have never actually produced
    data for this account (extended-tier metrics with no collector
    built, a service the account has zero resources of, etc.) -- a
    threshold on a metric that can never have a value is just clutter,
    not something to configure. One query, not one per threshold row.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT r.resource_type, m.metric_name
        FROM metrics m
        JOIN resources r ON r.id = m.resource_id
        WHERE r.aws_account_id = %s
    """, (account_id,))
    pairs = set(cur.fetchall())
    cur.close(); conn.close()
    return pairs'''

SETTINGS_HELPER_STATE_B_OLD = '''def _metrics_with_data_for_account(account_id: int) -> set:
    """
    {(resource_type, metric_name_lower), ...} -- every (resource_type,
    metric_name) combination that has AT LEAST ONE row in the `metrics`
    last-value cache for a resource belonging to this account.
    metric_name is lowercased here because metric_catalog.metric_name
    stores CloudWatch-style names ("CPUUtilization") while
    app/collector/metrics/runner.py's write_metric() writes its own
    lowercase db_metric_name convention ("cpuutilization") into `metrics`
    -- comparing them as plain Python strings without normalizing case
    would incorrectly treat every AWS metric as having no data, since
    the two sides never match by construction. (SQL comparisons
    elsewhere in this app, e.g. alert_evaluator.py's JOIN, happen to work
    despite this because MySQL's default collation is case-insensitive;
    this is a plain Python set membership check, which is not.) Callers
    must also .lower() the metric_name they're checking against this set.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT r.resource_type, m.metric_name
        FROM metrics m
        JOIN resources r ON r.id = m.resource_id
        WHERE r.aws_account_id = %s
    """, (account_id,))
    pairs = {(resource_type, metric_name.lower()) for resource_type, metric_name in cur.fetchall()}
    cur.close(); conn.close()
    return pairs'''

SETTINGS_HELPER_NEW = SETTINGS_HELPER_STATE_B_OLD  # State B's body is already the correct final form.

SETTINGS_COMPARISON_STATE_A_OLD = '''        has_data = (r["resource_type"], r["metric_name"]) in has_data_pairs'''
SETTINGS_COMPARISON_NEW = '''        has_data = (r["resource_type"], (r["metric_name"] or "").lower()) in has_data_pairs'''
# (State B already has SETTINGS_COMPARISON_NEW verbatim -- no change needed there.)


def die(msg):
    print(f"\n[ABORT] {msg}", file=sys.stderr)
    sys.exit(1)


def find_repo_root():
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(cur, "app", "auth", "security.py")) and \
           os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            die("Could not locate the monitoring-hub-multi-cloud repo root.")
        cur = parent


def backup(path):
    bpath = path + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(path, bpath)
    return bpath


def try_replace(content, old, new, label):
    """Returns (new_content, True) if old was found exactly once and replaced,
    (content, False) if not found at all. Dies if found more than once."""
    n = content.count(old)
    if n == 0:
        return content, False
    if n > 1:
        die(f"{label}: expected at most 1 match, found {n}. File may differ from what this script expects.")
    return content.replace(old, new, 1), True


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="(default; kept for backward compatibility)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    repo_root = find_repo_root()
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    td_path = os.path.join(repo_root, "app", "threshold_defaults.py")
    mc_path = os.path.join(repo_root, "app", "api", "metric_catalog.py")
    settings_path = os.path.join(repo_root, "app", "api", "settings.py")

    plan = []

    # ── threshold_defaults.py ──
    with open(td_path) as f:
        td_content = f.read()
    td_done = "normalize_threshold_resource_type" in td_content
    if td_done:
        plan.append(("threshold_defaults.py", None, "already has normalize_threshold_resource_type -- skipping"))
    else:
        new_td, found = try_replace(td_content, TD_OLD, TD_NEW, "threshold_defaults.py")
        if not found:
            die("threshold_defaults.py: anchor text not found -- file may differ from what this script expects.")
        plan.append(("threshold_defaults.py", new_td, "OK"))

    # ── metric_catalog.py ──
    with open(mc_path) as f:
        mc_content = f.read()
    mc_done = "normalize_threshold_resource_type(service)" in mc_content
    if mc_done:
        plan.append(("metric_catalog.py", None, "already fixed -- skipping"))
    else:
        new_mc = mc_content
        new_mc, found_import = try_replace(new_mc, MC_IMPORT_OLD, MC_IMPORT_NEW, "metric_catalog.py import")
        new_mc, found_write = try_replace(new_mc, MC_WRITE_OLD, MC_WRITE_NEW, "metric_catalog.py write")
        if not (found_import and found_write):
            die("metric_catalog.py: expected anchors not found -- file may differ from what this script expects.")
        plan.append(("metric_catalog.py", new_mc, "OK"))

    # ── settings.py (handles both possible starting states) ──
    with open(settings_path) as f:
        s_content = f.read()
    s_done = "from app.threshold_defaults import" in s_content and \
             "normalize_threshold_resource_type" in s_content and \
             "_THRESHOLD_RESOURCE_TYPE_ALIASES" not in s_content
    if s_done:
        plan.append(("settings.py", None, "already fully fixed -- skipping"))
    else:
        new_s = s_content
        new_s, _ = try_replace(new_s, SETTINGS_IMPORT_OLD, SETTINGS_IMPORT_NEW, "settings.py import")
        new_s, found_map = try_replace(new_s, SETTINGS_LOCAL_MAP_OLD, SETTINGS_LOCAL_MAP_NEW, "settings.py local map")
        if not found_map:
            die("settings.py: local resource_type map not found -- file may differ from what this script expects.")

        new_s, found_a = try_replace(new_s, SETTINGS_HELPER_STATE_A_OLD, SETTINGS_HELPER_NEW, "settings.py helper (state A)")
        if not found_a:
            new_s, found_b = try_replace(new_s, SETTINGS_HELPER_STATE_B_OLD, SETTINGS_HELPER_NEW, "settings.py helper (state B)")
            if not found_b:
                die("settings.py: has_data helper matched neither expected state -- file may differ from what this script expects.")

        # Only State A needs the comparison-line fix; State B already has it.
        new_s, _ = try_replace(new_s, SETTINGS_COMPARISON_STATE_A_OLD, SETTINGS_COMPARISON_NEW, "settings.py comparison")

        plan.append(("settings.py", new_s, "OK"))

    print("\nFile patch plan:")
    for name, content, note in plan:
        print(f"  {name}: {note}")

    if all(content is None for _, content, _ in plan):
        print("\nNothing to do -- everything already applied.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    for name, content, path in [
        ("threshold_defaults.py", plan[0][1], td_path),
        ("metric_catalog.py", plan[1][1], mc_path),
        ("settings.py", plan[2][1], settings_path),
    ]:
        if content is None:
            continue
        backup(path)
        with open(path, "w") as f:
            f.write(content)
        print(f"Patched {name}")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 40 --no-pager

  B) THE REAL TEST for the newly-closed gap: click "Apply Default
     Template" on an account with an ALB, or toggle an ALB metric off
     and back on in Settings -> Metrics to Monitor, then check:
       mysql -u monitor -p monitoring_hub -e \\
         "SELECT resource_type, COUNT(*) FROM thresholds GROUP BY resource_type;"
     Should show NO 'alb'/'nlb' rows even after triggering these
     actions -- previously these specific actions would have
     reintroduced them.

  C) Confirm Settings -> Metric Thresholds still correctly hides/shows
     no-data metrics exactly as before (this should be unaffected --
     same behavior, just now backed by the shared, case-insensitive,
     correctly-normalized logic).

  D) Review, commit, push -- this captures BOTH the resource-type gap
     fix AND (if not already committed) the earlier has_data
     case-sensitivity fix in one clean commit:
       git status
       git diff app/threshold_defaults.py app/api/metric_catalog.py app/api/settings.py
       git add app/threshold_defaults.py app/api/metric_catalog.py app/api/settings.py apply_fix_threshold_resource_type_everywhere.py
       git commit -m "fix(alerts): ALB/NLB resource_type normalization only lived in settings.py, missing metric_catalog.py's own separate INSERT INTO thresholds (onboarding, Apply Default Template, Settings->Metrics changes all funneled through the unfixed path). Moved to the shared app/threshold_defaults.py module so this can't drift apart again. Also captures the earlier has_data case-sensitivity fix, which was applied live but never committed."
       git push origin main
""")


if __name__ == "__main__":
    main()
