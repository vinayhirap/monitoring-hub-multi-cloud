#!/usr/bin/env python3
"""
apply_fix_upsert_threshold_nameerror.py
========================================
URGENT regression fix. apply_fix_threshold_resource_type_everywhere.py
(the previous script) removed settings.py's local
_normalize_threshold_resource_type() function (moving its logic to the
shared app/threshold_defaults.py module, imported as
normalize_threshold_resource_type, no underscore) -- but missed updating
TWO call sites that still referenced the old, now-deleted underscore-
prefixed name:

  - upsert_threshold() (POST /api/settings/thresholds) -- the endpoint
    behind EVERY "Save" button on the Metric Thresholds page. This one
    is user-reported: clicking Save on ANY threshold card (not just
    BurstBalance -- every single one) raised NameError, returned as a
    500 Internal Server Error.
  - seed_default_thresholds() (POST /api/settings/thresholds/seed) --
    would raise the same NameError the first time an account with zero
    existing thresholds loads the Settings page (the frontend
    auto-triggers this seed call when the thresholds list is empty).

This is a real regression in previously-shipped work, not a
pre-existing bug -- confirmed by reproducing the exact NameError
against the broken code and confirming this fix resolves it, using the
actual upsert_threshold() function (not a reimplementation). Apologies
for shipping this without testing the specific function that broke --
earlier testing covered get_thresholds() and
_sync_thresholds_for_selection() but not upsert_threshold() itself,
which is why this slipped through.

THE FIX
---------
Both call sites now use normalize_threshold_resource_type (the shared,
correctly-imported name), matching the rest of the file.

TESTED: reproduced the exact NameError against the broken code (using
the real upsert_threshold() function, not a mock of it), confirmed the
fix resolves it and returns the expected {"status": "saved", "id": ...}
result for the exact BurstBalance-save scenario reported.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_upsert_threshold_nameerror.py --dry-run
    python3 apply_fix_upsert_threshold_nameerror.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

OLD_1 = '    resource_type  = _normalize_threshold_resource_type(payload.get("resource_type", "ec2"))'
NEW_1 = '    resource_type  = normalize_threshold_resource_type(payload.get("resource_type", "ec2"))'

OLD_2 = '            """, (account_id, _normalize_threshold_resource_type(m["service"]), m["id"], warn, crit, comp))'
NEW_2 = '            """, (account_id, normalize_threshold_resource_type(m["service"]), m["id"], warn, crit, comp))'


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
    path = os.path.join(repo_root, "app", "api", "settings.py")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    with open(path) as f:
        content = f.read()

    remaining = content.count("_normalize_threshold_resource_type(")
    if remaining == 0:
        print("\nAlready fixed -- no leftover references to the old name found. Nothing to do.")
        return

    print(f"\nFound {remaining} leftover reference(s) to the undefined _normalize_threshold_resource_type.")
    n1, n2 = content.count(OLD_1), content.count(OLD_2)
    if n1 not in (0, 1) or n2 not in (0, 1):
        die(f"Unexpected match counts (upsert_threshold: {n1}, seed_default_thresholds: {n2}) "
            f"-- file may differ from what this script expects.")

    new_content = content
    if n1:
        new_content = new_content.replace(OLD_1, NEW_1, 1)
    if n2:
        new_content = new_content.replace(OLD_2, NEW_2, 1)

    still_remaining = new_content.count("_normalize_threshold_resource_type(")
    if still_remaining:
        die(f"{still_remaining} reference(s) to the old name still remain after patching -- "
            f"this script's two known call sites don't cover everything. Investigate manually before proceeding.")

    print(f"\nFile patch plan:\n  app/api/settings.py: OK ({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w") as f:
        f.write(new_content)
    print("Patched app/api/settings.py")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub

  B) THE REAL TEST: go to Settings -> Metric Thresholds and click Save
     on the BurstBalance card (or any card) -- should now show a
     success state, not "Failed". Check the browser console -- no more
     500 on POST /api/settings/thresholds.

  C) Review, commit, push:
       git diff app/api/settings.py
       git add app/api/settings.py apply_fix_upsert_threshold_nameerror.py
       git commit -m "fix(urgent): regression in the previous threshold_resource_type fix -- upsert_threshold and seed_default_thresholds still called the deleted underscore-prefixed function name, breaking every threshold Save with a 500. Fixed and tested against the actual function this time."
       git push origin main
""")


if __name__ == "__main__":
    main()
