#!/usr/bin/env python3
"""
apply_fix_stale_cutoff_too_aggressive.py
========================================
URGENT correction for a real regression, confirmed live within hours of
shipping the previous fix (apply_fix_stale_metrics_cache.py): its
60-minute staleness cutoff was far too aggressive. Confirmed by direct
comparison of two screenshots taken minutes apart: ALB's
HTTPCode_Target_5XX_Count -- correctly shown right after the naming-map
fix (apply_fix_has_data_naming_map.py) -- disappeared from Metric
Thresholds after the 60-minute cutoff shipped. The hidden count jumped
from 85 to 91 (+6, not the expected +1 for BurstBalance alone).

ROOT CAUSE
------------
HTTPCode_Target_5XX_Count is a Sum-type, EVENT-DRIVEN CloudWatch
metric -- AWS only publishes a datapoint for it when a 5xx error
actually happens. Zero 5xx errors for an hour is a GOOD sign (a
healthy load balancer), not evidence the collector stopped running.
The 60-minute cutoff couldn't distinguish "genuinely abandoned metric"
(the BurstBalance case the fix was built for -- a metric permanently
dropped from Phase 1's collection, whose row will NEVER refresh again)
from "actively, correctly collected event metric that currently has
nothing to report" (5xx errors, 4xx errors, throttles, and similar
sum-type metrics generally). 60 minutes is far too short a window for
any event/error-count metric on a quiet-but-healthy resource.

THE FIX
---------
Widens _STALE_DATA_CUTOFF_MINUTES from 60 minutes to 7 days (10080
minutes) -- matching app/collector/metrics_writer.py's
prune_metric_history()'s EXISTING 7-day retention window, an already-
established boundary for "how long is data considered relevant" in
this app, rather than picking a new arbitrary number. This is a
deliberate trade-off: BurstBalance-style permanently-abandoned metrics
will take longer to disappear from the UI (up to 7 days, instead of 1
hour) -- accepted, because the alternative (wrongly hiding real,
actively-collected error/event metrics after any quiet hour) is a far
more common and more harmful failure mode for actual monitoring use.

TESTED: reproduced the exact regression (an event metric quiet for 5
hours, alongside the original BurstBalance scenario) using the real
_metrics_with_data_for_account() function. Confirmed three properties
in one test: (1) the 5-hour-quiet event metric is now correctly shown
-- regression fixed; (2) a 20-hour-old abandoned-metric row also now
correctly shows (the accepted short-term trade-off); (3) an 8-day-old
abandoned-metric row is STILL correctly hidden -- confirming the
original BurstBalance-class bug is still eventually caught, just on a
longer timeline.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_stale_cutoff_too_aggressive.py --dry-run
    python3 apply_fix_stale_cutoff_too_aggressive.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

OLD = '''# How old a `metrics` row can be before _metrics_with_data_for_account()
# stops counting it as "this metric has data" -- deliberately generous
# (well beyond the slowest normal collection tier, 15 minutes) so a
# brief scheduler restart never falsely hides a metric that's still
# genuinely being collected. See that function's docstring for the real
# bug this closes (a metric dropped from collection entirely still
# showing as "has data" forever, because `metrics` has no equivalent of
# metric_history's prune_metric_history()).
_STALE_DATA_CUTOFF_MINUTES = 60'''

NEW = '''# How old a `metrics` row can be before _metrics_with_data_for_account()
# stops counting it as "this metric has data" -- deliberately generous.
#
# CORRECTED (apply_fix_stale_cutoff_too_aggressive.py): originally set to
# 60 minutes, which caused a real regression -- confirmed live: ALB's
# HTTPCode_Target_5XX_Count disappeared from Metric Thresholds within
# hours of shipping, despite being correctly, actively collected. Root
# cause: this is a Sum-type, EVENT-DRIVEN CloudWatch metric -- AWS only
# publishes a datapoint for it when a 5xx error actually happens.  Zero
# 5xx errors for an hour is a GOOD sign (a healthy load balancer), not
# evidence the collector stopped, but a 60-minute cutoff couldn't tell
# the difference between "genuinely abandoned metric" (the BurstBalance
# case this was built for) and "actively collected, currently just has
# nothing to report" (this case). 60 minutes is far too short a window
# for any event/error-count metric on a quiet-but-healthy resource.
#
# Widened to match metric_history's OWN existing retention window (7
# days, see prune_metric_history() in app/collector/metrics_writer.py)
# instead of picking a new arbitrary number -- this app already treats
# 7 days as "how long data stays relevant" elsewhere, so reusing it here
# is a principled choice, not a guess. A metric permanently dropped from
# collection (like BurstBalance) will reliably exceed even a 7-day
# window eventually, since nothing will EVER refresh it again -- while
# an event metric would need to go a full week with zero occurrences to
# be wrongly hidden, a much rarer, more defensible edge case than an
# hour.
_STALE_DATA_CUTOFF_MINUTES = 7 * 24 * 60  # 10080 -- 7 days'''


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

    if "7 * 24 * 60" in content:
        print("\nAlready patched -- nothing to do.")
        return

    n = content.count(OLD)
    if n != 1:
        die(f"Expected exactly 1 match, found {n}. File may differ from what this script expects.")

    new_content = content.replace(OLD, NEW, 1)
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

  B) THE REAL TEST: open Settings -> Metric Thresholds. ALB's
     HTTPCode_Target_5XX_Count (and any other event-driven metric that
     disappeared) should be back. The hidden count should drop back
     down close to where it was right after the naming-map fix (around
     85), not 91.

  C) BurstBalance will likely also reappear for now (its ~20h-old row
     is well within the new 7-day window) -- this is the accepted
     trade-off, not a bug. It will correctly disappear again once that
     row passes 7 days old, since nothing will ever refresh it.

  D) Review, commit, push:
       git diff app/api/settings.py
       git add app/api/settings.py apply_fix_stale_cutoff_too_aggressive.py
       git commit -m "fix(urgent): the stale-metrics-cache fix's 60-minute cutoff was far too aggressive, wrongly hiding sum-type event metrics (confirmed live: ALB 5xx errors) that only get a CloudWatch datapoint when the event occurs. Widened to 7 days, matching metric_history's own existing retention window."
       git push origin main
""")


if __name__ == "__main__":
    main()
