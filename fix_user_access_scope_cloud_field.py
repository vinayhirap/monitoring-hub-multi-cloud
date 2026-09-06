#!/usr/bin/env python3
"""
fix_user_access_scope_cloud_field.py
=========================================
Monitoring Hub -- frontend audit finding: a real, silent access-control
bug, not just a wording issue.

BUG
---
UserManagement.jsx's "Add User" modal lets an admin grant a viewer/editor
scoped access to specific accounts via a multi-select that lists every
account this app knows about -- AWS, Azure, and GCP alike, with no
provider shown per option. But the scope payload sent to the backend
hardcodes `cloud: "aws"` for every selection, regardless of which
provider the chosen account actually belongs to:

    scopes: form.accountIds.map(id => ({ cloud: "aws", account_ref_id: Number(id) }))

The backend takes `cloud` seriously -- app/auth/authorization.py's
validate_scope_shape() checks the account_ref_id against a per-cloud set
of valid IDs (valid_account_ids_by_cloud[cloud]), and get_accessible_
account_ids() matches scopes by cloud too. So picking an Azure or GCP
account here sends a scope claiming it's an AWS account with that
account's numeric ID -- which either fails validate_scope_shape's check
(account_ref_id not found under "aws") or, worse, could coincidentally
validate against an unrelated AWS account that happens to share the same
numeric ID. Either way, the admin's intent (scope this user to that
specific Azure/GCP account) silently does not happen -- and the POST is
wrapped in `.catch(() => {})`, so no error ever surfaces. An admin can
believe they've restricted a viewer to one Azure account and have
actually granted nothing, or granted access to an unrelated AWS account.

FIX
---
1. Send the account's REAL provider as `cloud`, looked up from the same
   `accounts` list already loaded for the picker, instead of a hardcoded
   string.
2. Show each account's provider in the picker option text (e.g. "Contoso
   Prod (azure)") so an admin can actually see what they're selecting --
   previously indistinguishable from an AWS account by name alone.

Deliberately NOT changing the `.catch(() => {})` silent-failure pattern
here -- that's a separate, broader "errors are swallowed all over this
modal" issue, not specific to the cloud-field bug, and worth its own
targeted look rather than bundling in as a side effect of this fix.

TESTED: the corrected mapping logic exercised against a representative
`accounts` array (mixed aws/azure/gcp), confirming each selected ID
resolves to its real provider and that a missing/unknown account ID
falls back to "aws" without throwing (same fail-safe default the rest of
this codebase uses for a missing provider column). Not tested: the
actual POST round-trip to a running backend -- verify by adding a scoped
viewer/editor against a real Azure or GCP account and confirming
get_accessible_account_ids actually includes it afterward.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_user_access_scope_cloud_field.py --dry-run
    python3 fix_user_access_scope_cloud_field.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

OLD_SCOPES = '''            scopes: form.accountIds.map(id => ({ cloud: "aws", account_ref_id: Number(id) })),'''

NEW_SCOPES = '''            // Previously hardcoded cloud: "aws" for every selection regardless
            // of the account's real provider -- the backend's
            // validate_scope_shape() checks account_ref_id against a
            // per-cloud valid-ID set, so an Azure/GCP account picked here
            // either failed validation silently (.catch(() => {}) below
            // swallows it) or, worse, could validate against an unrelated
            // AWS account sharing the same numeric ID. Look up the real
            // provider from the same `accounts` list the picker itself
            // renders from.
            scopes: form.accountIds.map(id => ({
              cloud: accounts.find(a => a.id === Number(id))?.provider || "aws",
              account_ref_id: Number(id),
            })),'''

OLD_OPTION = '''                    {accounts.map(a => (
                      <option key={a.id} value={a.id}>{a.account_name}</option>
                    ))}'''

NEW_OPTION = '''                    {accounts.map(a => (
                      <option key={a.id} value={a.id}>
                        {a.account_name} ({a.provider || "aws"})
                      </option>
                    ))}'''


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

    path = os.path.join(repo_root, "frontend", "src", "pages", "UserManagement.jsx")
    if not os.path.exists(path):
        die(f"frontend/src/pages/UserManagement.jsx not found at {path}.")

    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    if "Previously hardcoded cloud: \"aws\" for every selection" in content:
        print("UserManagement.jsx already has this fix -- nothing to do.")
        return

    for old, label in [(OLD_SCOPES, "scope cloud field"), (OLD_OPTION, "account picker option label")]:
        n = content.count(old)
        if n != 1:
            die(f"{label}: expected exactly 1 match, found {n}. "
                f"File may differ from what this script expects.")

    new_content = content.replace(OLD_SCOPES, NEW_SCOPES, 1)
    new_content = new_content.replace(OLD_OPTION, NEW_OPTION, 1)

    print(f"\nPatch matched expected content exactly: "
          f"frontend/src/pages/UserManagement.jsx ({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("Patched frontend/src/pages/UserManagement.jsx")

    print("""
[Manual follow-up]

  A) Frontend only -- rebuild and redeploy:
       cd /opt/monitoring-hub/app/frontend
       sudo -u hcsadmin npm run build
       cd /opt/monitoring-hub/app

  B) No backend restart needed -- pure frontend fix, the backend's
     validate_scope_shape()/get_accessible_account_ids() were already
     correct, they just never received an accurate `cloud` value from
     this one call site.

  C) Real verification: add a viewer or editor, scope them to an Azure
     or GCP account (now visibly labeled with its provider in the
     picker), save, then check that user's actual access -- either by
     logging in as them or checking `access_scopes` in the DB directly
     (SELECT * FROM access_scopes WHERE user_id = <new_user_id>) --
     confirm `cloud` matches the real provider, not "aws".

  D) Review, commit, push:
       git status
       git diff frontend/src/pages/UserManagement.jsx
       git add frontend/src/pages/UserManagement.jsx fix_user_access_scope_cloud_field.py
       git commit -m "fix(rbac): user access-scope grants hardcoded cloud=aws regardless of the account's actual provider"
       git push origin main
""")


if __name__ == "__main__":
    main()
