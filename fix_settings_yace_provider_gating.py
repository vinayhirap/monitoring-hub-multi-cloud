#!/usr/bin/env python3
"""
fix_settings_yace_provider_gating.py
=========================================
Monitoring Hub -- frontend audit finding: Settings page's "Download YACE
Config" buttons and their explanatory text render unconditionally for
every account, regardless of provider.

BUG
---
YACE (yet-another-cloudwatch-exporter) is an AWS/CloudWatch-specific
scraper -- Azure and GCP use a completely different collection path in
this app (the Python-native push collectors in
app/providers/{azure,gcp}/metrics_collector.py, confirmed in this
audit's earlier phases; no YACE involved at all). But Settings.jsx's
"Critical (60s) / Standard (300s) / Trend (900s)" download buttons, and
the paragraph explaining "this is what actually saves GetMetricData
cost... splitting by tier is required for tiering to affect AWS call
volume," show for every selected account with no provider check.

Clicking one of these buttons for an Azure or GCP account calls the
existing generate_yace_config backend endpoint, which has no provider
filter either -- it would produce a YAML file listing that account's
enabled metrics using their real metric_catalog namespace values, e.g.
"Microsoft.Compute/virtualMachines" or "compute.googleapis.com/instance"
dressed up as YACE discovery jobs. YACE only understands CloudWatch
namespaces, so this file would be useless if actually deployed -- and
the on-screen text about "AWS call volume" and "GetMetricData cost" is
simply wrong for a cloud that was never billed that way to begin with
(confirmed in this audit's provider-consistency work: Azure/GCP platform
metric reads are free, no per-call billing to avoid).

FIX
---
Both the three download buttons and the explanatory paragraph now check
the selected account's provider (reusing the `selectedAccount` variable
already computed earlier in this file). AWS accounts (or no account
selected yet, matching the existing disabled-by-default state) see
exactly what they saw before -- zero behavior change. Azure/GCP accounts
see neither the buttons nor the AWS-specific paragraph; instead, a short
accurate note explains that their metrics are collected automatically on
a fixed interval with nothing to download or deploy.

TESTED: mechanical conditional-wrapping verified against the exact
current file content -- the AWS branch is a byte-for-byte copy of the
previously-unconditional content, so there is no behavior change for
AWS. Visual verification in the browser (select an AWS account, confirm
identical to before; select an Azure/GCP account, confirm buttons are
gone and the new note appears) is the real test.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_settings_yace_provider_gating.py --dry-run
    python3 fix_settings_yace_provider_gating.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

OLD_BUTTONS = '''            <button className="btn-clear" onClick={() => handleDownloadYaceConfig("critical")} disabled={!accountId} title="60s poll — run as its own YACE instance">
              <DownloadIcon size={13}/> Critical (60s)
            </button>
            <button className="btn-clear" onClick={() => handleDownloadYaceConfig("standard")} disabled={!accountId} title="300s poll — run as its own YACE instance">
              <DownloadIcon size={13}/> Standard (300s)
            </button>
            <button className="btn-clear" onClick={() => handleDownloadYaceConfig("trend")} disabled={!accountId} title="900s poll — run as its own YACE instance">
              <DownloadIcon size={13}/> Trend (900s)
            </button>'''

NEW_BUTTONS = '''            {/* YACE is AWS/CloudWatch-specific -- Azure/GCP use this app's own
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
            )}'''

OLD_PARAGRAPH = '''            <p style={{ fontSize: 11, color: "var(--text-muted)", margin: "0 0 10px 0" }}>
              Each tier button generates a separate config.yml for that polling speed — deploy all three as
              separate YACE instances on this account/region's monitoring server (Critical/60s, Standard/300s,
              Trend/900s), each started with the matching <code>--scraping-interval</code> flag. This
              is what actually saves GetMetricData cost: one YACE process only has one global scrape interval,
              so splitting by tier is required for tiering to affect AWS call volume, not just query windows.
              Nothing is pushed automatically.
            </p>'''

NEW_PARAGRAPH = '''            {(!selectedAccount || selectedAccount.provider === "aws") ? (
              <p style={{ fontSize: 11, color: "var(--text-muted)", margin: "0 0 10px 0" }}>
                Each tier button generates a separate config.yml for that polling speed — deploy all three as
                separate YACE instances on this account/region's monitoring server (Critical/60s, Standard/300s,
                Trend/900s), each started with the matching <code>--scraping-interval</code> flag. This
                is what actually saves GetMetricData cost: one YACE process only has one global scrape interval,
                so splitting by tier is required for tiering to affect AWS call volume, not just query windows.
                Nothing is pushed automatically.
              </p>
            ) : (
              <p style={{ fontSize: 11, color: "var(--text-muted)", margin: "0 0 10px 0" }}>
                {selectedAccount.provider === "azure" ? "Azure" : "GCP"} metrics for this account are collected
                automatically every 5 minutes by this app's built-in collector — there's no separate config
                to download or deploy, and no per-call cost to tier around (platform metric reads are free
                on this provider).
              </p>
            )}'''


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
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply_ = args.apply and not args.dry_run

    repo_root = find_repo_root()
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    path = os.path.join(repo_root, "frontend", "src", "pages", "Settings.jsx")
    if not os.path.exists(path):
        die(f"frontend/src/pages/Settings.jsx not found at {path}.")

    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    if "YACE is AWS/CloudWatch-specific" in content:
        print("Settings.jsx already has this fix -- nothing to do.")
        return

    for old, label in [(OLD_BUTTONS, "YACE download buttons"), (OLD_PARAGRAPH, "YACE explanatory paragraph")]:
        n = content.count(old)
        if n != 1:
            die(f"{label}: expected exactly 1 match, found {n}. "
                f"File may differ from what this script expects.")

    new_content = content.replace(OLD_BUTTONS, NEW_BUTTONS, 1)
    new_content = new_content.replace(OLD_PARAGRAPH, NEW_PARAGRAPH, 1)

    print(f"\nPatch matched expected content exactly: "
          f"frontend/src/pages/Settings.jsx ({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("Patched frontend/src/pages/Settings.jsx")

    print("""
[Manual follow-up]

  A) Frontend only -- rebuild and redeploy:
       cd /opt/monitoring-hub/app/frontend
       sudo -u hcsadmin npm run build
       cd /opt/monitoring-hub/app

  B) No backend restart needed -- pure frontend conditional, the backend
     generate_yace_config endpoint is untouched (it's simply no longer
     linked to for non-AWS accounts from this page).

  C) Verify: select an AWS account in Settings -> Metrics -- should look
     and behave EXACTLY as before (buttons present, same text). Select
     an Azure or GCP account -- the three download buttons should be
     gone, replaced by a short note about automatic collection.

  D) Review, commit, push:
       git status
       git diff frontend/src/pages/Settings.jsx
       git add frontend/src/pages/Settings.jsx fix_settings_yace_provider_gating.py
       git commit -m "fix(frontend): hide AWS/YACE-specific config downloads for Azure/GCP accounts, which use a different collection path entirely"
       git push origin main
""")


if __name__ == "__main__":
    main()
