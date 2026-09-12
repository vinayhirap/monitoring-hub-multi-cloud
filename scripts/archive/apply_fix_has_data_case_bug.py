#!/usr/bin/env python3
"""
apply_fix_has_data_case_bug.py
========================================
Fixes a real bug in the "hide no-data metrics" feature shipped just
before this: it compared metric_catalog's CloudWatch-style metric names
("CPUUtilization", "RequestCount") against `metrics` table's names as
plain Python strings with no case normalization -- but
app/collector/metrics/runner.py's write_metric() writes its OWN
lowercase convention ("cpuutilization", "requestcount") into `metrics`.
The two sides never match by construction, meaning the has_data check
was wrong for every single AWS metric, not just the ones that
genuinely have no data.

CONFIRMED, NOT GUESSED
-------------------------
Reproduced directly: replaying the exact pre-fix code against a
realistic dataset (EC2 CPU and ALB RequestCount both WITH real data,
EBS BurstBalance genuinely WITHOUT any) hides all three -- including
the two with real data. That's a definite defect, independent of
exactly how it manifested on any particular live server (which likely
also has some residual/inconsistent rows in `metrics` from testing
throughout this session, compounding the confusion, but not the root
cause).

Why this wasn't caught by the alert-evaluation pipeline itself: SQL
comparisons (e.g. alert_evaluator.py's `mc.metric_name = m.metric_name`
JOIN) work fine despite the casing difference, because MySQL's default
collation (utf8mb4_0900_ai_ci) is case-insensitive. This bug is
specific to the NEW Python-side set-membership check this feature
added, which is not.

THE FIX
---------
_metrics_with_data_for_account() now lowercases metric_name when
building its {(resource_type, metric_name)} set, and get_thresholds()
lowercases the metric_name it checks against that set. Both sides now
compare consistently regardless of which casing convention either table
happens to use.

TESTED: reproduced the bug against the pre-fix code (confirmed it hides
metrics WITH real data, not just ones without), then confirmed the
fixed code correctly distinguishes EC2 CPUUtilization and ALB
RequestCount (both with real data) from EBS BurstBalance (genuinely
none) in the same test.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_has_data_case_bug.py --dry-run
    python3 apply_fix_has_data_case_bug.py --apply
(no frontend rebuild needed -- this is a Python-only backend fix)
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

HELPER_OLD = '''def _metrics_with_data_for_account(account_id: int) -> set:
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

HELPER_NEW = '''def _metrics_with_data_for_account(account_id: int) -> set:
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

COMPARISON_OLD = '''        has_data = (r["resource_type"], r["metric_name"]) in has_data_pairs'''

COMPARISON_NEW = '''        has_data = (r["resource_type"], (r["metric_name"] or "").lower()) in has_data_pairs'''


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


def prepare_patch(path, label, replacements, done_marker):
    if not os.path.exists(path):
        die(f"{label} not found at {path}.")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    if done_marker in content:
        return None, f"{label} already patched -- skipping."
    new_content = content
    for old, new in replacements:
        n = new_content.count(old)
        if n != 1:
            die(f"{label}: expected exactly 1 match for one of the expected blocks, found {n}. "
                f"File may differ from what this script expects.")
        new_content = new_content.replace(old, new, 1)
    return new_content, f"{label}: OK ({len(new_content) - len(content):+d} bytes)"


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

    content, note = prepare_patch(
        path, "app/api/settings.py",
        [(HELPER_OLD, HELPER_NEW), (COMPARISON_OLD, COMPARISON_NEW)],
        "metric_name_lower",
    )
    print(f"\nFile patch plan:\n  {note}")

    if content is None:
        print("\nNothing to do.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print("Patched app/api/settings.py")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub

  B) Open Settings -> Metric Thresholds for AuroGov Mumbai. You should
     now see EC2, EBS, RDS (if it has real resources -- confirmed this
     account has 0 RDS instances discovered, so RDS will correctly stay
     hidden), Lambda (only if it's actually been invoked -- Lambda's
     own metrics are invocation-driven, "0 datapoints" in your own
     scheduler logs just means it hasn't run, not a bug), and ALB
     (which DOES have real collected data per your own logs -- this is
     the one that should visibly reappear after this fix).

  C) Confirm the hidden count now makes more sense relative to what you
     know actually has data vs. genuinely doesn't.

  D) On the "30+ alerts at once" from Check Now: this is very likely
     the EXPECTED result of the ALB/NLB threshold fix (earlier this
     session) working correctly for the first time -- if those alerts
     had been silently unable to fire for a long time, the first real
     check surfaces the whole backlog at once. Worth reviewing whether
     the threshold VALUES themselves are realistic for current traffic
     (e.g. an EC2 Net In/Net Out threshold of 1,000,000 while real
     traffic regularly exceeds that isn't a code bug -- it's a
     threshold that may need retuning) before assuming this is another
     defect.

  E) Review, commit, push:
       git diff app/api/settings.py
       git add app/api/settings.py apply_fix_has_data_case_bug.py
       git commit -m "fix(ui): has_data check for hiding no-data metrics compared CloudWatch-style names against runner.py's lowercase db convention with no case normalization -- hid metrics with real data, not just ones without"
       git push origin main
""")


if __name__ == "__main__":
    main()
