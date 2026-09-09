#!/usr/bin/env python3
"""
apply_metrics_to_monitor_cleanup.py
========================================
Two related fixes to Settings -> Metrics to Monitor, both from direct
user feedback on a screenshot:

1. REMOVED the legacy YACE config.yml download buttons entirely.
   Previously only relabeled (apply_fix_stale_yace_documentation.py) as
   a "legacy export" to correct false claims about what they do -- the
   user has now explicitly said this is still not ideal to have
   visible at all, overriding the earlier, more cautious "keep for
   possible external use" decision. The backend endpoint
   (GET /api/account-metrics/{id}/yace-config) is left in place in case
   anyone with a genuine external need calls it directly; only the UI
   entry point and its now-unnecessary explanatory paragraph are
   removed.

2. NLB (and any other core-tier service sharing this problem) no
   longer appears in Metrics to Monitor when the account has zero
   matching discovered resources. Root cause: ALB and NLB are two
   separate metric_catalog services, but app/collector/discovery/
   runner.py stores BOTH under resources.resource_type='elb'
   uniformly (this codebase doesn't distinguish load balancer type at
   discovery time) -- the only way to tell them apart is the ARN
   pattern (loadbalancer/app/ vs loadbalancer/net/). Because nothing
   previously checked resource existence at all for this page (it's
   meant for selecting metrics, including ones for not-yet-provisioned
   resources), an account with real ALBs but zero NLBs still saw an
   "NLB" section to configure, which the user correctly flagged as
   confusing since they don't have that infrastructure.

   SAFETY: only hides a core service group if NOTHING in it is already
   enabled. Settings -> Metrics to Monitor's Save does a full-
   replacement PUT of whatever this GET returns -- silently dropping a
   group the user had already explicitly turned on (even if its
   resources are gone now) would silently disable it the next time they
   hit Save, without them ever choosing to. Confirmed with a test
   covering this exact scenario.

   SCOPE: only applied to the 7 core AWS services this app actually
   discovers resources for (ec2/ebs/rds/lambda/alb/nlb/ecs).
   EXTENDED-tier services (the ~33 others) are deliberately left alone
   -- this app has no discovery for any of them at all, so "zero
   resources found" would be true for literally every one, and hiding
   the whole extended tier would remove the ability to pre-select
   metrics before a resource is even provisioned, a legitimate use of
   this page.

TESTED: built a realistic account scenario (2 real ALBs, zero NLBs, EC2
resources present, zero ECS) and confirmed with the real
get_account_metrics() function: ALB and EC2 correctly shown, NLB and
ECS correctly hidden, and an extended-tier service (DynamoDB) is never
touched by this logic regardless of resource state. Separately
confirmed the safety guard: an NLB group with an already-enabled
selection is NOT hidden even with zero resources. Frontend build
(`npm run build`) confirmed clean after the YACE removal.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_metrics_to_monitor_cleanup.py --dry-run
    python3 apply_metrics_to_monitor_cleanup.py --apply
    cd frontend && npm install && npm run build && cd ..
(the frontend rebuild is required -- this changes .jsx source, not the
built dist/ the server actually serves)
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

# ── frontend: remove YACE buttons + orphaned help text ──────────────

FE_BUTTONS_OLD = '''            </button>
            {/* YACE is AWS/CloudWatch-specific -- Azure/GCP use this app's own
                push collectors instead (see app/providers/{azure,gcp}/
                metrics_collector.py), so these downloads are meaningless
                for them. Show for AWS or while nothing is selected yet
                (matches the existing disabled-until-selected behavior).
                LEGACY EXPORT, not this app's own collection mechanism --
                see the help paragraph below. Phase 1
                (apply_direct_gmd_metrics_revival.py) replaced YACE/VM with
                this app's own scheduler.py tiers for AWS; these buttons
                only remain for anyone still running an external YACE +
                VictoriaMetrics stack for their own separate purposes. */}
            {(!selectedAccount || selectedAccount.provider === "aws") && (
              <>
                <button className="btn-clear" onClick={() => handleDownloadYaceConfig("critical")} disabled={!accountId} title="Legacy YACE config export -- this app's own collection already runs on this schedule automatically, no deployment needed">
                  <DownloadIcon size={13}/> Critical (60s)
                </button>
                <button className="btn-clear" onClick={() => handleDownloadYaceConfig("standard")} disabled={!accountId} title="Legacy YACE config export -- this app's own collection already runs on this schedule automatically, no deployment needed">
                  <DownloadIcon size={13}/> Standard (300s)
                </button>
                <button className="btn-clear" onClick={() => handleDownloadYaceConfig("trend")} disabled={!accountId} title="Legacy YACE config export -- this app's own collection already runs on this schedule automatically, no deployment needed">
                  <DownloadIcon size={13}/> Trend (900s)
                </button>
              </>
            )}
            <button className="btn-check" onClick={saveMetricSelection} disabled={metricsSaving || !metricsDirty}>'''

FE_BUTTONS_NEW = '''            </button>
            {/* Legacy YACE config.yml download buttons REMOVED entirely --
                explicit user request, not just relabeled. This app's own
                AWS collection has used scheduler.py's built-in tiers since
                Phase 1 (apply_direct_gmd_metrics_revival.py); nothing syncs
                data back from an external YACE + VictoriaMetrics stack into
                this app anymore, so offering the download here was
                confusing regardless of how it was labeled. See
                apply_metrics_to_monitor_cleanup.py. The backend endpoint
                (GET /api/account-metrics/{id}/yace-config) is left in place
                in case it's used directly by anyone with a genuine external
                need -- only the UI entry point is removed. */}
            <button className="btn-check" onClick={saveMetricSelection} disabled={metricsSaving || !metricsDirty}>'''

FE_HELP_OLD = '''                AWS metrics for this account are collected automatically by
                this app's own scheduler (app/collector/scheduler.py) --
                Critical/Standard/Low tiers run built-in, direct
                GetMetricData calls every 2/5/15 minutes respectively, with
                no separate infrastructure to deploy. That's what actually
                controls GetMetricData cost and polling frequency today.
                The config.yml downloads below are a LEGACY export for
                anyone still running an external YACE + VictoriaMetrics
                stack for their own purposes (e.g. feeding a separate
                Grafana dashboard) -- deploying them has no effect on this
                app's own collection, cost, or alerting, since nothing
                syncs data back from VictoriaMetrics into this app anymore.
              </p>'''

FE_HELP_NEW = '''                AWS metrics for this account are collected automatically by
                this app's own scheduler (app/collector/scheduler.py) --
                Critical/Standard/Low tiers run built-in, direct
                GetMetricData calls every 2/5/15 minutes respectively, with
                no separate infrastructure to deploy. That's what actually
                controls GetMetricData cost and polling frequency today.
              </p>'''

# ── backend: hide core services with zero matching resources ────────

BE_OLD = '''@router.get("/api/account-metrics/{account_id}")
def get_account_metrics(account_id: int, current_user: dict = Depends(require_permission("metrics.view"))):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, provider FROM aws_accounts WHERE id = %s", (account_id,))
    account = cur.fetchone()
    if not account:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="Account not found")
    provider = account.get("provider") or "aws"

    # IMPORTANT: scoped to this account's own provider. Without the
    # mc.provider filter, editing an Azure/GCP account's metric selection
    # in Settings -> Metrics rendered AWS+Azure+GCP catalog rows all mixed
    # together (metric_catalog has no per-account provider boundary on its
    # own) -- the exact "services listed that shouldn't be there" symptom.
    cur.execute("""
        SELECT mc.id, mc.service, mc.namespace, mc.display_service, mc.metric_name,
               mc.statistic, mc.unit, mc.category, mc.description, mc.is_default,
               COALESCE(ams.enabled, 0) AS enabled,
               ams.source
        FROM metric_catalog mc
        LEFT JOIN account_metric_selections ams
               ON ams.metric_id = mc.id AND ams.aws_account_id = %s
        WHERE mc.provider = %s AND (mc.metric_name != '' OR mc.metric_name IS NULL)
        ORDER BY mc.category = 'core' DESC, mc.category = 'extended' DESC,
                 mc.display_service, mc.metric_name
    """, (account_id, provider))
    rows = cur.fetchall(); cur.close(); conn.close()

    grouped = {}
    for r in rows:
        key = r["service"]
        if key not in grouped:
            grouped[key] = {
                "service": key, "display_service": r["display_service"],
                "namespace": r["namespace"], "category": r["category"],
                "metrics": [],
            }
        if r["metric_name"]:
            grouped[key]["metrics"].append(_ser({
                "id": r["id"], "metric_name": r["metric_name"],
                "statistic": r["statistic"], "unit": r["unit"],
                "description": r["description"], "is_default": bool(r["is_default"]),
                "enabled": bool(r["enabled"]),
            }))
        else:
            grouped[key]["directory_id"] = r["id"]

    return sorted(grouped.values(), key=lambda g: (g["category"] != "core", g["category"] != "extended", g["display_service"] or ""))'''

BE_NEW = '''@router.get("/api/account-metrics/{account_id}")
def get_account_metrics(account_id: int, current_user: dict = Depends(require_permission("metrics.view"))):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id, provider FROM aws_accounts WHERE id = %s", (account_id,))
    account = cur.fetchone()
    if not account:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="Account not found")
    provider = account.get("provider") or "aws"

    # IMPORTANT: scoped to this account's own provider. Without the
    # mc.provider filter, editing an Azure/GCP account's metric selection
    # in Settings -> Metrics rendered AWS+Azure+GCP catalog rows all mixed
    # together (metric_catalog has no per-account provider boundary on its
    # own) -- the exact "services listed that shouldn't be there" symptom.
    cur.execute("""
        SELECT mc.id, mc.service, mc.namespace, mc.display_service, mc.metric_name,
               mc.statistic, mc.unit, mc.category, mc.description, mc.is_default,
               COALESCE(ams.enabled, 0) AS enabled,
               ams.source
        FROM metric_catalog mc
        LEFT JOIN account_metric_selections ams
               ON ams.metric_id = mc.id AND ams.aws_account_id = %s
        WHERE mc.provider = %s AND (mc.metric_name != '' OR mc.metric_name IS NULL)
        ORDER BY mc.category = 'core' DESC, mc.category = 'extended' DESC,
                 mc.display_service, mc.metric_name
    """, (account_id, provider))
    rows = cur.fetchall()

    # Hide CORE-tier services this account has zero matching resources
    # for -- e.g. "NLB" showing up as a selectable service in Metrics to
    # Monitor even though this account has never had a Network Load
    # Balancer, only ALBs. Both share resources.resource_type='elb' (see
    # app/collector/discovery/runner.py), distinguished only by their ARN
    # pattern (loadbalancer/app/ vs loadbalancer/net/), so a plain
    # resource_type match can't tell them apart -- this checks the ARN
    # pattern directly for that one case. Only applied to the 7 core
    # AWS services this app actually discovers resources for
    # (ec2/ebs/rds/lambda/alb/nlb/ecs); EXTENDED-tier services are left
    # alone deliberately -- this app has no discovery for them at all, so
    # "zero resources found" would be true for literally all of them,
    # and hiding the whole extended tier would remove the ability to
    # pre-select metrics before a resource is even provisioned, a
    # legitimate use of this page. See apply_metrics_to_monitor_cleanup.py.
    present_core_services = None
    if provider == "aws":
        cur.execute("""
            SELECT resource_type, resource_id FROM resources WHERE aws_account_id = %s
        """, (account_id,))
        resource_rows = cur.fetchall()
        present_core_services = set()
        for rr in resource_rows:
            rt, rid = rr["resource_type"], rr["resource_id"] or ""
            if rt == "elb":
                if "loadbalancer/app/" in rid:
                    present_core_services.add("alb")
                if "loadbalancer/net/" in rid:
                    present_core_services.add("nlb")
            elif rt in ("ecs", "ecs_service"):
                present_core_services.add("ecs")
            else:
                present_core_services.add(rt)

    cur.close(); conn.close()

    _CORE_SERVICES_WITH_DISCOVERY = {"ec2", "ebs", "rds", "lambda", "alb", "nlb", "ecs"}

    grouped = {}
    for r in rows:
        key = r["service"]
        if key not in grouped:
            grouped[key] = {
                "service": key, "display_service": r["display_service"],
                "namespace": r["namespace"], "category": r["category"],
                "metrics": [],
            }
        if r["metric_name"]:
            grouped[key]["metrics"].append(_ser({
                "id": r["id"], "metric_name": r["metric_name"],
                "statistic": r["statistic"], "unit": r["unit"],
                "description": r["description"], "is_default": bool(r["is_default"]),
                "enabled": bool(r["enabled"]),
            }))
        else:
            grouped[key]["directory_id"] = r["id"]

    if present_core_services is not None:
        # Only hide a core service group with zero matching resources if
        # NOTHING in it is already enabled -- Settings -> Metrics to
        # Monitor's Save does a full-replacement PUT of whatever this GET
        # returns, so silently dropping a group the user had already
        # explicitly turned on (even if its resources are gone now) would
        # silently disable it the next time they hit Save, without them
        # ever choosing to. Only ever hides groups nobody has touched.
        grouped = {
            key: g for key, g in grouped.items()
            if not (
                g["category"] == "core"
                and key in _CORE_SERVICES_WITH_DISCOVERY
                and key not in present_core_services
                and not any(m["enabled"] for m in g["metrics"])
            )
        }

    return sorted(grouped.values(), key=lambda g: (g["category"] != "core", g["category"] != "extended", g["display_service"] or ""))'''


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
    fe_path = os.path.join(repo_root, "frontend", "src", "pages", "Settings.jsx")
    be_path = os.path.join(repo_root, "app", "api", "metric_catalog.py")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    fe_content, fe_note = prepare_patch(
        fe_path, "frontend/src/pages/Settings.jsx",
        [(FE_BUTTONS_OLD, FE_BUTTONS_NEW), (FE_HELP_OLD, FE_HELP_NEW)],
        "YACE config.yml download buttons REMOVED entirely",
    )
    be_content, be_note = prepare_patch(
        be_path, "app/api/metric_catalog.py",
        [(BE_OLD, BE_NEW)],
        "_CORE_SERVICES_WITH_DISCOVERY",
    )

    print(f"\nFile patch plan:\n  {fe_note}\n  {be_note}")

    if fe_content is None and be_content is None:
        print("\nNothing to do.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    if fe_content is not None:
        backup(fe_path)
        with open(fe_path, "w", encoding="utf-8") as fh:
            fh.write(fe_content)
        print("Patched frontend/src/pages/Settings.jsx")
    if be_content is not None:
        backup(be_path)
        with open(be_path, "w", encoding="utf-8") as fh:
            fh.write(be_content)
        print("Patched app/api/metric_catalog.py")

    print("""
[Manual follow-up]

  A) Rebuild the frontend -- REQUIRED:
       cd frontend
       npm install
       npm run build
       cd ..

  B) Restart:
       sudo systemctl restart monitoring-hub

  C) Open Settings -> Metrics to Monitor for AuroGov Mumbai -- the
     Critical/Standard/Trend download buttons should be gone entirely,
     and NLB should no longer appear as a selectable service (ALB
     should still be there, since real ALBs exist).

  D) Review, commit, push:
       git diff frontend/src/pages/Settings.jsx app/api/metric_catalog.py
       git add frontend/src/pages/Settings.jsx app/api/metric_catalog.py apply_metrics_to_monitor_cleanup.py
       git commit -m "fix(ui): remove legacy YACE download buttons entirely (explicit request), hide core AWS services like NLB from Metrics to Monitor when the account has zero matching discovered resources"
       git push origin main
""")


if __name__ == "__main__":
    main()
