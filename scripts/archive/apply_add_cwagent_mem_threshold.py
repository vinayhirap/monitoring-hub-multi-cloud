#!/usr/bin/env python3
"""
apply_add_cwagent_mem_threshold.py
========================================
Adds Memory Utilization as a real, configurable, ALERTABLE threshold
for EC2 instances with the CloudWatch Agent installed -- not just a
visual field, since a catalog entry alone would be cosmetic without a
data path for the scheduled evaluator to check.

WHY THIS WASN'T JUST "ADD A CATALOG ROW"
----------------------------------------------
Confirmed by reading app/aws/collector_direct.py's
get_ec2_metric_series(): EC2's Memory/Disk Utilization (CWAgent's
mem_used_percent/disk_used_percent) are 100% LIVE, on-demand boto3
calls made only when someone opens an EC2 detail page -- nothing is
ever written to `metrics` or `metric_history`. That's fine for the
chart (which already correctly shows real data on page load), but it
means there was NO possible way for app/collector/alert_evaluator.py's
scheduled evaluation or check_and_write_alerts() to ever check a
Memory threshold, even if the UI offered one to configure -- there's
no data anywhere for either of them to read.

THE FIX
---------
1. app/aws/metric_catalog_data.py: added a "mem_used_percent" entry
   under EC2's core catalog -- deliberately using CWAgent's own literal
   published metric name (snake_case, not this app's usual PascalCase
   AWS-namespace convention) since that's the actual official name
   CloudWatch Agent publishes under, consistent with this session's
   broader naming-consistency work. is_default=False (opt-in), since
   unlike CPUUtilization/NetworkIn/etc, which apply to every instance,
   this metric doesn't exist at all for instances without the agent.

2. app/threshold_defaults.py: added a default (80% warn, 90% crit,
   greater-than) for 'mem_used_percent', matching the existing pattern
   for CPUUtilization.

3. app/collector/metrics/runner.py: NEW scheduled collection,
   dispatched alongside the existing low-tier EC2 collection (every 15
   min). For each account's EC2 instances, a free ListMetrics call
   checks which ones actually have CWAgent reporting mem_used_percent
   (most won't -- avoids wasting GetMetricData calls on instances
   without the agent), then a normal GetMetricData call collects real
   values for just that filtered subset, written into `metrics` (for
   alert-checking) the same way every other AWS metric already is.
   Uses this account's own assumed-role session throughout (matching
   runner.py's existing multi-account pattern) -- NOT
   collector_direct.py's bare boto3.client() (a separate, pre-existing
   thing, not touched here).

DELIBERATELY NOT INCLUDED: Disk Space Utilized (disk_used_percent).
Unlike mem_used_percent (dimensioned by InstanceId alone), CWAgent
publishes disk_used_percent per mount point (path/device/fstype
dimensions vary per instance and need per-instance discovery, see
collector_direct.py's _ec2_cwagent_dimensions()) -- a meaningfully
larger, differently-shaped piece of work than memory. Flagged as a
known follow-up rather than rushed alongside this, once this same
pattern is proven.

TESTED: exercised the presence-filter and collection logic directly
with a mocked boto3 CloudWatch client simulating 3 instances (2 with
CWAgent reporting, 1 without) -- confirmed the filter correctly
identifies only the 2 real ones, and confirmed the collection call is
scoped to exactly that filtered subset (not wastefully querying all 3),
using the correct CWAgent namespace and metric name.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_add_cwagent_mem_threshold.py --dry-run
    python3 apply_add_cwagent_mem_threshold.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

CATALOG_OLD = '''        ("MetadataNoToken",    "Count",   "Sum",     False, "IMDSv1 requests (no token) — security signal"),
    ]),'''

CATALOG_NEW = '''        ("MetadataNoToken",    "Count",   "Sum",     False, "IMDSv1 requests (no token) — security signal"),
        # CWAgent-published, not standard EC2 namespace -- only present
        # for instances with the CloudWatch Agent actually installed and
        # reporting (see collector_direct.py's _ec2_cwagent_installed).
        # is_default=False deliberately -- unlike CPUUtilization/etc,
        # which apply to every instance, this is opt-in since it doesn't
        # exist at all for instances without the agent. metric_name uses
        # CWAgent's own literal published name (snake_case, not the
        # PascalCase convention of standard EC2 metrics) for consistency
        # with the official platform naming, matching this session's
        # broader naming-consistency work. See apply_add_cwagent_mem_threshold.py.
        ("mem_used_percent",   "Percent", "Average", False, "Memory utilization (requires CloudWatch Agent)"),
    ]),'''

THRESHOLD_OLD = "    'MemoryUtilization': (70, 90, '>'),"

THRESHOLD_NEW = """    'MemoryUtilization': (70, 90, '>'),
    # EC2 CWAgent's own literal metric name (snake_case, distinct from the
    # PascalCase 'MemoryUtilization' key above used by ECS/other services)
    # -- see apply_add_cwagent_mem_threshold.py.
    'mem_used_percent': (80, 90, '>'),"""

RUNNER_COLLECTOR_OLD = '''def _collect_ec2_low(cw, resources):
    n = _run_gmd(cw, resources, EC2_METRICS_LOW, minutes=16)
    logger.info(f"    EC2 low: {n} datapoints / {len(resources)} instances")'''

RUNNER_COLLECTOR_NEW = '''def _collect_ec2_low(cw, resources):
    n = _run_gmd(cw, resources, EC2_METRICS_LOW, minutes=16)
    logger.info(f"    EC2 low: {n} datapoints / {len(resources)} instances")

# CWAgent's mem_used_percent is dimensioned by InstanceId alone (unlike
# disk_used_percent, which also carries path/device/fstype and needs
# per-instance dimension discovery -- deliberately NOT added here, see
# apply_add_cwagent_mem_threshold.py for why disk is a separate,
# larger follow-up rather than bundled in). Because the dimension shape
# matches EC2's own convention, _run_gmd/_build_queries work for it
# unmodified -- only the namespace differs (CWAgent, not AWS/EC2).
CWAGENT_MEM_METRICS = [
    ("mem_used_percent", "mem_used_percent", "Average", "CWAgent"),
]


def _ec2_instances_with_cwagent_mem(cw, resources):
    """
    Filter to only the EC2 instances that have actually published
    mem_used_percent to CWAgent -- a free ListMetrics call per instance,
    unlike GetMetricData. Most instances won't have the CloudWatch Agent
    installed at all, so querying GetMetricData unconditionally for all
    of them would mostly return empty and waste real API cost for
    nothing. Mirrors collector_direct.py's _ec2_cwagent_installed()
    check, but using this account's own assumed-role session (that
    function's bare boto3.client() is a separate, pre-existing thing --
    not touched here) since this runs across every customer account,
    not just wherever this process happens to have default credentials.
    """
    present = []
    for r in resources:
        try:
            resp = cw.list_metrics(
                Namespace="CWAgent",
                MetricName="mem_used_percent",
                Dimensions=[{"Name": "InstanceId", "Value": r["resource_id"]}],
            )
            if resp.get("Metrics"):
                present.append(r)
        except Exception as e:
            logger.warning(f"CWAgent presence check [{r['resource_id']}]: {e}")
    return present


def _collect_ec2_cwagent_mem(cw, resources):
    cwagent_resources = _ec2_instances_with_cwagent_mem(cw, resources)
    if not cwagent_resources:
        logger.info(f"    EC2 CWAgent mem: 0/{len(resources)} instances have CWAgent reporting")
        return
    n = _run_gmd(cw, cwagent_resources, CWAGENT_MEM_METRICS, minutes=16)
    logger.info(f"    EC2 CWAgent mem: {n} datapoints / {len(cwagent_resources)} of {len(resources)} instances")'''

RUNNER_DISPATCH_OLD = '''    _DISPATCH = {
        "ec2_critical":    _collect_ec2_critical,
        "ec2_low":         _collect_ec2_low,
        "ebs":             _collect_ebs,
        "rds":             _collect_rds,
        "elb":             _collect_elb,
        "lambda_standard": _collect_lambda_standard,
        "lambda_low":      _collect_lambda_low,
    }'''

RUNNER_DISPATCH_NEW = '''    _DISPATCH = {
        "ec2_critical":    _collect_ec2_critical,
        "ec2_low":         _collect_ec2_low,
        "ec2_cwagent_mem": _collect_ec2_cwagent_mem,
        "ebs":             _collect_ebs,
        "rds":             _collect_rds,
        "elb":             _collect_elb,
        "lambda_standard": _collect_lambda_standard,
        "lambda_low":      _collect_lambda_low,
    }'''

RUNNER_TASK_OLD = '''        if resource_type == "ec2":
            if tier in ("critical", "standard"):
                tasks.append((cw, resources, "ec2_critical"))
            if tier == "low":
                tasks.append((cw, resources, "ec2_low"))
'''

RUNNER_TASK_NEW = '''        if resource_type == "ec2":
            if tier in ("critical", "standard"):
                tasks.append((cw, resources, "ec2_critical"))
            if tier == "low":
                tasks.append((cw, resources, "ec2_low"))
                tasks.append((cw, resources, "ec2_cwagent_mem"))
'''


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
            die(f"{label}: expected exactly 1 match, found {n}. File may differ from what this script expects.")
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
    catalog_path = os.path.join(repo_root, "app", "aws", "metric_catalog_data.py")
    threshold_path = os.path.join(repo_root, "app", "threshold_defaults.py")
    runner_path = os.path.join(repo_root, "app", "collector", "metrics", "runner.py")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    catalog_content, catalog_note = prepare_patch(
        catalog_path, "app/aws/metric_catalog_data.py",
        [(CATALOG_OLD, CATALOG_NEW)],
        "mem_used_percent",
    )
    threshold_content, threshold_note = prepare_patch(
        threshold_path, "app/threshold_defaults.py",
        [(THRESHOLD_OLD, THRESHOLD_NEW)],
        "'mem_used_percent': (80, 90",
    )
    runner_content, runner_note = prepare_patch(
        runner_path, "app/collector/metrics/runner.py",
        [(RUNNER_COLLECTOR_OLD, RUNNER_COLLECTOR_NEW),
         (RUNNER_DISPATCH_OLD, RUNNER_DISPATCH_NEW),
         (RUNNER_TASK_OLD, RUNNER_TASK_NEW)],
        "_ec2_instances_with_cwagent_mem",
    )

    print(f"\nFile patch plan:\n  {catalog_note}\n  {threshold_note}\n  {runner_note}")

    if catalog_content is None and threshold_content is None and runner_content is None:
        print("\nNothing to do.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    if catalog_content is not None:
        backup(catalog_path)
        with open(catalog_path, "w", encoding="utf-8") as fh:
            fh.write(catalog_content)
        print("Patched app/aws/metric_catalog_data.py")
    if threshold_content is not None:
        backup(threshold_path)
        with open(threshold_path, "w", encoding="utf-8") as fh:
            fh.write(threshold_content)
        print("Patched app/threshold_defaults.py")
    if runner_content is not None:
        backup(runner_path)
        with open(runner_path, "w", encoding="utf-8") as fh:
            fh.write(runner_content)
        print("Patched app/collector/metrics/runner.py")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 40 --no-pager

  B) Wait for a low-tier cycle (every 15 min), then check the logs for:
       "EC2 CWAgent mem: N datapoints / M of K instances"
     M should be > 0 for any account with CWAgent-equipped instances
     (like HCS-PROD-MD-01, confirmed showing real 83.4% memory in an
     earlier screenshot).

  C) Confirm real data landed:
       mysql -u monitor -p monitoring_hub -e \\
         "SELECT r.resource_id, m.metric_value, m.metric_timestamp
          FROM metrics m JOIN resources r ON r.id = m.resource_id
          WHERE r.resource_type='ec2' AND m.metric_name='mem_used_percent';"

  D) In Settings -> Metrics to Monitor, enable "mem_used_percent" for
     EC2 (it's opt-in, is_default=False) and Save -- then check
     Settings -> Metric Thresholds; a Memory Utilization card should
     now appear and be configurable, and (once data lands per step C)
     should NOT be hidden by the no-data filter.

  E) Review, commit, push:
       git diff app/aws/metric_catalog_data.py app/threshold_defaults.py app/collector/metrics/runner.py
       git add app/aws/metric_catalog_data.py app/threshold_defaults.py app/collector/metrics/runner.py apply_add_cwagent_mem_threshold.py
       git commit -m "feat(ec2): wire Memory Utilization (CWAgent mem_used_percent) into the threshold/alerting system -- previously 100% live/on-demand with no data path for the scheduled evaluator to ever check. Disk Space Utilized deliberately deferred as a separate, larger follow-up (per-mount-point dimension discovery needed)."
       git push origin main
""")


if __name__ == "__main__":
    main()
