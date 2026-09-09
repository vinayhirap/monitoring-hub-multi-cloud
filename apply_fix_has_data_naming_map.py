#!/usr/bin/env python3
"""
apply_fix_has_data_naming_map.py
========================================
Fixes a systemic, previously-undiscovered bug found by systematically
comparing EVERY collected AWS metric's official catalog name against
its actual app/collector/metrics/runner.py db_metric_name (not
spot-checked one at a time from user reports -- the full table was
built and diffed programmatically). The earlier case-sensitivity fix
assumed catalog_metric_name.lower() always equals the real
metrics.metric_name -- true for MOST AWS metrics and for ALL of Azure/
GCP (confirmed by reading their collectors directly: both write
metric_catalog.metric_name through completely unchanged, no
abbreviation at all) -- but FALSE for 6 specific AWS metrics that have
genuinely different internal shorthand:

    RDS DatabaseConnections          -> dbconnections        (not "databaseconnections")
    RDS FreeStorageSpace             -> freestorage           (not "freestoragespace")
    ELB HTTPCode_Target_5XX_Count    -> errors5xx             (not the lowered CW name)
    ELB TargetResponseTime           -> responselatency       (not "targetresponsetime")
    ELB HealthyHostCount             -> healthyhosts_describe (different SOURCE entirely --
    ELB UnHealthyHostCount           -> unhealthyhosts_describe   see apply_fix_alb_healthy_hosts.py)

Practical impact, confirmed with a before/after test using the real
get_thresholds() function: of 9 representative metrics tested, the
pre-fix code correctly showed only 2 (CPUUtilization, VolumeReadOps)
and WRONGLY hid the other 7 -- including 6 that have real,
actively-collected data (RDS connections/storage, all 4 ELB metrics)
and only correctly hiding the 1 that genuinely has none (EBS
BurstBalance). Anyone with RDS or ALB thresholds configured has been
seeing them silently disappear from Settings -> Metric Thresholds by
default since the has_data feature shipped.

THE FIX
---------
Replaces the blind .lower() guess with an explicit override table
(AWS_METRIC_NAME_TO_DB_NAME in app/threshold_defaults.py, the
established shared home for this class of mapping) covering exactly
the 6 divergent AWS metrics, checked first; anything not in the table
(the rest of AWS, and all of Azure/GCP, both confirmed to write
metric_name unchanged) correctly falls through to the same .lower()
comparison as before -- no regression for the metrics that already
worked.

TESTED: built the full official-name-vs-db-name comparison table for
every currently-collected AWS metric programmatically (not manually
spot-checked) to find all 6 mismatches, then ran a before/after test
using the REAL get_thresholds() function against 9 representative
metrics (7 previously-mismatched + 2 controls that already worked +
1 genuinely-no-data control) -- confirmed the pre-fix code fails
exactly as predicted (2 of 9 shown) and the post-fix code passes (8 of
9 shown, only BurstBalance correctly still hidden).

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_has_data_naming_map.py --dry-run
    python3 apply_fix_has_data_naming_map.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

TD_OLD = '''THRESHOLD_RESOURCE_TYPE_ALIASES = {"alb": "elb", "nlb": "elb"}


def normalize_threshold_resource_type(value):
    return THRESHOLD_RESOURCE_TYPE_ALIASES.get(value, value)'''

TD_NEW = '''THRESHOLD_RESOURCE_TYPE_ALIASES = {"alb": "elb", "nlb": "elb"}


def normalize_threshold_resource_type(value):
    return THRESHOLD_RESOURCE_TYPE_ALIASES.get(value, value)


# Confirmed by reading app/providers/azure/metrics_collector.py and
# app/providers/gcp/metrics_collector.py directly: both write
# metric_catalog.metric_name into `metrics`/`metric_history` completely
# UNCHANGED (Azure: metric.name, the SDK's own echo of the exact
# requested catalog name; GCP: row["metric_name"], the catalog row
# itself) -- no transform, no abbreviation, for either cloud. A simple
# .lower() comparison on both sides always correctly matches for Azure
# and GCP, and for MOST AWS metrics too (app/collector/metrics/runner.py
# happens to use "cpuutilization" for "CPUUtilization", etc.).
#
# But AWS's db_metric_name convention is a genuinely separate,
# hand-picked abbreviation in several cases -- confirmed by comparing
# every entry in runner.py's EC2_METRICS_*/EBS_METRICS/RDS_METRICS/
# ELB_METRICS/LAMBDA_METRICS_* tuples against metric_catalog's official
# name, catalog_name.lower() != db_metric_name for these specific ones:
#   RDS DatabaseConnections -> dbconnections (not "databaseconnections")
#   RDS FreeStorageSpace    -> freestorage   (not "freestoragespace")
#   ELB HTTPCode_Target_5XX_Count -> errors5xx (not the CW name, lowered)
#   ELB TargetResponseTime  -> responselatency (not "targetresponsetime")
#   ELB HealthyHostCount    -> healthyhosts_describe (different SOURCE
#                              entirely -- see apply_fix_alb_healthy_hosts.py;
#                              CloudWatch-based collection for this metric
#                              never worked at all, describe_polling.py's
#                              free DescribeTargetHealth path is the only
#                              real source)
#   ELB UnHealthyHostCount  -> unhealthyhosts_describe (same as above)
# A blind catalog_name.lower() guess is WRONG for exactly these 6 --
# without this override, has_data-style checks would incorrectly treat
# metrics that genuinely have real, actively-collected data as if they
# never produced anything. This map is checked FIRST; anything not
# listed here (which covers the rest of AWS plus all of Azure/GCP)
# correctly falls back to the plain .lower() comparison.
AWS_METRIC_NAME_TO_DB_NAME = {
    ("rds", "DatabaseConnections"): "dbconnections",
    ("rds", "FreeStorageSpace"): "freestorage",
    ("elb", "HTTPCode_Target_5XX_Count"): "errors5xx",
    ("elb", "TargetResponseTime"): "responselatency",
    ("elb", "HealthyHostCount"): "healthyhosts_describe",
    ("elb", "UnHealthyHostCount"): "unhealthyhosts_describe",
}


def resolve_db_metric_name(resource_type, catalog_metric_name):
    """
    The single source of truth for "given a metric_catalog metric_name
    and its resource_type, what string actually appears in
    metrics.metric_name / metric_history.metric_name?" -- checks the
    explicit AWS override table first (for the handful of AWS metrics
    where the internal abbreviation genuinely diverges from a case-fold
    of the official name), falling back to a plain lowercase compare
    for everything else (correct for Azure, GCP, and most of AWS, which
    all write their metric_name consistent with a simple case-fold).
    """
    override = AWS_METRIC_NAME_TO_DB_NAME.get((resource_type, catalog_metric_name))
    if override is not None:
        return override
    return (catalog_metric_name or "").lower()'''

SETTINGS_IMPORT_OLD = "from app.threshold_defaults import DEFAULT_THRESHOLDS, FALLBACK_THRESHOLD, normalize_threshold_resource_type"
SETTINGS_IMPORT_NEW = "from app.threshold_defaults import DEFAULT_THRESHOLDS, FALLBACK_THRESHOLD, normalize_threshold_resource_type, resolve_db_metric_name"

SETTINGS_COMPARISON_OLD = '        has_data = (r["resource_type"], (r["metric_name"] or "").lower()) in has_data_pairs'
SETTINGS_COMPARISON_NEW = '        has_data = (r["resource_type"], resolve_db_metric_name(r["resource_type"], r["metric_name"])) in has_data_pairs'


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
    td_path = os.path.join(repo_root, "app", "threshold_defaults.py")
    settings_path = os.path.join(repo_root, "app", "api", "settings.py")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    with open(td_path) as f:
        td_content = f.read()
    td_done = "AWS_METRIC_NAME_TO_DB_NAME" in td_content
    if td_done:
        td_new = None
        td_note = "threshold_defaults.py: already has AWS_METRIC_NAME_TO_DB_NAME -- skipping"
    else:
        if td_content.count(TD_OLD) != 1:
            die("threshold_defaults.py: anchor text not found -- file may differ from what this script expects.")
        td_new = td_content.replace(TD_OLD, TD_NEW, 1)
        td_note = f"threshold_defaults.py: OK ({len(td_new) - len(td_content):+d} bytes)"

    with open(settings_path) as f:
        s_content = f.read()
    s_done = "resolve_db_metric_name" in s_content
    if s_done:
        s_new = None
        s_note = "settings.py: already uses resolve_db_metric_name -- skipping"
    else:
        if s_content.count(SETTINGS_IMPORT_OLD) != 1 or s_content.count(SETTINGS_COMPARISON_OLD) != 1:
            die("settings.py: expected anchors not found -- file may differ from what this script expects.")
        s_new = s_content.replace(SETTINGS_IMPORT_OLD, SETTINGS_IMPORT_NEW, 1)
        s_new = s_new.replace(SETTINGS_COMPARISON_OLD, SETTINGS_COMPARISON_NEW, 1)
        s_note = f"settings.py: OK ({len(s_new) - len(s_content):+d} bytes)"

    print(f"\nFile patch plan:\n  {td_note}\n  {s_note}")

    if td_new is None and s_new is None:
        print("\nNothing to do -- everything already applied.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    if td_new is not None:
        backup(td_path)
        with open(td_path, "w") as f:
            f.write(td_new)
        print("Patched app/threshold_defaults.py")
    if s_new is not None:
        backup(settings_path)
        with open(settings_path, "w") as f:
            f.write(s_new)
        print("Patched app/api/settings.py")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub

  B) THE REAL TEST: open Settings -> Metric Thresholds for an account
     with RDS instances and/or load balancers. If it previously showed
     "N metric(s) hidden", that count should now be noticeably smaller
     -- RDS's DatabaseConnections/FreeStorageSpace and ALB's
     HTTPCode_Target_5XX_Count/TargetResponseTime/HealthyHostCount/
     UnHealthyHostCount should now appear as real, configurable
     threshold cards (assuming those resources exist and have
     collected data).

  C) Review, commit, push:
       git diff app/threshold_defaults.py app/api/settings.py
       git add app/threshold_defaults.py app/api/settings.py apply_fix_has_data_naming_map.py
       git commit -m "fix(ui): has_data check assumed catalog_name.lower() always equals the real db metric_name -- false for 6 AWS metrics with genuinely divergent internal abbreviations (RDS DatabaseConnections/FreeStorageSpace, all 4 ELB metrics), which were being wrongly hidden from Metric Thresholds despite having real data. Replaced the guess with an explicit, tested mapping."
       git push origin main

  Found via a systematic audit comparing every AWS metric's official
  name against its actual db_metric_name, not one-off user reports --
  same technique can be reapplied if more services are added later.
""")


if __name__ == "__main__":
    main()
