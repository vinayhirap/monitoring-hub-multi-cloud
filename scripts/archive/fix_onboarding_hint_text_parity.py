#!/usr/bin/env python3
"""
fix_onboarding_hint_text_parity.py
=======================================
Monitoring Hub -- small follow-up to the earlier onboarding auto-detect
parity fix (commit 5f67ee0), caught by continuing the frontend audit
after that fix shipped.

WHAT WAS MISSED
----------------
fix_onboarding_autodetect_parity.py correctly widened the SUBMIT-time
useAutoDetect condition to all three providers, and correctly populated
detectedServices from the Test Connection response for Azure/GCP too.
But a separate piece of UI in the same file -- the "METRICS TO MONITOR"
section's explanatory hint text -- still gates on `provider === "aws"`
in two places:

  1. Whether to show "Detected N services... will be enabled
     automatically" vs the generic "cost-optimized defaults are
     pre-selected" text.
  2. Whether to show the "Run Test Connection first to auto-detect"
     hint at all.

Net effect: for Azure/GCP, detectedServices IS now populated correctly
after Test Connection (per the earlier fix), and submitting WILL
correctly auto-detect (useAutoDetect no longer checks provider) -- but
the on-screen text never reflects any of that. An Azure/GCP user who
successfully tests their connection and has services detected still
sees "Recommended cost-optimized defaults are pre-selected," which is
now actively misleading: it describes the opposite of what submission
will actually do.

FIX
---
Removes the `provider === "aws" &&` gate from both conditions. The hint
text itself was already provider-neutral wording ("this account/region",
not "this AWS account"), so no wording changes needed -- just stop
hiding accurate, already-correct text from two-thirds of users.

TESTED: mechanical, single-condition removal verified against the exact
current file content; the surrounding JSX structure is unchanged (same
ternary shape, same two branches, same nested hint). Visual verification
in the browser is the real test here, per the earlier onboarding fix's
own follow-up note in this project's log.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_onboarding_hint_text_parity.py --dry-run
    python3 fix_onboarding_hint_text_parity.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

OLD_BLOCK = '''            {provider === "aws" && detectedServices.length > 0 ? (
              <p className="ob-metrics-hint">
                Detected {detectedServices.length} service{detectedServices.length !== 1 ? "s" : ""} in this
                account/region: <strong>{detectedServices.join(", ")}</strong>. Their default metrics will be
                enabled automatically on submit — no need to pick anything below unless you want to add,
                remove, or fine-tune the selection now.
              </p>
            ) : (
              <p className="ob-metrics-hint">
                Recommended cost-optimized defaults are pre-selected. Add or remove any
                metric now, or come back later from Settings → Metrics for this account.
                {provider === "aws" && (
                  <> Run "Test Connection" above first to auto-detect what's actually in this
                  account instead of picking manually.</>
                )}
              </p>
            )}'''

NEW_BLOCK = '''            {detectedServices.length > 0 ? (
              <p className="ob-metrics-hint">
                Detected {detectedServices.length} service{detectedServices.length !== 1 ? "s" : ""} in this
                account/region: <strong>{detectedServices.join(", ")}</strong>. Their default metrics will be
                enabled automatically on submit — no need to pick anything below unless you want to add,
                remove, or fine-tune the selection now.
              </p>
            ) : (
              <p className="ob-metrics-hint">
                Recommended cost-optimized defaults are pre-selected. Add or remove any
                metric now, or come back later from Settings → Metrics for this account.
                {/* Previously AWS-only -- Test Connection now detects real
                    services for Azure/GCP too (see fix_onboarding_autodetect_parity.py),
                    so this hint applies to all three providers. */}
                <> Run "Test Connection" above first to auto-detect what's actually in this
                account instead of picking manually.</>
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

    path = os.path.join(repo_root, "frontend", "src", "pages", "AccountOnboarding.jsx")
    if not os.path.exists(path):
        die(f"frontend/src/pages/AccountOnboarding.jsx not found at {path}.")

    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    if "Previously AWS-only -- Test Connection now detects real" in content:
        print("AccountOnboarding.jsx already has this fix -- nothing to do.")
        return

    n = content.count(OLD_BLOCK)
    if n != 1:
        die(f"Expected exactly 1 match for the current hint-text block, found {n}. "
            f"File may differ from what this script expects.")

    new_content = content.replace(OLD_BLOCK, NEW_BLOCK, 1)

    print(f"\nPatch matched expected content exactly: "
          f"frontend/src/pages/AccountOnboarding.jsx ({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("Patched frontend/src/pages/AccountOnboarding.jsx")

    print("""
[Manual follow-up]

  A) Frontend only -- rebuild and redeploy:
       cd /opt/monitoring-hub/app/frontend
       sudo -u hcsadmin npm run build
       cd /opt/monitoring-hub/app

  B) No backend restart needed -- this is a pure frontend text/condition
     change.

  C) Verify: start adding a new Azure or GCP account, fill in real
     credentials, click "Test Connection." If services were detected,
     the "Metrics to Monitor" section should now say "Detected N
     service(s)..." instead of the generic defaults text. If nothing was
     detected, it should now also show the "Run Test Connection above
     first..." hint, matching AWS's onboarding flow exactly.

  D) Review, commit, push:
       git status
       git diff frontend/src/pages/AccountOnboarding.jsx
       git add frontend/src/pages/AccountOnboarding.jsx fix_onboarding_hint_text_parity.py
       git commit -m "fix(onboarding): metrics-hint text was still AWS-only despite the underlying auto-detect already working for all providers"
       git push origin main
""")


if __name__ == "__main__":
    main()
