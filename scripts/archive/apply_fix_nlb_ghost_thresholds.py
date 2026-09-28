#!/usr/bin/env python3
"""
apply_fix_nlb_ghost_thresholds.py
========================================
Fixes: NLB threshold rows show up in Settings -> Metric Thresholds
(get_thresholds() in app/api/settings.py) even for accounts with real
ALBs and ZERO real NLBs.

ROOT CAUSE (confirmed, matches issue 2.1 in the Sep 6-10 handover):
ALB and NLB are two separate metric_catalog.service values, but
app/collector/discovery/runner.py stores BOTH under
resources.resource_type='elb' uniformly -- this app doesn't
distinguish load balancer type at discovery time, only by ARN pattern
(loadbalancer/app/ vs loadbalancer/net/).

get_thresholds()'s has_data check (_metrics_with_data_for_account) only
ever compared (resource_type, metric_name). Since ALB and NLB share
resource_type='elb' AND commonly share catalog metric names (e.g.
HealthyHostCount), an account with real ALB data makes NLB's *separate*
metric_catalog row for the same metric name look like it "has data"
too -- with zero real NLB resources.

This is the exact ARN-pattern check already built and shipped for the
Metrics to Monitor page (apply_metrics_to_monitor_cleanup.py's
present_core_services logic in app/api/metric_catalog.py) -- reused
here rather than reinvented, applied at the per-threshold-row level and
keyed by the original metric_catalog.service (alb/nlb), not the
normalized resource_type.

FIX:
  1. _metrics_with_data_for_account() now also returns each matching
     row's resources.resource_id (the ARN string), not just
     (resource_type, metric_name).
  2. get_thresholds() checks has_data differently depending on
     mc.service:
       - for "alb"/"nlb" rows: requires a data row whose resource ARN
         actually matches that service's ARN pattern
         (loadbalancer/app/ for alb, loadbalancer/net/ for nlb) --
         not just any elb resource with a matching metric name.
       - for every other service: unchanged, existing
         (resource_type, metric_name) behavior.

SAFETY: this only ever REMOVES ghost has_data=True results for
alb/nlb rows that shouldn't have been marked as having data. It
cannot cause a threshold row to newly appear that wasn't already
eligible to appear -- ALB rows with real ALB data are unaffected
(their ARN pattern still matches), and every other service's logic is
untouched. include_no_data=true still bypasses this filter entirely,
same as before.

TESTED: verified with a synthetic scenario -- 2 ALB resources with an
ARN matching loadbalancer/app/ and recent HealthyHostCount data, 0 NLB
resources -- confirms the ALB threshold row keeps has_data=True and
the NLB threshold row for the same metric name correctly flips to
has_data=False. See _selftest() below, run automatically before any
patch is applied.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_nlb_ghost_thresholds.py --dry-run
    python3 apply_fix_nlb_ghost_thresholds.py --apply
    sudo systemctl restart monitoring-hub
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

OLD = '''def _metrics_with_data_for_account(account_id: int) -> set:
    """
    {(resource_type, metric_name_lower), ...} -- every (resource_type,
    metric_name) combination that has at least one RECENT row in the
    `metrics` last-value cache for a resource belonging to this account.
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

    RECENT, not just present: `metrics` is a last-value cache with NO
    equivalent of metric_history's prune_metric_history() -- a row
    written once, ever, sits there forever even after whatever collected
    it stops running entirely. Confirmed live: EBS BurstBalance (dropped
    from collection entirely by Phase 1, see
    apply_dashboard_charts_metric_history.py) still had a row from ~20
    hours before this fix, permanently making has_data report a false
    positive with no way for it to ever self-correct. _STALE_DATA_CUTOFF
    below is deliberately generous (well beyond the slowest normal
    collection tier, 15 minutes) so a brief scheduler restart or hiccup
    never falsely hides a metric that's still genuinely being collected
    -- it's tuned to catch abandoned metrics measured in hours/days, not
    to be a tight liveness check.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT r.resource_type, m.metric_name
        FROM metrics m
        JOIN resources r ON r.id = m.resource_id
        WHERE r.aws_account_id = %s
          AND m.metric_timestamp >= DATE_SUB(NOW(), INTERVAL %s MINUTE)
    """, (account_id, _STALE_DATA_CUTOFF_MINUTES))
    pairs = {(resource_type, metric_name.lower()) for resource_type, metric_name in cur.fetchall()}
    cur.close(); conn.close()
    return pairs'''

NEW = '''# ALB/NLB share resources.resource_type='elb' (see
# app/collector/discovery/runner.py -- this app doesn't distinguish load
# balancer type at discovery time) and commonly share metric_catalog
# metric names (e.g. HealthyHostCount). The only way to tell them apart
# is the target-group/load-balancer ARN pattern. Same constants
# app/api/metric_catalog.py's present_core_services logic already uses
# for the Metrics to Monitor page -- reused here, not reinvented. See
# apply_fix_nlb_ghost_thresholds.py.
_ALB_ARN_PATTERN = "loadbalancer/app/"
_NLB_ARN_PATTERN = "loadbalancer/net/"


def _metrics_with_data_for_account(account_id: int) -> set:
    """
    {(resource_type, metric_name_lower, resource_id), ...} -- every
    (resource_type, metric_name, resource ARN) combination that has at
    least one RECENT row in the `metrics` last-value cache for a
    resource belonging to this account. metric_name is lowercased here
    because metric_catalog.metric_name stores CloudWatch-style names
    ("CPUUtilization") while app/collector/metrics/runner.py's
    write_metric() writes its own lowercase db_metric_name convention
    ("cpuutilization") into `metrics` -- comparing them as plain Python
    strings without normalizing case would incorrectly treat every AWS
    metric as having no data, since the two sides never match by
    construction. (SQL comparisons elsewhere in this app, e.g.
    alert_evaluator.py's JOIN, happen to work despite this because
    MySQL's default collation is case-insensitive; this is a plain
    Python set membership check, which is not.) Callers must also
    .lower() the metric_name they're checking against this set.

    resource_id (the resource's ARN, not its DB primary key) is
    included so callers can further disambiguate cases where
    resource_type alone is too coarse -- e.g. ALB vs NLB, which both
    normalize to 'elb'. See _has_data_for_threshold_row() below.

    RECENT, not just present: `metrics` is a last-value cache with NO
    equivalent of metric_history's prune_metric_history() -- a row
    written once, ever, sits there forever even after whatever collected
    it stops running entirely. Confirmed live: EBS BurstBalance (dropped
    from collection entirely by Phase 1, see
    apply_dashboard_charts_metric_history.py) still had a row from ~20
    hours before this fix, permanently making has_data report a false
    positive with no way for it to ever self-correct. _STALE_DATA_CUTOFF
    below is deliberately generous (well beyond the slowest normal
    collection tier, 15 minutes) so a brief scheduler restart or hiccup
    never falsely hides a metric that's still genuinely being collected
    -- it's tuned to catch abandoned metrics measured in hours/days, not
    to be a tight liveness check.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT r.resource_type, m.metric_name, r.resource_id
        FROM metrics m
        JOIN resources r ON r.id = m.resource_id
        WHERE r.aws_account_id = %s
          AND m.metric_timestamp >= DATE_SUB(NOW(), INTERVAL %s MINUTE)
    """, (account_id, _STALE_DATA_CUTOFF_MINUTES))
    triples = {
        (resource_type, metric_name.lower(), resource_id or "")
        for resource_type, metric_name, resource_id in cur.fetchall()
    }
    cur.close(); conn.close()
    return triples


def _has_data_for_threshold_row(service, resource_type, db_metric_name, has_data_triples):
    """
    True if this threshold row's (service, resource_type, metric_name)
    genuinely has recent data for THIS row's own service -- not a
    same-resource_type, same-metric-name sibling service's data.

    For alb/nlb specifically, resource_type alone ('elb' for both) and
    metric_name alone (frequently shared, e.g. HealthyHostCount) are
    both too coarse: an account with real ALBs and zero NLBs would
    otherwise make the NLB row look populated purely because an ALB's
    data happens to match on both fields. Disambiguated with the same
    ARN pattern check apply_metrics_to_monitor_cleanup.py already uses
    for Metrics to Monitor (loadbalancer/app/ vs loadbalancer/net/).

    Every other service keeps the original, simpler
    (resource_type, metric_name) membership check -- unaffected.
    """
    if service in ("alb", "nlb"):
        pattern = _ALB_ARN_PATTERN if service == "alb" else _NLB_ARN_PATTERN
        return any(
            rt == resource_type and mn == db_metric_name and pattern in rid
            for rt, mn, rid in has_data_triples
        )
    return any(
        rt == resource_type and mn == db_metric_name
        for rt, mn, _rid in has_data_triples
    )'''

GET_THRESHOLDS_OLD = '''    has_data_pairs = _metrics_with_data_for_account(account_id)
    no_data_count = 0
    out = []
    for r in rows:
        has_data = (r["resource_type"], resolve_db_metric_name(r["resource_type"], r["metric_name"])) in has_data_pairs
        r["has_data"] = has_data'''

GET_THRESHOLDS_NEW = '''    has_data_triples = _metrics_with_data_for_account(account_id)
    no_data_count = 0
    out = []
    for r in rows:
        db_metric_name = resolve_db_metric_name(r["resource_type"], r["metric_name"])
        has_data = _has_data_for_threshold_row(r["service"], r["resource_type"], db_metric_name, has_data_triples)
        r["has_data"] = has_data'''

DONE_MARKER = "_has_data_for_threshold_row"


def _selftest():
    """
    Pure-Python check of the new matching logic against a synthetic
    scenario mirroring the real bug, run before ever touching a file.
    2 ALB resources (ARN matches loadbalancer/app/) with recent
    HealthyHostCount data, 0 NLB resources. Confirms: ALB threshold row
    -> has_data True (unaffected), NLB threshold row for the SAME
    metric name -> has_data False (the fix).
    """
    ns = {}
    exec(compile(NEW, "<selftest>", "exec"), ns)
    has_data_fn = ns["_has_data_for_threshold_row"]

    has_data_triples = {
        ("elb", "healthyhostcount", "arn:aws:elasticloadbalancing:ap-south-1:111:loadbalancer/app/my-alb/abc"),
        ("elb", "healthyhostcount", "arn:aws:elasticloadbalancing:ap-south-1:111:loadbalancer/app/my-alb-2/def"),
    }

    alb_has_data = has_data_fn("alb", "elb", "healthyhostcount", has_data_triples)
    nlb_has_data = has_data_fn("nlb", "elb", "healthyhostcount", has_data_triples)
    other_has_data = has_data_fn("ec2", "ec2", "cpuutilization",
                                  {("ec2", "cpuutilization", "arn:aws:ec2:...:instance/i-abc")})
    other_no_data = has_data_fn("ec2", "ec2", "cpuutilization", set())

    if not alb_has_data:
        die("Self-test failed: ALB row with real ALB data should have has_data=True.")
    if nlb_has_data:
        die("Self-test failed: NLB row with zero real NLB data should have has_data=False.")
    if not other_has_data:
        die("Self-test failed: non-alb/nlb service with matching data should have has_data=True.")
    if other_no_data:
        die("Self-test failed: non-alb/nlb service with no data should have has_data=False.")
    print("[selftest] OK -- ALB keeps has_data=True, NLB correctly flips to has_data=False, other services unaffected.")


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


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="(default; kept for backward compatibility)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    _selftest()

    repo_root = find_repo_root()
    path = os.path.join(repo_root, "app", "api", "settings.py")
    print(f"\nRepo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    if not os.path.exists(path):
        die(f"app/api/settings.py not found at {path}.")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    if DONE_MARKER in content:
        print(f"\napp/api/settings.py already patched -- skipping. Nothing to do.")
        return

    if OLD not in content:
        die("app/api/settings.py: _metrics_with_data_for_account() doesn't match what this script "
            "expects. File may have changed since this script was written.")
    if GET_THRESHOLDS_OLD not in content:
        die("app/api/settings.py: get_thresholds()'s has_data block doesn't match what this script "
            "expects. File may have changed since this script was written.")

    new_content = content.replace(OLD, NEW, 1)
    new_content = new_content.replace(GET_THRESHOLDS_OLD, GET_THRESHOLDS_NEW, 1)

    print(f"\nFile patch plan:\n  app/api/settings.py: OK ({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("Patched app/api/settings.py")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub

  B) Open Settings -> Metric Thresholds for an account with real ALBs
     and zero NLBs -- NLB threshold rows sharing a metric name with ALB
     (e.g. HealthyHostCount) should no longer appear; ALB rows should
     be unaffected.

  C) Review, commit, push:
       git diff app/api/settings.py
       git add app/api/settings.py apply_fix_nlb_ghost_thresholds.py
       git commit -m "fix(alerts): NLB threshold rows falsely showed has_data=True from ALB's data on the shared elb resource_type/metric_name; disambiguate via ARN pattern like Metrics to Monitor already does"
       git push origin main
""")


if __name__ == "__main__":
    main()
