#!/usr/bin/env python3
"""
fix_overview_aws_branding.py
=================================
Monitoring Hub -- frontend audit finding: the dashboard homepage's main
tagline and primary section header both say "AWS" specifically, even
though the underlying data (groupByAccount, filteredGroups) is already
fully provider-agnostic -- confirmed by reading the grouping logic
itself, which has no provider filter at all.

This is the single most visible page in the entire app -- the first
thing anyone sees after logging in -- stating "Live AWS infrastructure
monitoring" and "AWS Accounts (N)" as a section header, for a product
whose own repo is named monitoring-hub-multi-cloud and which (as of this
session's audit) genuinely monitors, alerts on, and discovers resources
across Azure and GCP too. This is stale branding text left over from
before multi-cloud support existed, not a functional restriction --
purely cosmetic, but on the page every single user sees first.

FIX
---
Two text-only changes, no logic touched:
  1. Page subtitle: "Live AWS infrastructure monitoring across all
     accounts" -> "Live infrastructure monitoring across all accounts
     and clouds"
  2. Section header: "AWS Accounts" -> "Accounts" (the (N) count next to
     it already reflects every provider, so the label should too)

TESTED: mechanical text replacement verified against the exact current
file content; no JSX structure changed, so no parser check needed beyond
the exact-match guard already built into this script.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_overview_aws_branding.py --dry-run
    python3 fix_overview_aws_branding.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

OLD_SUBTITLE = '''            Live AWS infrastructure monitoring across all accounts'''
NEW_SUBTITLE = '''            Live infrastructure monitoring across all accounts and clouds'''

OLD_HEADER = '''        <h2 style={{ fontSize: 17, fontWeight: 700 }}>
          AWS Accounts
          <span style={{ fontWeight: 400, fontSize: 13, color: "var(--text-muted)", marginLeft: 8 }}>
            ({filteredGroups.length})
          </span>
        </h2>'''
NEW_HEADER = '''        <h2 style={{ fontSize: 17, fontWeight: 700 }}>
          Accounts
          <span style={{ fontWeight: 400, fontSize: 13, color: "var(--text-muted)", marginLeft: 8 }}>
            ({filteredGroups.length})
          </span>
        </h2>'''


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

    path = os.path.join(repo_root, "frontend", "src", "pages", "Overview.jsx")
    if not os.path.exists(path):
        die(f"frontend/src/pages/Overview.jsx not found at {path}.")

    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    if "and clouds" in content and ">\n          Accounts\n" in content:
        print("Overview.jsx already has this fix -- nothing to do.")
        return

    for old, label in [(OLD_SUBTITLE, "page subtitle"), (OLD_HEADER, "section header")]:
        n = content.count(old)
        if n != 1:
            die(f"{label}: expected exactly 1 match, found {n}. "
                f"File may differ from what this script expects.")

    new_content = content.replace(OLD_SUBTITLE, NEW_SUBTITLE, 1)
    new_content = new_content.replace(OLD_HEADER, NEW_HEADER, 1)

    print(f"\nPatch matched expected content exactly: "
          f"frontend/src/pages/Overview.jsx ({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("Patched frontend/src/pages/Overview.jsx")

    print("""
[Manual follow-up]

  A) Frontend only -- rebuild and redeploy:
       cd /opt/monitoring-hub/app/frontend
       sudo -u hcsadmin npm run build
       cd /opt/monitoring-hub/app

  B) No backend restart needed -- text-only change.

  C) Verify: load the dashboard homepage, confirm the subtitle and
     section header no longer say "AWS" and the account count next to
     "Accounts" is unchanged (still every account, every provider).

  D) Review, commit, push:
       git status
       git diff frontend/src/pages/Overview.jsx
       git add frontend/src/pages/Overview.jsx fix_overview_aws_branding.py
       git commit -m "fix(frontend): dashboard homepage said 'AWS' in its main tagline/header despite being fully multi-cloud underneath"
       git push origin main
""")


if __name__ == "__main__":
    main()
