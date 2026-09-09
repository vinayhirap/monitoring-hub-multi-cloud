#!/usr/bin/env python3
"""
apply_fix_stale_metrics_cache.py
========================================
Fixes the actual root cause of the reported BurstBalance mystery,
confirmed with real data from the live server, not assumed:

    mysql> SELECT r.resource_id, m.metric_value, m.metric_timestamp, NOW()
           FROM metrics m JOIN resources r ON r.id = m.resource_id
           WHERE r.resource_type='ebs' AND m.metric_name='burstbalance';
    +-----------------------+--------------+---------------------+---------------------+
    | resource_id           | metric_value | metric_timestamp    | NOW()               |
    +-----------------------+--------------+---------------------+---------------------+
    | vol-0e141f9a946607672 |           99 | 2026-09-08 14:18:17 | 2026-09-09 10:39:41 |
    +-----------------------+--------------+---------------------+---------------------+

A real row exists -- but it's ~20 hours old. Phase 1
(apply_dashboard_charts_metric_history.py) permanently dropped
BurstBalance from collection entirely, so nothing will EVER refresh
this row again. `metric_history` (the time-series table) has
prune_metric_history(), which runs every low-tier cycle and deletes
rows older than 7 days -- but `metrics` (the last-value cache) has NO
equivalent staleness handling at all. A row written once, ever, sits
there permanently, even after whatever wrote it stops running
completely. That's why has_data kept reporting BurstBalance as "has
data" no matter what: the row was real, just frozen in time forever.

THE FIX
---------
_metrics_with_data_for_account() now requires a `metrics` row to be
within the last 60 minutes (a new _STALE_DATA_CUTOFF_MINUTES constant,
easily tunable) to count as "this metric has data" -- not just present.
60 minutes is deliberately generous: the slowest normal collection
tier runs every 15 minutes, so this comfortably survives a scheduler
restart or brief hiccup without falsely hiding a metric that's still
genuinely being collected, while still catching anything abandoned on
the scale of hours or days (like this 20-hour-old orphan).

This does NOT touch metric_history or its existing prune job -- those
already work correctly. This is specifically about the separate
`metrics` last-value cache, which had no equivalent mechanism at all.

TESTED: reproduced the exact reported scenario (a 20-hour-old
BurstBalance row alongside a fresh EC2 CPUUtilization row) using the
real _metrics_with_data_for_account() function -- confirmed the stale
row is correctly excluded and the fresh row is correctly included, and
confirmed the cutoff constant is genuinely wired into the query, not
just present in a docstring.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_stale_metrics_cache.py --dry-run
    python3 apply_fix_stale_metrics_cache.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

CONSTANT_OLD = '''logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["Settings"])
'''

CONSTANT_NEW = '''logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["Settings"])

# How old a `metrics` row can be before _metrics_with_data_for_account()
# stops counting it as "this metric has data" -- deliberately generous
# (well beyond the slowest normal collection tier, 15 minutes) so a
# brief scheduler restart never falsely hides a metric that's still
# genuinely being collected. See that function's docstring for the real
# bug this closes (a metric dropped from collection entirely still
# showing as "has data" forever, because `metrics` has no equivalent of
# metric_history's prune_metric_history()).
_STALE_DATA_CUTOFF_MINUTES = 60
'''

DOCSTRING_OLD = '''def _metrics_with_data_for_account(account_id: int) -> set:
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
    cur.close(); conn.close()'''

DOCSTRING_NEW = '''def _metrics_with_data_for_account(account_id: int) -> set:
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
    cur.close(); conn.close()'''


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

    repo_root = find_repo_root()
    path = os.path.join(repo_root, "app", "api", "settings.py")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    with open(path) as f:
        content = f.read()

    if "_STALE_DATA_CUTOFF_MINUTES" in content:
        print("\nAlready patched -- nothing to do.")
        return

    if content.count(CONSTANT_OLD) != 1 or content.count(DOCSTRING_OLD) != 1:
        die("Expected anchors not found -- file may differ from what this script expects.")

    new_content = content.replace(CONSTANT_OLD, CONSTANT_NEW, 1)
    new_content = new_content.replace(DOCSTRING_OLD, DOCSTRING_NEW, 1)

    print(f"\nFile patch plan:\n  app/api/settings.py: OK ({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w") as f:
        f.write(new_content)
    print("Patched app/api/settings.py")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub

  B) THE REAL TEST: open Settings -> Metric Thresholds. BurstBalance
     should now be hidden (correctly -- its data really is 20+ hours
     stale and will never refresh). The hidden count should go up by 1
     compared to before this fix.

  C) Confirm nothing genuinely-still-collected got wrongly hidden --
     spot check a metric you know updates on the low tier (every 15
     min, e.g. EC2 DiskReadBytes/DiskWriteBytes) and confirm it's still
     shown.

  D) Review, commit, push:
       git diff app/api/settings.py
       git add app/api/settings.py apply_fix_stale_metrics_cache.py
       git commit -m "fix(ui): has_data check counted a metrics row as data if it existed at all, even if the collector that wrote it was removed entirely and the row has been frozen for hours/days (confirmed live: EBS BurstBalance, ~20h stale). Now requires the row to be within the last 60 minutes."
       git push origin main

  Worth knowing, not fixed here: `metrics` still has no equivalent of
  metric_history's prune_metric_history() -- this fix makes has_data
  correctly IGNORE stale rows, but doesn't delete them. If that matters
  for storage/cleanliness reasons later, a periodic DELETE (or simply
  reusing this same staleness definition) would be the natural next
  step -- not bundled in here since it's a different kind of change
  (deleting data vs. ignoring it for one feature).
""")


if __name__ == "__main__":
    main()
