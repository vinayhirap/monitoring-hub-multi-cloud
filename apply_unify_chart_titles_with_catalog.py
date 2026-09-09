#!/usr/bin/env python3
"""
apply_unify_chart_titles_with_catalog.py
========================================
Fixes a real UX inconsistency, reported directly: the same metric was
displayed under two completely different, independently-invented names
depending on which page you were looking at. Settings -> Metric
Thresholds shows the raw metric_catalog.metric_name (the official AWS
CloudWatch metric name, e.g. "VolumeReadOps", "BurstBalance") because
that's literally what's stored in the database and rendered directly.
The Services page's chart titles were separate, hand-written strings in
frontend/src/pages/ServiceDetail.jsx ("Read Ops/s", "Burst Balance %")
that were never reconciled against the catalog -- two different naming
schemes for the same 24 metrics, invented independently by whoever
wrote each page.

THE FIX
---------
Chart titles across EC2, EBS, Lambda, RDS, and ALB/ELB now use the
EXACT string stored in metric_catalog.metric_name for that metric --
confirmed against app/aws/metric_catalog_data.py directly, not
reconstructed from memory or AWS documentation. Some titles (e.g.
"Invocations", "Errors", "Throttles") already matched and needed no
change; others ("Read Ops/s" -> "VolumeReadOps", "DB Connections" ->
"DatabaseConnections") did not.

WHAT WAS DELIBERATELY LEFT UNCHANGED, AND WHY
--------------------------------------------------
Only metrics with a CONFIRMED metric_catalog entry got renamed --
guessing an "official" name for something not in the catalog would just
replace one invented name with another, unverified one:
  - EC2's "Memory Utilization %" / "Disk Space Utilized %": these are
    CloudWatch Agent (CWAgent) metrics, a different, custom namespace
    not present in metric_catalog's "ec2" entry at all -- confirmed by
    checking. No catalog counterpart to unify against.
  - ECS's "Desired Tasks" / "CPU Reserved" / "Memory Reserved":
    confirmed NOT in metric_catalog's "ecs" entry (which only has
    CPUUtilization, MemoryUtilization, RunningTaskCount,
    PendingTaskCount) -- likely leftover from an earlier version of
    this page. Left alone rather than guessed at.
  - ALL of S3's chart titles: metric_catalog's "s3" entry doesn't
    include "GetRequests"/"PutRequests"/"BytesDownloaded" at all (only
    BucketSizeBytes, NumberOfObjects, AllRequests, 4xxErrors, 5xxErrors,
    FirstByteLatency, TotalRequestLatency) -- the chart and catalog
    appear to have diverged more substantially here and this needs its
    own investigation before renaming anything, not a blind pass.

TESTED: ran the actual frontend build (`npm run build`) after applying
this change -- compiles clean, not just eyeballed. Diffed every
replaced title against app/aws/metric_catalog_data.py's stored
metric_name for that exact metric to confirm each one, not assumed
from AWS documentation memory.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_unify_chart_titles_with_catalog.py --dry-run
    python3 apply_unify_chart_titles_with_catalog.py --apply
    cd frontend && npm install && npm run build && cd ..
(the frontend rebuild is required -- this changes .jsx source, not the
built dist/ the server actually serves)
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

# (old title substring, new title substring) -- each checked for exactly
# 1 occurrence in the target file before being applied.
REPLACEMENTS = [
    ('title="CPU Utilization %" data={metrics.cpu} color="#2bb3ac" unit="%" threshold={85} thresholdLabel="alert threshold"',
     'title="CPUUtilization" data={metrics.cpu} color="#2bb3ac" unit="%" threshold={85} thresholdLabel="alert threshold"'),
    ('title="Network In (KB)" ', 'title="NetworkIn" '),
    ('title="Network Out (KB)"', 'title="NetworkOut"'),
    ('title="Read Ops/s"', 'title="VolumeReadOps"'),
    ('title="Write Ops/s"', 'title="VolumeWriteOps"'),
    ('title="Read Bytes"', 'title="VolumeReadBytes"'),
    ('title="Write Bytes"', 'title="VolumeWriteBytes"'),
    ('title="Queue Length"', 'title="VolumeQueueLength"'),
    ('title="Burst Balance %"', 'title="BurstBalance"'),
    ('title="Duration (ms)"', 'title="Duration"'),
    ('title="Concurrent Exec"', 'title="ConcurrentExecutions"'),
    ('title="CPU Utilization %" data={metrics.cpu} color="#2bb3ac" unit="%" threshold={85} timeRange={rangLabel} />\n              </div>\n              <MetricChart title="DB Connections"',
     'title="CPUUtilization" data={metrics.cpu} color="#2bb3ac" unit="%" threshold={85} timeRange={rangLabel} />\n              </div>\n              <MetricChart title="DatabaseConnections"'),
    ('title="Free Memory"', 'title="FreeableMemory"'),
    ('title="Read IOPS"', 'title="ReadIOPS"'),
    ('title="Write IOPS"', 'title="WriteIOPS"'),
    ('title="Read Latency"', 'title="ReadLatency"'),
    ('title="Write Latency"', 'title="WriteLatency"'),
    ('title="Request Count"', 'title="RequestCount"'),
    ('title="5XX Errors (Target)"', 'title="HTTPCode_Target_5XX_Count"'),
    ('title="4XX Errors (Target)"', 'title="HTTPCode_Target_4XX_Count"'),
    ('title="5XX Errors (ELB)"', 'title="HTTPCode_ELB_5XX_Count"'),
    ('title="Target Response Time (s)"', 'title="TargetResponseTime"'),
    ('title="Healthy Hosts"', 'title="HealthyHostCount"'),
    ('title="Unhealthy Hosts"', 'title="UnHealthyHostCount"'),
    ('title="Active Connections"', 'title="ActiveConnectionCount"'),
    ('title="New Connections"', 'title="NewConnectionCount"'),
    ('title="CPU Utilization %"    data={metrics?.cpu_utilization    || []} color="#34d399"',
     'title="CPUUtilization"    data={metrics?.cpu_utilization    || []} color="#34d399"'),
    ('title="Memory Utilization %" data={metrics?.mem_utilization    || []} color="#7c6ee0" unit="%" threshold={85} timeRange={rangLabel} />\n              </div>\n              <MetricChart title="Running Tasks"',
     'title="MemoryUtilization" data={metrics?.mem_utilization    || []} color="#7c6ee0" unit="%" threshold={85} timeRange={rangLabel} />\n              </div>\n              <MetricChart title="RunningTaskCount"'),
    ('title="Pending Tasks"', 'title="PendingTaskCount"'),
]

DONE_MARKER = 'title="VolumeReadOps"'


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
    path = os.path.join(repo_root, "frontend", "src", "pages", "ServiceDetail.jsx")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    with open(path) as f:
        content = f.read()

    if DONE_MARKER in content:
        print("\nAlready patched -- nothing to do.")
        return

    new_content = content
    applied = 0
    for old, new in REPLACEMENTS:
        n = new_content.count(old)
        if n == 0:
            die(f"Expected block not found: {old[:60]!r}... -- file may differ from what this script expects.")
        if n > 1:
            die(f"Expected exactly 1 match, found {n}: {old[:60]!r}...")
        new_content = new_content.replace(old, new, 1)
        applied += 1

    print(f"\nFile patch plan:\n  frontend/src/pages/ServiceDetail.jsx: {applied} title(s) unified "
          f"({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w") as f:
        f.write(new_content)
    print("Patched frontend/src/pages/ServiceDetail.jsx")

    print("""
[Manual follow-up]

  A) Rebuild the frontend -- REQUIRED, this changed .jsx source, not
     what's actually served:
       cd frontend
       npm install   # only if node_modules isn't already there
       npm run build
       cd ..

  B) Restart:
       sudo systemctl restart monitoring-hub

  C) Open an EBS volume's detail page -- chart titles should now read
     "VolumeReadOps", "VolumeWriteOps", "VolumeQueueLength", etc.,
     matching exactly what Settings -> Metric Thresholds shows for the
     same metrics. Spot-check EC2, RDS, Lambda, and ALB detail pages
     too.

  D) Review, commit, push:
       git diff frontend/src/pages/ServiceDetail.jsx
       git add frontend/src/pages/ServiceDetail.jsx apply_unify_chart_titles_with_catalog.py
       git commit -m "fix(ui): unify Services-page chart titles with the official CloudWatch metric names already shown in Settings -> Metric Thresholds -- same metric was displayed under two different invented names depending on which page you were on. EC2 CWAgent metrics, 3 uncataloged ECS fields, and all of S3 deliberately left alone -- no confirmed metric_catalog entry to unify against."
       git push origin main

  Still open, not done here: S3's chart titles need their own
  investigation first (several don't have a metric_catalog match at
  all, suggesting real divergence between the catalog and what's
  actually charted for S3, not just a naming mismatch).
""")


if __name__ == "__main__":
    main()
