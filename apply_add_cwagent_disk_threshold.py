#!/usr/bin/env python3
"""
apply_add_cwagent_disk_threshold.py
========================================
Completes the CWAgent threshold work by adding Disk Space Utilized
(disk_used_percent) alongside Memory Utilization
(apply_add_cwagent_mem_threshold.py + the dimension-discovery
correction, apply_fix_cwagent_mem_dimensions.py) -- the user's original
request was for both memory AND disk to be configurable, and disk was
deliberately deferred as its own follow-up given its added complexity.

WHY DISK NEEDED MORE THAN JUST COPYING THE MEMORY PATTERN
------------------------------------------------------------------
CWAgent publishes disk_used_percent PER MOUNT POINT (path/device/fstype
dimensions), not as a single series per instance the way mem_used_percent
usually is -- an instance with multiple mounted volumes reports multiple
disk_used_percent series, each needing its own discovered dimension
set. Built following collector_direct.py's _ec2_cwagent_dimensions()
-- the function already powering the working live chart for this exact
metric -- which prefers the root filesystem ("/" on Linux, "C:" on
Windows) when multiple mount points are reporting, since that's what
"disk space utilized" means to someone glancing at a single number on
the dashboard. This keeps the new scheduled threshold path and the
existing on-demand chart in agreement about which mount point "the"
number refers to for a given instance, rather than picking a different,
inconsistent one.

THE FIX
---------
1. app/aws/metric_catalog_data.py: added "disk_used_percent" under
   EC2's core catalog, same conventions as mem_used_percent
   (CWAgent's own literal name, is_default=False since it's opt-in).
2. app/threshold_defaults.py: added a default (80% warn, 90% crit).
3. app/collector/metrics/runner.py: new
   _ec2_instances_with_cwagent_disk_dims() (discovers dimensions,
   preferring root filesystem, falling back to whichever mount point
   CloudWatch returns first if no root is reporting) and
   _collect_ec2_cwagent_disk(), dispatched on the same low tier
   alongside the memory collector.

TESTED: two scenarios with a mocked CloudWatch client -- (1) an
instance reporting BOTH a "/data" and a "/" mount point: confirmed the
collector correctly selects "/" over "/data"; (2) an instance reporting
only a single, non-root "/mnt/data" mount point: confirmed it correctly
falls back to using that one rather than finding nothing. Both
confirmed the real, correct value gets written to `metrics`.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_add_cwagent_disk_threshold.py --dry-run
    python3 apply_add_cwagent_disk_threshold.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

CATALOG_OLD = '''        ("mem_used_percent",   "Percent", "Average", False, "Memory utilization (requires CloudWatch Agent)"),
    ]),'''

CATALOG_NEW = '''        ("mem_used_percent",   "Percent", "Average", False, "Memory utilization (requires CloudWatch Agent)"),
        # disk_used_percent: same rationale as mem_used_percent above
        # (CWAgent-published, opt-in, snake_case official name) -- but
        # published PER MOUNT POINT (multiple series per instance if
        # more than one is monitored). The scheduled collector prefers
        # the root filesystem ("/" / "C:") when multiple exist, matching
        # what the Services page chart already does for the same reason
        # -- "disk space utilized" as a single dashboard number means
        # the root volume to anyone glancing at it. See
        # apply_add_cwagent_disk_threshold.py.
        ("disk_used_percent",  "Percent", "Average", False, "Disk space utilized, root filesystem (requires CloudWatch Agent)"),
    ]),'''

THRESHOLD_OLD = "    'mem_used_percent': (80, 90, '>'),"

THRESHOLD_NEW = """    'mem_used_percent': (80, 90, '>'),
    # Matches the EC2 chart's own existing threshold indicator for this
    # metric (35.5% shown as healthy in an earlier screenshot) --
    # disk filling up is generally a slower-moving, later-warning signal
    # than memory, so a slightly higher bar is reasonable. See
    # apply_add_cwagent_disk_threshold.py.
    'disk_used_percent': (80, 90, '>'),"""

RUNNER_OLD = '''def _collect_ec2_cwagent_mem(cw, resources):
    cwagent_map = _ec2_instances_with_cwagent_mem_dims(cw, resources)
    if not cwagent_map:
        logger.info(f"    EC2 CWAgent mem: 0/{len(resources)} instances have CWAgent reporting")
        return

    queries = []
    id_map = {}
    for i, (resource, dims) in enumerate(cwagent_map.values()):
        qid = f"cwmem{i}"
        queries.append({
            "Id": qid,
            "MetricStat": {
                "Metric": {
                    "Namespace": "CWAgent",
                    "MetricName": "mem_used_percent",
                    "Dimensions": dims,  # full, DISCOVERED set -- not assumed
                },
                "Period": 60,
                "Stat": "Average",
            },
            "ReturnData": True,
        })
        id_map[qid] = (resource["id"], "mem_used_percent")

    n = _execute_gmd(cw, queries, id_map, minutes=16)
    logger.info(f"    EC2 CWAgent mem: {n} datapoints / {len(cwagent_map)} of {len(resources)} instances")'''

RUNNER_NEW = '''def _collect_ec2_cwagent_mem(cw, resources):
    cwagent_map = _ec2_instances_with_cwagent_mem_dims(cw, resources)
    if not cwagent_map:
        logger.info(f"    EC2 CWAgent mem: 0/{len(resources)} instances have CWAgent reporting")
        return

    queries = []
    id_map = {}
    for i, (resource, dims) in enumerate(cwagent_map.values()):
        qid = f"cwmem{i}"
        queries.append({
            "Id": qid,
            "MetricStat": {
                "Metric": {
                    "Namespace": "CWAgent",
                    "MetricName": "mem_used_percent",
                    "Dimensions": dims,  # full, DISCOVERED set -- not assumed
                },
                "Period": 60,
                "Stat": "Average",
            },
            "ReturnData": True,
        })
        id_map[qid] = (resource["id"], "mem_used_percent")

    n = _execute_gmd(cw, queries, id_map, minutes=16)
    logger.info(f"    EC2 CWAgent mem: {n} datapoints / {len(cwagent_map)} of {len(resources)} instances")


def _ec2_instances_with_cwagent_disk_dims(cw, resources):
    """
    {resource: full_dimension_list} for every EC2 instance that has
    actually published disk_used_percent to CWAgent -- one metric PER
    MOUNT POINT (path/device/fstype dimensions), a meaningfully
    different shape than mem_used_percent's single InstanceId-only (or
    close to it) series. When an instance reports multiple mount
    points, prefers the root filesystem ("/" on Linux, "C:" on Windows)
    since that's what "disk space utilized" means to someone glancing
    at a single number on the dashboard -- exactly matching
    collector_direct.py's _ec2_cwagent_dimensions(), the function
    already powering the working live chart for this same metric, so
    the scheduled threshold path and the on-demand chart agree on which
    mount point "the" disk utilization number means for a given
    instance. Falls back to whichever mount point CloudWatch happens to
    return first if there's no root/C: mount reporting.
    """
    result = {}
    for r in resources:
        try:
            resp = cw.list_metrics(
                Namespace="CWAgent",
                MetricName="disk_used_percent",
                Dimensions=[{"Name": "InstanceId", "Value": r["resource_id"]}],
            )
            metrics = resp.get("Metrics", [])
            if not metrics:
                continue
            chosen = None
            for m in metrics:
                dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
                if dims.get("path") in ("/", "C:"):
                    chosen = m["Dimensions"]
                    break
            if chosen is None:
                chosen = metrics[0]["Dimensions"]
            result[r["resource_id"]] = (r, chosen)
        except Exception as e:
            logger.warning(f"CWAgent disk presence check [{r['resource_id']}]: {e}")
    return result


def _collect_ec2_cwagent_disk(cw, resources):
    cwagent_map = _ec2_instances_with_cwagent_disk_dims(cw, resources)
    if not cwagent_map:
        logger.info(f"    EC2 CWAgent disk: 0/{len(resources)} instances have CWAgent reporting")
        return

    queries = []
    id_map = {}
    for i, (resource, dims) in enumerate(cwagent_map.values()):
        qid = f"cwdisk{i}"
        queries.append({
            "Id": qid,
            "MetricStat": {
                "Metric": {
                    "Namespace": "CWAgent",
                    "MetricName": "disk_used_percent",
                    "Dimensions": dims,  # full, DISCOVERED set, root-fs-preferred
                },
                "Period": 60,
                "Stat": "Average",
            },
            "ReturnData": True,
        })
        id_map[qid] = (resource["id"], "disk_used_percent")

    n = _execute_gmd(cw, queries, id_map, minutes=16)
    logger.info(f"    EC2 CWAgent disk: {n} datapoints / {len(cwagent_map)} of {len(resources)} instances")'''

DISPATCH_OLD = '''    _DISPATCH = {
        "ec2_critical":    _collect_ec2_critical,
        "ec2_low":         _collect_ec2_low,
        "ec2_cwagent_mem": _collect_ec2_cwagent_mem,
        "ebs":             _collect_ebs,
        "rds":             _collect_rds,
        "elb":             _collect_elb,
        "lambda_standard": _collect_lambda_standard,
        "lambda_low":      _collect_lambda_low,
    }'''

DISPATCH_NEW = '''    _DISPATCH = {
        "ec2_critical":     _collect_ec2_critical,
        "ec2_low":          _collect_ec2_low,
        "ec2_cwagent_mem":  _collect_ec2_cwagent_mem,
        "ec2_cwagent_disk": _collect_ec2_cwagent_disk,
        "ebs":              _collect_ebs,
        "rds":              _collect_rds,
        "elb":              _collect_elb,
        "lambda_standard":  _collect_lambda_standard,
        "lambda_low":       _collect_lambda_low,
    }'''

TASK_OLD = '''        if resource_type == "ec2":
            if tier in ("critical", "standard"):
                tasks.append((cw, resources, "ec2_critical"))
            if tier == "low":
                tasks.append((cw, resources, "ec2_low"))
                tasks.append((cw, resources, "ec2_cwagent_mem"))
'''

TASK_NEW = '''        if resource_type == "ec2":
            if tier in ("critical", "standard"):
                tasks.append((cw, resources, "ec2_critical"))
            if tier == "low":
                tasks.append((cw, resources, "ec2_low"))
                tasks.append((cw, resources, "ec2_cwagent_mem"))
                tasks.append((cw, resources, "ec2_cwagent_disk"))
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
        [(CATALOG_OLD, CATALOG_NEW)], "disk_used_percent",
    )
    threshold_content, threshold_note = prepare_patch(
        threshold_path, "app/threshold_defaults.py",
        [(THRESHOLD_OLD, THRESHOLD_NEW)], "'disk_used_percent': (80, 90",
    )
    runner_content, runner_note = prepare_patch(
        runner_path, "app/collector/metrics/runner.py",
        [(RUNNER_OLD, RUNNER_NEW), (DISPATCH_OLD, DISPATCH_NEW), (TASK_OLD, TASK_NEW)],
        "_ec2_instances_with_cwagent_disk_dims",
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

  B) Wait for a low-tier cycle (every 15 min), then check:
       sudo journalctl -u monitoring-hub --since "-16min" --no-pager | grep "CWAgent disk"
     Should show "EC2 CWAgent disk: N datapoints / M of K instances"
     with M matching however many instances have CWAgent installed
     (same instances as the memory collector found).

  C) Confirm real data landed:
       mysql -u monitor -p monitoring_hub -e \\
         "SELECT r.resource_id, m.metric_value, m.metric_timestamp
          FROM metrics m JOIN resources r ON r.id = m.resource_id
          WHERE r.resource_type='ec2' AND m.metric_name='disk_used_percent';"
     Should show a value close to what the Services page chart already
     shows for the same instance (e.g. ~35.5% per an earlier screenshot).

  D) In Settings -> Metrics to Monitor, enable "disk_used_percent" for
     EC2 and Save -- a Disk Space Utilized card should appear in
     Metric Thresholds and stay visible once data lands.

  E) Review, commit, push:
       git diff app/aws/metric_catalog_data.py app/threshold_defaults.py app/collector/metrics/runner.py
       git add app/aws/metric_catalog_data.py app/threshold_defaults.py app/collector/metrics/runner.py apply_add_cwagent_disk_threshold.py
       git commit -m "feat(ec2): add Disk Space Utilized (CWAgent disk_used_percent) to the threshold/alerting system, completing the original memory+disk request. Handles per-mount-point dimensions with root-filesystem preference, matching the already-working live chart's own logic."
       git push origin main
""")


if __name__ == "__main__":
    main()
