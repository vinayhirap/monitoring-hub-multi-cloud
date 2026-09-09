#!/usr/bin/env python3
"""
apply_fix_cwagent_mem_dimensions.py
========================================
Corrects a real bug in apply_add_cwagent_mem_threshold.py, confirmed
live within one collection cycle of shipping: the presence filter
correctly identified 1 of 20 EC2 instances as having CWAgent reporting
mem_used_percent (matching HCS-PROD-MD-01, the exact instance an
earlier screenshot showed with real 83.4% memory usage) -- but the
actual data collection returned "0 datapoints / 1 of 20 instances",
and the metrics table had zero rows for it.

ROOT CAUSE
------------
The original collector assumed mem_used_percent is always dimensioned
by InstanceId alone and built its GetMetricData query with only that
one dimension. This is exactly the mistake this codebase's OWN existing
code already knows to avoid -- confirmed by reading
app/aws/collector_direct.py's _ec2_cwagent_dimensions() (the function
powering the ALREADY-WORKING live chart for this same instance/metric),
whose docstring says plainly: "GetMetricData needs the COMPLETE
dimension set a datapoint was actually published under; a partial
match (InstanceId only) returns nothing." CWAgent's append_dimensions
config can add extra dimensions (ImageId, InstanceType, etc.) depending
on how the agent is configured -- this varies per instance, not
something to assume uniformly, and this specific instance's real data
apparently has more than InstanceId alone.

THE FIX
---------
The collector now discovers each instance's REAL, complete dimension
set via the same ListMetrics call already being made for presence-
checking (no extra API cost -- it's the same underlying data), instead
of assuming InstanceId alone, matching the pattern the already-working
live chart code uses. Builds its own MetricDataQueries directly (since
_run_gmd's generic path assumes one uniform dimension per resource
type, which doesn't fit per-instance-discovered dimension sets) and
calls _execute_gmd directly for the actual API call and write --
_execute_gmd itself didn't need any changes, it was always
dimension-agnostic.

TESTED: reproduced the EXACT live scenario (an instance with real
memory data published under InstanceId + an extra dimension) against
the pre-fix code first -- confirmed it builds an InstanceId-only query
and fails the assertion, proving this is the real bug, not a guess.
Then confirmed the fixed code uses the full discovered dimension set
and correctly writes the real value (83.4) to `metrics`.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_cwagent_mem_dimensions.py --dry-run
    python3 apply_fix_cwagent_mem_dimensions.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

OLD = '''# CWAgent's mem_used_percent is dimensioned by InstanceId alone (unlike
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

NEW = '''# CWAgent's mem_used_percent CAN be dimensioned by InstanceId alone, but
# is NOT guaranteed to be -- append_dimensions in the agent's own config
# can add more (ImageId, InstanceType, etc.), varying per instance. The
# collector below discovers each instance's REAL, complete dimension set
# via ListMetrics rather than assuming a fixed shape -- see
# apply_fix_cwagent_mem_dimensions.py for why an earlier, simpler
# version of this (reusing _run_gmd's uniform single-dimension path)
# returned zero data despite correctly identifying which instances have
# the agent installed.


def _ec2_instances_with_cwagent_mem_dims(cw, resources):
    """
    {resource: full_dimension_list} for every EC2 instance that has
    actually published mem_used_percent to CWAgent.

    CORRECTED: originally assumed mem_used_percent is always dimensioned
    by InstanceId alone and built GetMetricData queries with only that
    one dimension -- confirmed live this returns ZERO data even for an
    instance that genuinely, visibly has real memory data (the same
    HCS-PROD-MD-01 instance the Services page already shows 83.4%
    memory for). Root cause, already documented elsewhere in this exact
    codebase (collector_direct.py's _ec2_cwagent_dimensions(), the
    function powering that already-working chart): "GetMetricData needs
    the COMPLETE dimension set a datapoint was actually published
    under; a partial match (InstanceId only) returns nothing." CWAgent's
    append_dimensions config can add extra dimensions (ImageId,
    InstanceType, etc.) beyond InstanceId depending on how it's
    configured -- this varies per instance/config, not something to
    assume uniformly. Now discovers the REAL, complete dimension set via
    the same ListMetrics call already being made (no extra API cost --
    presence-check and dimension-discovery are the same underlying
    data), instead of assuming InstanceId alone. See
    apply_fix_cwagent_mem_dimensions.py.
    """
    result = {}
    for r in resources:
        try:
            resp = cw.list_metrics(
                Namespace="CWAgent",
                MetricName="mem_used_percent",
                Dimensions=[{"Name": "InstanceId", "Value": r["resource_id"]}],
            )
            metrics = resp.get("Metrics", [])
            if metrics:
                result[r["resource_id"]] = (r, metrics[0]["Dimensions"])
        except Exception as e:
            logger.warning(f"CWAgent presence check [{r['resource_id']}]: {e}")
    return result


def _collect_ec2_cwagent_mem(cw, resources):
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
    path = os.path.join(repo_root, "app", "collector", "metrics", "runner.py")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    with open(path) as f:
        content = f.read()

    if "_ec2_instances_with_cwagent_mem_dims" in content:
        print("\nAlready patched -- nothing to do.")
        return

    n = content.count(OLD)
    if n != 1:
        die(f"Expected exactly 1 match, found {n}. File may differ from what this script expects.")

    new_content = content.replace(OLD, NEW, 1)
    print(f"\nFile patch plan:\n  app/collector/metrics/runner.py: OK ({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w") as f:
        f.write(new_content)
    print("Patched app/collector/metrics/runner.py")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub

  B) Wait for a low-tier cycle (every 15 min), then check:
       sudo journalctl -u monitoring-hub --since "-16min" --no-pager | grep "CWAgent mem"
     Should now show a NON-ZERO datapoint count, e.g.
     "EC2 CWAgent mem: 1 datapoints / 1 of 20 instances".

  C) Confirm real data landed:
       mysql -u monitor -p monitoring_hub -e \\
         "SELECT r.resource_id, m.metric_value, m.metric_timestamp
          FROM metrics m JOIN resources r ON r.id = m.resource_id
          WHERE r.resource_type='ec2' AND m.metric_name='mem_used_percent';"
     Should show a real row now, with a value close to what the
     Services page chart already shows for the same instance.

  D) Review, commit, push:
       git diff app/collector/metrics/runner.py
       git add app/collector/metrics/runner.py apply_fix_cwagent_mem_dimensions.py
       git commit -m "fix(ec2): CWAgent memory collector assumed InstanceId-only dimensions, returning 0 datapoints for an instance with real, visible data -- now discovers the actual full dimension set per instance via ListMetrics, matching the already-working live chart's own approach."
       git push origin main
""")


if __name__ == "__main__":
    main()
