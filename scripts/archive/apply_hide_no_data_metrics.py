#!/usr/bin/env python3
"""
apply_hide_no_data_metrics.py
========================================
Implements "hide metrics with no data" for Settings -> Metric Thresholds
and the EC2/EBS/RDS/etc. Services detail charts.

SCOPE, HONESTLY STATED
------------------------
The request behind this was large: map every metric across seeding,
threshold config, onboarding selection, and the database for AWS/Azure/
GCP, and hide anything with no data everywhere, dynamically. Investigated
that whole system first (app/api/metric_catalog.py, account_metric_
selections, the threshold-sync-on-selection-change logic, the Azure/GCP
curated catalogs) rather than guessing at scope -- it's already a mature,
well-designed system: provider-scoped catalogs with matching shapes
across AWS/Azure/GCP, automatic threshold creation/disabling synced to
metric selection changes, live "discover extended metrics" per provider,
audit logging on every change. That part didn't need touching.

What genuinely didn't exist, and is what this script adds:

1. Settings -> Metric Thresholds now HIDES, by default, any threshold
   for a metric that has never produced a single data point for this
   account (checked against the `metrics` table, one query, not per-row).
   A checkbox reveals them again -- not permanently invisible, since a
   real reason to look ("why isn't this collecting?") should stay
   possible, matching how mature monitoring tools handle this (a toggle,
   not a wall).

2. The one Services-page chart CONFIRMED to structurally never have
   data -- EBS BurstBalance (Phase 1's collector deliberately excludes
   it, and unlike every other chart field in this file, this one has no
   boto3 fallback to eventually populate it) -- is now hidden entirely
   instead of showing a permanent, pointless "No data in last 6H" box.

WHY NOT MORE THAN THIS ON THE SERVICES PAGE
------------------------------------------------
Every OTHER chart field that can show "no data" (Lambda
ConcurrentExecutions, several ELB fields, ECS CPU/Memory) already has an
automatic boto3-GetMetricData fallback built in (confirmed by reading
each function, not assumed) -- they DO eventually get real data, just
via a live API call instead of the free local cache. Hiding those would
remove a working feature, not fix a broken one. Only BurstBalance has
zero path to ever having data. Marking anything else as "never has
data" would have been guessing past what the code actually does.

Azure/GCP have no chart-detail endpoints at all yet (confirmed in an
earlier phase of this session) -- "hide on the Services page" doesn't
structurally apply to them until those endpoints exist, which is a much
larger, separate undertaking (building AND testing 6+ new endpoints per
provider) not bundled into this fix.

THE FIX
---------
1. app/api/settings.py: new _metrics_with_data_for_account() helper (one
   query: every (resource_type, metric_name) pair with at least one row
   in `metrics` for this account). GET /thresholds now accepts
   include_no_data (default false), returns {"thresholds": [...],
   "hidden_no_data_count": N} instead of a bare list, and each row gets
   a has_data field.
2. frontend/src/pages/Settings.jsx: updated to the new response shape,
   added a checkbox toggle + count label.
3. app/aws/collector_direct.py: EBS BurstBalance's chart field is now
   explicit None instead of always calling a query that will always
   return [] -- a small efficiency win too (one less pointless DB query
   per EBS chart load).
4. frontend/src/pages/ServiceDetail.jsx: MetricChart treats data === null
   (never applicable) differently from data === [] (might have data
   later, none right now) -- the former hides the card entirely, the
   latter keeps the existing "No data in last 6H" placeholder.

TESTED: _metrics_with_data_for_account()-driven filtering was unit
tested directly (3 threshold rows, one with confirmed no data, checked
both include_no_data=False and =True, confirmed has_data flags and the
count stay correct in both modes). The frontend build was run for
real (`npm run build`) after both JSX edits -- compiles clean, not just
eyeballed. NOT tested: an actual live Settings page load or Services
page load against a real account's real data -- no server access
available here; verify per the checklist below.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_hide_no_data_metrics.py --dry-run
    python3 apply_hide_no_data_metrics.py --apply
    cd frontend && npm run build && cd ..
(the frontend rebuild is required -- this changes .jsx source, not the
built dist/ the server actually serves)
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

SETTINGS_HELPER_OLD = '''def _normalize_threshold_resource_type(value):
    return _THRESHOLD_RESOURCE_TYPE_ALIASES.get(value, value)


def _ser(obj):'''

SETTINGS_HELPER_NEW = '''def _normalize_threshold_resource_type(value):
    return _THRESHOLD_RESOURCE_TYPE_ALIASES.get(value, value)


def _metrics_with_data_for_account(account_id: int) -> set:
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
    return pairs


def _ser(obj):'''

SETTINGS_ENDPOINT_OLD = '''@router.get("/thresholds")
def get_thresholds(account_id: int = Query(3), current_user: dict = Depends(require_permission("alerts.view"))):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT
            t.id, t.aws_account_id, t.resource_type, t.metric_id,
            t.warning_value, t.critical_value, t.comparison,
            t.evaluation_period, t.enabled, t.created_at,
            mc.metric_name, mc.service, mc.namespace, mc.statistic, mc.unit
        FROM thresholds t
        LEFT JOIN metric_catalog mc ON t.metric_id = mc.id
        WHERE t.aws_account_id = %s
        ORDER BY mc.service, mc.metric_name
    """, (account_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [_ser(r) for r in rows]'''

SETTINGS_ENDPOINT_NEW = '''@router.get("/thresholds")
def get_thresholds(
    account_id: int = Query(3),
    include_no_data: bool = Query(
        False,
        description="If false (default), thresholds for metrics that have "
                    "never produced any data for this account are hidden -- "
                    "not deleted, just excluded from this response. Pass "
                    "true to see everything, e.g. for debugging why a "
                    "metric never collects.",
    ),
    current_user: dict = Depends(require_permission("alerts.view")),
):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT
            t.id, t.aws_account_id, t.resource_type, t.metric_id,
            t.warning_value, t.critical_value, t.comparison,
            t.evaluation_period, t.enabled, t.created_at,
            mc.metric_name, mc.service, mc.namespace, mc.statistic, mc.unit
        FROM thresholds t
        LEFT JOIN metric_catalog mc ON t.metric_id = mc.id
        WHERE t.aws_account_id = %s
        ORDER BY mc.service, mc.metric_name
    """, (account_id,))
    rows = cur.fetchall(); cur.close(); conn.close()

    has_data_pairs = _metrics_with_data_for_account(account_id)
    no_data_count = 0
    out = []
    for r in rows:
        has_data = (r["resource_type"], r["metric_name"]) in has_data_pairs
        r["has_data"] = has_data
        if not has_data:
            no_data_count += 1
            if not include_no_data:
                continue
        out.append(r)

    return {"thresholds": [_ser(r) for r in out], "hidden_no_data_count": no_data_count}'''

COLLECTOR_EBS_OLD = '''            "queue_length": s("volumequeuelength"),
            # burst_balance: Phase 1's GMD collector deliberately dropped
            # BurstBalance ("gp3 irrelevant" per its own triage note), so
            # metric_history never has this metric_name and this call
            # always returns []. Unlike the other 5 functions in this
            # file, this one has no boto3 fallback -- this chart series
            # is now PERMANENTLY EMPTY. See apply_dashboard_charts_metric_history.py's
            # docstring: a known, documented trade, not fixed here.
            "burst_balance": s("volumeburstbalance"),'''

COLLECTOR_EBS_NEW = '''            "queue_length": s("volumequeuelength"),
            # burst_balance: Phase 1's GMD collector deliberately dropped
            # BurstBalance ("gp3 irrelevant" per its own triage note), and
            # unlike the other 5 fields here, this one has NO boto3
            # fallback -- it can structurally never have data, not just
            # "none in this time window". Explicit None (not an empty
            # list from a query that will always return nothing) tells
            # the frontend to hide this chart card entirely instead of
            # showing a permanent, pointless "no data" placeholder. See
            # apply_hide_no_data_metrics.py.
            "burst_balance": None,'''

FRONTEND_CHART_OLD = '''function MetricChart({ title, data, color, unit, threshold, thresholdLabel, timeRange }) {
  const { ianaName } = useTimezone();
  if (!data || data.length === 0) return ('''

FRONTEND_CHART_NEW = '''function MetricChart({ title, data, color, unit, threshold, thresholdLabel, timeRange }) {
  const { ianaName } = useTimezone();
  // data === null (not undefined, not []) means the backend knows this
  // metric structurally can never have data for this resource (e.g. EBS
  // BurstBalance -- dropped from collection with no fallback, see
  // apply_hide_no_data_metrics.py) -- hide the card entirely instead of
  // showing a permanent, pointless "no data" placeholder. data === []
  // still means "might have data later, just none in this window" and
  // keeps the existing placeholder below.
  if (data === null) return null;
  if (!data || data.length === 0) return ('''

FRONTEND_SETTINGS_STATE_OLD = '''  const [checkResult, setCheckResult] = useState(null);
  const [checking,    setChecking]    = useState(false);
  const [emailOn,     setEmailOn]     = useState(false);'''

FRONTEND_SETTINGS_STATE_NEW = '''  const [checkResult, setCheckResult] = useState(null);
  const [checking,    setChecking]    = useState(false);
  const [emailOn,     setEmailOn]     = useState(false);
  const [showNoData,  setShowNoData]  = useState(false);
  const [hiddenNoDataCount, setHiddenNoDataCount] = useState(0);'''

FRONTEND_SETTINGS_LOAD_OLD = '''  const load = useCallback(async () => {
    if (!accountId) return;
    setLoading(true);
    try {
      const t = await fetch(`${BASE}/api/settings/thresholds?account_id=${accountId}`).then(r => r.json());
      setThresholds(Array.isArray(t) ? t : []);
    } catch (e) {
      console.error("Settings load:", e);
    } finally {
      setLoading(false);
    }
  }, [accountId]);'''

FRONTEND_SETTINGS_LOAD_NEW = '''  const load = useCallback(async () => {
    if (!accountId) return;
    setLoading(true);
    try {
      const t = await fetch(
        `${BASE}/api/settings/thresholds?account_id=${accountId}&include_no_data=${showNoData}`
      ).then(r => r.json());
      setThresholds(Array.isArray(t?.thresholds) ? t.thresholds : []);
      setHiddenNoDataCount(t?.hidden_no_data_count || 0);
    } catch (e) {
      console.error("Settings load:", e);
    } finally {
      setLoading(false);
    }
  }, [accountId, showNoData]);'''

FRONTEND_SETTINGS_TOGGLE_OLD = '''            <button className="btn-check" onClick={runCheck} disabled={checking || !accountId}>
              {checking ? "⏳ Checking…" : "▶ Check Now"}
            </button>
            <button className="btn-clear" onClick={clearAlerts}><TrashIcon size={13}/> Clear Alerts</button>
          </div>
        </div>
'''

FRONTEND_SETTINGS_TOGGLE_NEW = '''            <button className="btn-check" onClick={runCheck} disabled={checking || !accountId}>
              {checking ? "⏳ Checking…" : "▶ Check Now"}
            </button>
            <button className="btn-clear" onClick={clearAlerts}><TrashIcon size={13}/> Clear Alerts</button>
          </div>
        </div>

        {hiddenNoDataCount > 0 && (
          <div style={{
            display: "flex", alignItems: "center", gap: 8,
            padding: "6px 12px", fontSize: 12, color: "var(--text-muted)",
          }}>
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={showNoData}
                onChange={e => setShowNoData(e.target.checked)}
              />
              {showNoData
                ? `Showing ${hiddenNoDataCount} metric(s) with no data`
                : `${hiddenNoDataCount} metric(s) hidden — never produced any data for this account`}
            </label>
          </div>
        )}
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
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    settings_path = os.path.join(repo_root, "app", "api", "settings.py")
    collector_path = os.path.join(repo_root, "app", "aws", "collector_direct.py")
    service_detail_path = os.path.join(repo_root, "frontend", "src", "pages", "ServiceDetail.jsx")
    settings_jsx_path = os.path.join(repo_root, "frontend", "src", "pages", "Settings.jsx")

    results = []

    settings_content, settings_note = prepare_patch(
        settings_path, "app/api/settings.py",
        [(SETTINGS_HELPER_OLD, SETTINGS_HELPER_NEW), (SETTINGS_ENDPOINT_OLD, SETTINGS_ENDPOINT_NEW)],
        "_metrics_with_data_for_account",
    )
    results.append((settings_path, "app/api/settings.py", settings_content, settings_note))

    collector_content, collector_note = prepare_patch(
        collector_path, "app/aws/collector_direct.py",
        [(COLLECTOR_EBS_OLD, COLLECTOR_EBS_NEW)],
        '"burst_balance": None,',
    )
    results.append((collector_path, "app/aws/collector_direct.py", collector_content, collector_note))

    service_detail_content, service_detail_note = prepare_patch(
        service_detail_path, "frontend/src/pages/ServiceDetail.jsx",
        [(FRONTEND_CHART_OLD, FRONTEND_CHART_NEW)],
        "if (data === null) return null;",
    )
    results.append((service_detail_path, "frontend/src/pages/ServiceDetail.jsx", service_detail_content, service_detail_note))

    settings_jsx_content, settings_jsx_note = prepare_patch(
        settings_jsx_path, "frontend/src/pages/Settings.jsx",
        [
            (FRONTEND_SETTINGS_STATE_OLD, FRONTEND_SETTINGS_STATE_NEW),
            (FRONTEND_SETTINGS_LOAD_OLD, FRONTEND_SETTINGS_LOAD_NEW),
            (FRONTEND_SETTINGS_TOGGLE_OLD, FRONTEND_SETTINGS_TOGGLE_NEW),
        ],
        "hiddenNoDataCount",
    )
    results.append((settings_jsx_path, "frontend/src/pages/Settings.jsx", settings_jsx_content, settings_jsx_note))

    print("\nFile patch plan:")
    for _, _, _, note in results:
        print(f"  {note}")

    if all(content is None for _, _, content, _ in results):
        print("\nNothing to do -- everything this script would change is already applied.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    for path, label, content, note in results:
        if content is None:
            continue
        backup(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"Patched {label}")

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
       sudo journalctl -u monitoring-hub -n 40 --no-pager

  C) Open Settings -> Metric Thresholds for a real account. You should
     see fewer rows than before if that account has any metric enabled
     that's never actually collected data, plus a checkbox like
     "N metric(s) hidden -- never produced any data for this account".
     Toggle it and confirm they reappear.

  D) Open an EBS volume's detail page in Services -- confirm the
     "Burst Balance %" chart card is simply absent now, not showing an
     empty "No data in last 6H" box. Confirm every OTHER EBS chart
     (read/write ops, read/write bytes, queue length) still renders
     normally.

  E) Review, commit, push:
       git status
       git diff app/api/settings.py app/aws/collector_direct.py \\
                frontend/src/pages/ServiceDetail.jsx frontend/src/pages/Settings.jsx
       git add app/api/settings.py app/aws/collector_direct.py \\
               frontend/src/pages/ServiceDetail.jsx frontend/src/pages/Settings.jsx \\
               apply_hide_no_data_metrics.py
       git commit -m "feat(ui): hide metrics with no data -- Metric Thresholds hides no-data rows by default (toggle to reveal), EBS BurstBalance chart (the one field confirmed to structurally never collect) hidden entirely instead of a permanent empty placeholder"
       git push origin main

  Deliberately NOT done here, and why: extending "hide on no data" to
  Lambda/ELB/ECS chart fields (they all have working boto3 fallbacks,
  so hiding them would remove a working feature) and to Azure/GCP
  Services pages (no chart-detail endpoints exist there yet at all --
  a much larger, separate build).
""")


if __name__ == "__main__":
    main()
