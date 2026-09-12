#!/usr/bin/env python3
"""
apply_fix_stale_yace_documentation.py
========================================
Fixes a real, actively-misleading documentation bug spotted directly in
a screenshot: the "Metrics to Monitor" page's help text for AWS still
describes deploying separate YACE instances with different
--scraping-interval flags as "what actually saves GetMetricData cost",
and claims "Nothing is pushed automatically" -- both statements are
FALSE as of this session's Phase 1
(apply_direct_gmd_metrics_revival.py), which replaced AWS's entire
YACE/VictoriaMetrics collection path with app/collector/scheduler.py's
own built-in Critical/Standard/Low tiers, calling GetMetricData
directly on a schedule, automatically, with zero external deployment.

This is not just stale wording -- following this UI's own instructions
today (download the config.yml files, deploy 3 separate YACE
instances) would accomplish NOTHING for this app: metrics_vm_sync.py's
sync_metrics_from_vm() has been a permanent no-op for all three
providers since Phase 3 (apply_gcp_direct_metrics_fetch.py), so
whatever YACE pushed into VictoriaMetrics would never make it back into
this app's own `metrics`/`metric_history` tables, alerting, or charts.
A user following this advice would waste real effort standing up
infrastructure that has zero effect on anything this app does.

THE FIX
---------
Rewrites the AWS help paragraph to accurately describe the current
architecture (scheduler.py's built-in tiers, no deployment needed,
that's what actually controls cost/frequency today), and reframes the
three config.yml download buttons and their tooltips as a LEGACY
export -- kept only for anyone who might still want a YACE-format
config for a genuinely separate, external monitoring stack (e.g.
feeding their own Grafana instance), not as this app's own mechanism.
Also updates the backend endpoint's docstring/parameter description in
app/api/metric_catalog.py for the same reason -- it referenced "fix #2
in the cost-optimization plan" as if still current.

The feature itself is NOT removed -- deliberately conservative, since
there may be a genuine reason someone still wants a YACE-format export
for external use that this script can't rule out. Only the FALSE
claims about what it does for THIS app are corrected.

TESTED: ran the actual frontend build (`npm run build`) after applying
-- compiles clean. Backend file confirmed with py_compile.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_stale_yace_documentation.py --dry-run
    python3 apply_fix_stale_yace_documentation.py --apply
    cd frontend && npm install && npm run build && cd ..
(the frontend rebuild is required -- this changes .jsx source, not the
built dist/ the server actually serves)
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

FRONTEND_COMMENT_OLD = '''            {/* YACE is AWS/CloudWatch-specific -- Azure/GCP use this app's own
                push collectors instead (see app/providers/{azure,gcp}/
                metrics_collector.py), so these downloads are meaningless
                for them. Show for AWS or while nothing is selected yet
                (matches the existing disabled-until-selected behavior). */}
            {(!selectedAccount || selectedAccount.provider === "aws") && (
              <>
                <button className="btn-clear" onClick={() => handleDownloadYaceConfig("critical")} disabled={!accountId} title="60s poll — run as its own YACE instance">
                  <DownloadIcon size={13}/> Critical (60s)
                </button>
                <button className="btn-clear" onClick={() => handleDownloadYaceConfig("standard")} disabled={!accountId} title="300s poll — run as its own YACE instance">
                  <DownloadIcon size={13}/> Standard (300s)
                </button>
                <button className="btn-clear" onClick={() => handleDownloadYaceConfig("trend")} disabled={!accountId} title="900s poll — run as its own YACE instance">
                  <DownloadIcon size={13}/> Trend (900s)
                </button>
              </>
'''

FRONTEND_COMMENT_NEW = '''            {/* YACE is AWS/CloudWatch-specific -- Azure/GCP use this app's own
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
'''

FRONTEND_PARAGRAPH_OLD = '''            {(!selectedAccount || selectedAccount.provider === "aws") ? (
              <p style={{ fontSize: 11, color: "var(--text-muted)", margin: "0 0 10px 0" }}>
                Each tier button generates a separate config.yml for that polling speed — deploy all three as
                separate YACE instances on this account/region's monitoring server (Critical/60s, Standard/300s,
                Trend/900s), each started with the matching <code>--scraping-interval</code> flag. This
                is what actually saves GetMetricData cost: one YACE process only has one global scrape interval,
                so splitting by tier is required for tiering to affect AWS call volume, not just query windows.
                Nothing is pushed automatically.
              </p>
            ) : ('''

FRONTEND_PARAGRAPH_NEW = '''            {(!selectedAccount || selectedAccount.provider === "aws") ? (
              <p style={{ fontSize: 11, color: "var(--text-muted)", margin: "0 0 10px 0" }}>
                AWS metrics for this account are collected automatically by
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
              </p>
            ) : ('''

BACKEND_OLD = '''    tier: str = Query(
        None,
        description="Optional: 'critical' | 'standard' | 'trend'. Omit to "
                     "get every enabled metric in one file (old behavior, "
                     "back-compat). Pass a tier to get just that tier's "
                     "jobs, for deploying as one of the 3 separate YACE "
                     "instances (fix #2 in the cost-optimization plan).",
    ),
):
    """
    Builds a ready-to-use YACE (yet-another-cloudwatch-exporter) discovery
    config.yml from this account's enabled metric selection.
'''

BACKEND_NEW = '''    tier: str = Query(
        None,
        description="Optional: 'critical' | 'standard' | 'trend'. Omit to "
                     "get every enabled metric in one file (old behavior, "
                     "back-compat). Pass a tier to get just that tier's "
                     "jobs. LEGACY: AWS's own cost-tiering is now handled "
                     "automatically by app/collector/scheduler.py's "
                     "built-in intervals (see apply_direct_gmd_metrics_revival.py) "
                     "-- this export exists only for anyone still running "
                     "a separate external YACE + VictoriaMetrics stack.",
    ),
):
    """
    Builds a ready-to-use YACE (yet-another-cloudwatch-exporter) discovery
    config.yml from this account's enabled metric selection.

    LEGACY: this app's own AWS metric collection no longer uses YACE or
    VictoriaMetrics at all (see apply_direct_gmd_metrics_revival.py) --
    app/collector/scheduler.py's Critical/Standard/Low tiers call
    GetMetricData directly on their own schedule, automatically, with no
    deployment required. This endpoint is kept only as an export for
    anyone who wants a YACE-format config for their own SEPARATE
    external monitoring stack; deploying it has no effect on this app's
    own collection, cost, or alerting.
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
    settings_path = os.path.join(repo_root, "frontend", "src", "pages", "Settings.jsx")
    backend_path = os.path.join(repo_root, "app", "api", "metric_catalog.py")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    settings_content, settings_note = prepare_patch(
        settings_path, "frontend/src/pages/Settings.jsx",
        [(FRONTEND_COMMENT_OLD, FRONTEND_COMMENT_NEW), (FRONTEND_PARAGRAPH_OLD, FRONTEND_PARAGRAPH_NEW)],
        "LEGACY EXPORT, not this app's own collection mechanism",
    )
    backend_content, backend_note = prepare_patch(
        backend_path, "app/api/metric_catalog.py",
        [(BACKEND_OLD, BACKEND_NEW)],
        "LEGACY: this app's own AWS metric collection no longer uses YACE",
    )

    print(f"\nFile patch plan:\n  {settings_note}\n  {backend_note}")

    if settings_content is None and backend_content is None:
        print("\nNothing to do.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    if settings_content is not None:
        backup(settings_path)
        with open(settings_path, "w", encoding="utf-8") as fh:
            fh.write(settings_content)
        print("Patched frontend/src/pages/Settings.jsx")
    if backend_content is not None:
        backup(backend_path)
        with open(backend_path, "w", encoding="utf-8") as fh:
            fh.write(backend_content)
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

  C) Open Settings -> Metrics to Monitor for an AWS account -- the help
     text should now accurately describe scheduler.py's automatic
     tiering, and the three download buttons should say "Legacy YACE
     config export" on hover instead of "run as its own YACE instance".

  D) Review, commit, push:
       git diff frontend/src/pages/Settings.jsx app/api/metric_catalog.py
       git add frontend/src/pages/Settings.jsx app/api/metric_catalog.py apply_fix_stale_yace_documentation.py
       git commit -m "fix(docs): AWS metric-tiering help text and YACE config export still described the pre-Phase-1 architecture as current, including a false 'this is what saves GetMetricData cost' claim -- scheduler.py has handled this automatically since Phase 1. Feature kept as an explicit legacy export for external use, not removed."
       git push origin main
""")


if __name__ == "__main__":
    main()
