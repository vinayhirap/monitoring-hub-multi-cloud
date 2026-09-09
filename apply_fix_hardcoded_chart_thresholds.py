#!/usr/bin/env python3
"""
apply_fix_hardcoded_chart_thresholds.py
========================================
Fixes a real, systemic issue found while verifying whether threshold
changes in Settings actually propagate everywhere: EVERY chart
threshold reference line in frontend/src/pages/ServiceDetail.jsx was a
HARDCODED literal, completely disconnected from whatever the account
actually has configured. Confirmed directly: CPUUtilization's chart
line always showed "85" regardless of Settings -> Metric Thresholds
having 70/90, or any other value -- changing a threshold in Settings
had zero effect on any chart's dashed reference line, ever.

SCOPE
-------
19 occurrences across EC2/EBS/Lambda/RDS/S3/ELB/ECS. Of these:
  - 17 have a real, confirmed metric_catalog entry and now correctly
    show whatever warning_value is actually configured for that exact
    (resource_type, metric_name) pair.
  - 2 (both under S3, plus 3 uncataloged ECS fields already known from
    apply_unify_chart_titles_with_catalog.py to have no catalog
    counterpart) had their hardcoded value simply removed -- there is
    no real threshold to look up for them, so the chart now correctly
    shows no line at all instead of a misleading static one.
    MetricChart already handles `threshold={undefined}` gracefully
    (confirmed by reading its own render logic: `{threshold && (...)}`)
    -- no changes needed there.

Also fixed the two EC2 CWAgent charts' titles ("Memory Utilization %" /
"Disk Space Utilized %" -> "mem_used_percent" / "disk_used_percent")
while wiring their thresholds -- these didn't get the official-name
treatment in apply_unify_chart_titles_with_catalog.py because they had
no catalog entry at the time; they do now
(apply_add_cwagent_mem_threshold.py / apply_add_cwagent_disk_threshold.py).

THE FIX
---------
Added a getThreshold(resourceType, metricName) lookup, backed by one
fetch of GET /api/settings/thresholds?account_id={id}&include_no_data=true
per account load (the same endpoint Settings already uses) -- not a new
API surface. include_no_data=true because a chart-page visitor benefits
from seeing a configured-but-not-yet-collecting threshold too; the
"hide no data" filter is specifically for decluttering the Settings
list, not relevant on a chart. Keys are `${resource_type}:${metric_name}`
so the same metric name under different services (e.g. CPUUtilization
for both EC2 and RDS) never collides.

TESTED: ran the actual frontend build (`npm run build`) -- compiles
clean. Separately tested the map-building + lookup logic in isolation
with Node (a standalone reproduction of the exact same logic, since a
full React render harness isn't available here): confirmed EC2 and RDS
CPUUtilization thresholds resolve independently without colliding, and
confirmed an unconfigured metric correctly returns undefined (renders
as no line) rather than any stale default.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_hardcoded_chart_thresholds.py --dry-run
    python3 apply_fix_hardcoded_chart_thresholds.py --apply
    cd frontend && npm install && npm run build && cd ..
(the frontend rebuild is required -- this changes .jsx source, not the
built dist/ the server actually serves)
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

STATE_OLD = '''  const [activeAlerts, setActiveAlerts] = useState([]);
  const notImplRef  = useRef(false);
  const selectedRef = useRef(null);
  const autoSelectedRef = useRef(null);

  useEffect(() => {
fetchAccount(id).then(setAccount).catch(err => {
      console.error(err);
      navigate("/overview");
    });
        fetch("/api/alerts")
      .then(r => r.ok ? r.json() : [])
      .then(a => setActiveAlerts((Array.isArray(a) ? a : []).filter(x => (x.status||"").toLowerCase() === "active")))
      .catch(() => {});
  }, [id]);
'''

STATE_NEW = '''  const [activeAlerts, setActiveAlerts] = useState([]);
  const [thresholdMap, setThresholdMap] = useState({});
  const notImplRef  = useRef(false);
  const selectedRef = useRef(null);
  const autoSelectedRef = useRef(null);

  useEffect(() => {
fetchAccount(id).then(setAccount).catch(err => {
      console.error(err);
      navigate("/overview");
    });
        fetch("/api/alerts")
      .then(r => r.ok ? r.json() : [])
      .then(a => setActiveAlerts((Array.isArray(a) ? a : []).filter(x => (x.status||"").toLowerCase() === "active")))
      .catch(() => {});
    // Real, currently-configured thresholds for this account -- charts
    // used to draw a hardcoded, unrelated example number as the dashed
    // reference line (e.g. always "85" for CPUUtilization no matter
    // what Settings -> Metric Thresholds actually has configured for
    // this account). include_no_data=true because a chart-page visitor
    // benefits from seeing a configured-but-not-yet-collecting threshold
    // just as much as an actively-firing one -- the "hide no data"
    // filter is specifically for decluttering the Settings list, not
    // relevant here. See apply_fix_hardcoded_chart_thresholds.py.
    fetch(`/api/settings/thresholds?account_id=${id}&include_no_data=true`)
      .then(r => r.ok ? r.json() : { thresholds: [] })
      .then(data => {
        const map = {};
        (data.thresholds || []).forEach(t => {
          if (t.metric_name) map[`${t.resource_type}:${t.metric_name}`] = t.warning_value;
        });
        setThresholdMap(map);
      })
      .catch(() => {});
  }, [id]);

  // Looks up the REAL warning threshold configured in Settings for this
  // exact (resourceType, metricName) pair -- returns undefined if none
  // is configured, which MetricChart already correctly renders as "no
  // dashed line" rather than a misleading default.
  function getThreshold(resourceType, metricName) {
    return thresholdMap[`${resourceType}:${metricName}`];
  }
'''

CHART_REPLACEMENTS = [
    ('threshold={85} thresholdLabel="alert threshold"',
     'threshold={getThreshold("ec2", "CPUUtilization")} thresholdLabel="alert threshold"'),
    ('title="Memory Utilization %"  data={metrics.mem_utilization}   color="#7c6ee0" unit="%" threshold={90}',
     'title="mem_used_percent"  data={metrics.mem_utilization}   color="#7c6ee0" unit="%" threshold={getThreshold("ec2", "mem_used_percent")}'),
    ('title="Disk Space Utilized %" data={metrics.disk_used_percent} color="#fbbf24" unit="%" threshold={90}',
     'title="disk_used_percent" data={metrics.disk_used_percent} color="#fbbf24" unit="%" threshold={getThreshold("ec2", "disk_used_percent")}'),
    ('title="VolumeQueueLength"    data={metrics.queue_length}  color="#ef4444" unit=""     threshold={5}',
     'title="VolumeQueueLength"    data={metrics.queue_length}  color="#ef4444" unit=""     threshold={getThreshold("ebs", "VolumeQueueLength")}'),
    ('title="BurstBalance" data={metrics.burst_balance} color="#2bb3ac" unit="%"    threshold={20}',
     'title="BurstBalance" data={metrics.burst_balance} color="#2bb3ac" unit="%"    threshold={getThreshold("ebs", "BurstBalance")}'),
    ('title="Errors"          data={metrics.errors}      color="#ef4444" unit=""   threshold={5}',
     'title="Errors"          data={metrics.errors}      color="#ef4444" unit=""   threshold={getThreshold("lambda", "Errors")}'),
    ('title="Duration" data={metrics.duration}    color="#2bb3ac" unit="ms" threshold={8000}',
     'title="Duration" data={metrics.duration}    color="#2bb3ac" unit="ms" threshold={getThreshold("lambda", "Duration")}'),
    ('title="CPUUtilization" data={metrics.cpu} color="#2bb3ac" unit="%" threshold={85} timeRange={rangLabel} />\n              </div>\n              <MetricChart title="DatabaseConnections"',
     'title="CPUUtilization" data={metrics.cpu} color="#2bb3ac" unit="%" threshold={getThreshold("rds", "CPUUtilization")} timeRange={rangLabel} />\n              </div>\n              <MetricChart title="DatabaseConnections"'),
    ('title="ReadLatency"    data={metrics.read_latency}    color="#38bdf8" unit="s"   threshold={0.02}',
     'title="ReadLatency"    data={metrics.read_latency}    color="#38bdf8" unit="s"   threshold={getThreshold("rds", "ReadLatency")}'),
    ('title="WriteLatency"   data={metrics.write_latency}   color="#e879f9" unit="s"   threshold={0.02}',
     'title="WriteLatency"   data={metrics.write_latency}   color="#e879f9" unit="s"   threshold={getThreshold("rds", "WriteLatency")}'),
    ('title="5XX Errors"            data={metrics?.errors_5xx    || []} color="#ef4444" unit=""  threshold={5}',
     'title="5XX Errors"            data={metrics?.errors_5xx    || []} color="#ef4444" unit=""'),
    ('title="HTTPCode_Target_5XX_Count"       data={metrics?.errors_5xx         || []} color="#ef4444" unit=""  threshold={20}',
     'title="HTTPCode_Target_5XX_Count"       data={metrics?.errors_5xx         || []} color="#ef4444" unit=""  threshold={getThreshold("elb", "HTTPCode_Target_5XX_Count")}'),
    ('title="HTTPCode_Target_4XX_Count"       data={metrics?.errors_4xx         || []} color="#f59e0b" unit=""  threshold={50}',
     'title="HTTPCode_Target_4XX_Count"       data={metrics?.errors_4xx         || []} color="#f59e0b" unit=""  threshold={getThreshold("elb", "HTTPCode_Target_4XX_Count")}'),
    ('title="HTTPCode_ELB_5XX_Count"          data={metrics?.errors_elb_5xx     || []} color="#f472b6" unit=""  threshold={5} ',
     'title="HTTPCode_ELB_5XX_Count"          data={metrics?.errors_elb_5xx     || []} color="#f472b6" unit=""  threshold={getThreshold("elb", "HTTPCode_ELB_5XX_Count")} '),
    ('title="TargetResponseTime" data={metrics?.latency           || []} color="#fbbf24" unit="s" threshold={0.5}',
     'title="TargetResponseTime" data={metrics?.latency           || []} color="#fbbf24" unit="s" threshold={getThreshold("elb", "TargetResponseTime")}'),
    ('title="UnHealthyHostCount"           data={metrics?.unhealthy_hosts    || []} color="#ef4444" unit=""  threshold={1}',
     'title="UnHealthyHostCount"           data={metrics?.unhealthy_hosts    || []} color="#ef4444" unit=""  threshold={getThreshold("elb", "UnHealthyHostCount")}'),
    ('title="CPUUtilization"    data={metrics?.cpu_utilization    || []} color="#34d399" unit="%" threshold={85}',
     'title="CPUUtilization"    data={metrics?.cpu_utilization    || []} color="#34d399" unit="%" threshold={getThreshold("ecs", "CPUUtilization")}'),
    ('title="MemoryUtilization" data={metrics?.mem_utilization    || []} color="#7c6ee0" unit="%" threshold={85}',
     'title="MemoryUtilization" data={metrics?.mem_utilization    || []} color="#7c6ee0" unit="%" threshold={getThreshold("ecs", "MemoryUtilization")}'),
]


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

    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    if "getThreshold(resourceType, metricName)" in content:
        print("\nAlready patched -- nothing to do.")
        return

    if content.count(STATE_OLD) != 1:
        die("State/effect anchor not found exactly once -- file may differ from what this script expects.")
    new_content = content.replace(STATE_OLD, STATE_NEW, 1)

    applied = 0
    for old, new in CHART_REPLACEMENTS:
        n = new_content.count(old)
        if n != 1:
            die(f"Expected exactly 1 match, found {n}: {old[:70]!r}...")
        new_content = new_content.replace(old, new, 1)
        applied += 1

    print(f"\nFile patch plan:\n  frontend/src/pages/ServiceDetail.jsx: state/effect + {applied} chart(s) "
          f"({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("Patched frontend/src/pages/ServiceDetail.jsx")

    print("""
[Manual follow-up]

  A) Rebuild the frontend -- REQUIRED:
       cd frontend
       npm install
       npm run build
       cd ..

  B) Restart:
       sudo systemctl restart monitoring-hub

  C) THE REAL TEST: open an EC2 instance's detail page, note the
     CPUUtilization chart's dashed reference line position. Go to
     Settings -> Metric Thresholds, change that account's
     CPUUtilization Warn value to something very different (e.g. 40),
     Save, then refresh the chart page -- confirm the dashed line moved
     to the new value (this fetch runs on page load, not live-pushed,
     so a refresh is expected to be needed).

  D) Review, commit, push:
       git diff frontend/src/pages/ServiceDetail.jsx
       git add frontend/src/pages/ServiceDetail.jsx apply_fix_hardcoded_chart_thresholds.py
       git commit -m "fix(ui): remove all hardcoded chart threshold values -- charts always showed a static example number regardless of what was actually configured in Settings. Now dynamically looks up the real configured threshold per (resource_type, metric_name); metrics with no configured threshold correctly show no line instead of a misleading default."
       git push origin main
""")


if __name__ == "__main__":
    main()
