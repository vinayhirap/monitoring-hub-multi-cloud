#!/usr/bin/env python3
"""
apply_fix_getthreshold_scope.py
========================================
URGENT fix for a live JavaScript error, confirmed directly from the
browser console: "Uncaught ReferenceError: getThreshold is not
defined". apply_fix_hardcoded_chart_thresholds.py added the
thresholdMap state, its fetch effect, and the getThreshold() lookup
function inside ServiceDetail() -- but every <MetricChart threshold=
{getThreshold(...)}> call that actually needs it lives inside
ServiceDetailPanel, a completely SEPARATE, SIBLING function component
defined later in the same file (confirmed: `function
ServiceDetailPanel({ service, row, metrics, ... })` at its own top
level, not nested inside ServiceDetail()). JavaScript function
components don't share scope with their siblings -- only a parent's own
nested closures or explicitly-passed props are visible inside a child.
getThreshold was accessible where it was defined, but not where it was
actually called, so the page crashed at render time despite the
frontend build succeeding cleanly (a build only checks syntax, not
runtime scope across component boundaries -- exactly why this needed a
live browser test to catch, not just `npm run build`).

THE FIX
---------
Moves the thresholdMap state, its fetch effect, and the getThreshold()
function into ServiceDetailPanel itself, using its OWN already-existing
accountId prop (no prop-drilling changes needed at the call site --
ServiceDetailPanel already receives accountId={id} from its parent).
Every chart that uses getThreshold() now calls a function that's
genuinely in scope, in the same component that renders them.

TESTED: this time went beyond "the build succeeds" (which passed for
the ORIGINAL broken version too, since a bundler doesn't check
cross-component runtime scope) -- wrote a script that parses the actual
function boundaries in the source and confirms both the getThreshold
definition AND every one of its 17 real call sites (excluding one false
match against this very explanatory comment's own text) fall strictly
within ServiceDetailPanel's boundaries, not split across two different
component scopes. Also re-ran `npm run build` to confirm it still
compiles clean.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_getthreshold_scope.py --dry-run
    python3 apply_fix_getthreshold_scope.py --apply
    cd frontend && npm install && npm run build && cd ..
(the frontend rebuild is required -- this changes .jsx source, not the
built dist/ the server actually serves)
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

REMOVE_OLD = '''  const [activeAlerts, setActiveAlerts] = useState([]);
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

  const loadRows = useCallback(async () => {'''

REMOVE_NEW = '''  const [activeAlerts, setActiveAlerts] = useState([]);
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

  const loadRows = useCallback(async () => {'''

ADD_OLD = '''function ServiceDetailPanel({ service, row, metrics, mLoading, region, timeRange, onTimeRangeChange, allRows, onClose, onSelectRelated, accountId }) {
  const name = row.name || row.service_name || row.identifier || row.function_name || row.bucket_name || row.instance_id || "Resource";'''

ADD_NEW = '''function ServiceDetailPanel({ service, row, metrics, mLoading, region, timeRange, onTimeRangeChange, allRows, onClose, onSelectRelated, accountId }) {
  const [thresholdMap, setThresholdMap] = useState({});

  // Real, currently-configured thresholds for this account -- charts
  // used to draw a hardcoded, unrelated example number as the dashed
  // reference line (e.g. always "85" for CPUUtilization no matter what
  // Settings -> Metric Thresholds actually has configured for this
  // account). include_no_data=true because a chart-page visitor
  // benefits from seeing a configured-but-not-yet-collecting threshold
  // just as much as an actively-firing one -- the "hide no data" filter
  // is specifically for decluttering the Settings list, not relevant
  // here. Lives in THIS component, not the parent ServiceDetail(),
  // because that's a separate function scope -- the earlier version of
  // this fix defined getThreshold() in the parent while every chart
  // that needs it renders here, causing a live
  // "ReferenceError: getThreshold is not defined". See
  // apply_fix_getthreshold_scope.py.
  useEffect(() => {
    if (!accountId) return;
    fetch(`/api/settings/thresholds?account_id=${accountId}&include_no_data=true`)
      .then(r => r.ok ? r.json() : { thresholds: [] })
      .then(data => {
        const map = {};
        (data.thresholds || []).forEach(t => {
          if (t.metric_name) map[`${t.resource_type}:${t.metric_name}`] = t.warning_value;
        });
        setThresholdMap(map);
      })
      .catch(() => {});
  }, [accountId]);

  // Looks up the REAL warning threshold configured in Settings for this
  // exact (resourceType, metricName) pair -- returns undefined if none
  // is configured, which MetricChart already correctly renders as "no
  // dashed line" rather than a misleading default.
  function getThreshold(resourceType, metricName) {
    return thresholdMap[`${resourceType}:${metricName}`];
  }

  const name = row.name || row.service_name || row.identifier || row.function_name || row.bucket_name || row.instance_id || "Resource";'''


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

    if "Lives in THIS component, not the parent ServiceDetail()" in content:
        print("\nAlready patched -- nothing to do.")
        return

    if content.count(REMOVE_OLD) != 1:
        die("Removal anchor not found exactly once in ServiceDetail() -- file may differ from what this script expects.")
    if content.count(ADD_OLD) != 1:
        die("Insertion anchor not found exactly once in ServiceDetailPanel() -- file may differ from what this script expects.")

    new_content = content.replace(REMOVE_OLD, REMOVE_NEW, 1)
    new_content = new_content.replace(ADD_OLD, ADD_NEW, 1)

    print(f"\nFile patch plan:\n  frontend/src/pages/ServiceDetail.jsx: OK ({len(new_content) - len(content):+d} bytes)")

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

  C) THE REAL TEST -- open browser DevTools console BEFORE loading the
     page this time (not just checking the build succeeded, which
     passed for the broken version too): open any EC2/EBS/RDS/Lambda/
     ELB/ECS instance's detail page, click into a resource to load its
     metrics panel, confirm NO "getThreshold is not defined" error
     appears, and confirm the CPUUtilization chart's dashed line now
     reflects whatever's actually configured in Settings (e.g. 25/45
     per your own screenshot, not a hardcoded 85).

  D) Review, commit, push:
       git diff frontend/src/pages/ServiceDetail.jsx
       git add frontend/src/pages/ServiceDetail.jsx apply_fix_getthreshold_scope.py
       git commit -m "fix(urgent): getThreshold() was defined in the wrong component scope (ServiceDetail() instead of ServiceDetailPanel(), a separate sibling component where every chart that calls it actually lives), causing a live ReferenceError on every resource detail panel. Moved to the component that actually uses it."
       git push origin main
""")


if __name__ == "__main__":
    main()
