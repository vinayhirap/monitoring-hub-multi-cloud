#!/usr/bin/env python3
"""
apply_harden_group_policy_scope_check.py
========================================
Defense-in-depth addition, requested explicitly after discussion --
NOT a fix for a live, currently-exploitable bug.

WHAT WAS FOUND, AND WHY IT'S NOT A LIVE VULNERABILITY TODAY
------------------------------------------------------------------
app/api/admin/groups.py's add_group_policy() (POST /{group_id}/policies)
validates a requested scope's SHAPE (valid account IDs, well-formed
JSON) but never checked whether the GRANTING USER actually has that
scope themselves -- unlike app/api/admin/users.py's equivalent
_validate_and_insert_scopes(), which does call authz.scope_within() for
exactly this reason.

Investigated before concluding anything: this module's own docstring
explicitly documents that group policy grants are admin-only BY DESIGN
("treated like the AWS Organizations management account -- one root of
authority, not delegated"), and confirmed against
db/migrations/015_permissions_rbac.sql that role_permissions only ever
grants groups.update to admin, never editor/viewer. Since admin's
effective scope is always FULL_ACCESS (which scope_within already
treats as covering anything), the missing check is currently a no-op
in practice -- not an accidental gap, and not exploitable today.

THE ADDITION
--------------
Adds the check anyway, as a second, independent layer: if
groups.update is ever granted to a non-admin role in the future (a
permission-catalog change, not a code change), or if has_permission()
ever had its own bug, this endpoint would otherwise have zero secondary
defense. app/api/admin/users.py already treats this as important enough
to check independently of the permission gate ("never trust anything
the client sent about its own permissions") -- this brings
add_group_policy() to the same standard. For the current admin-only
reality, this change is a no-op (admin's FULL_ACCESS scope always
passes scope_within).

TESTED: exercised against the REAL app/auth/authorization.py module
(not a mock of it -- the actual scope_within/get_effective_scope logic
this depends on), three scenarios: (1) a non-admin actor requesting a
scope WIDER than their own effective scope is correctly blocked with
403, (2) admin is completely unaffected and can still grant anything,
(3) a non-admin actor requesting a scope WITHIN their own effective
scope still succeeds normally.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_harden_group_policy_scope_check.py --dry-run
    python3 apply_harden_group_policy_scope_check.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

OLD = '''    valid_accounts = _account_ids_by_cloud(conn)
    for s in scopes:
        err = authz.validate_scope_shape(s, valid_accounts)
        if err:
            conn.close()
            raise HTTPException(status_code=400, detail=f"Invalid scope: {err}")

    cursor = conn.cursor()'''

NEW = '''    valid_accounts = _account_ids_by_cloud(conn)
    for s in scopes:
        err = authz.validate_scope_shape(s, valid_accounts)
        if err:
            conn.close()
            raise HTTPException(status_code=400, detail=f"Invalid scope: {err}")

    # Defense-in-depth, not a fix for a live bug: today this endpoint is
    # reachable only by admin (groups.update is admin-only in
    # role_permissions -- see db/migrations/015_permissions_rbac.sql),
    # and admin's effective scope is FULL_ACCESS, so scope_within always
    # passes for the only caller who can reach this today. Added anyway,
    # matching the SAME redundant check app/api/admin/users.py already
    # has for individual access_scopes grants (_validate_and_insert_scopes),
    # so this endpoint isn't a single point of failure if groups.update
    # is ever granted to a non-admin role in the future, or if
    # has_permission() ever had its own bug -- the permission gate and
    # this scope check are independent layers, same principle as
    # users.py's own docstring ("never trust anything the client sent
    # about its own permissions").
    if current_user["role"] != "admin":
        actor_scope = authz.get_effective_scope(current_user)
        if not authz.scope_within(scopes, actor_scope):
            conn.close()
            raise HTTPException(
                status_code=403,
                detail="Cannot grant a group access outside your own assigned scope",
            )

    cursor = conn.cursor()'''


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
    path = os.path.join(repo_root, "app", "api", "admin", "groups.py")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    with open(path) as f:
        content = f.read()

    if "Cannot grant a group access outside your own assigned scope" in content:
        print("\nAlready patched -- nothing to do.")
        return

    n = content.count(OLD)
    if n != 1:
        die(f"Expected exactly 1 match, found {n}. File may differ from what this script expects.")

    new_content = content.replace(OLD, NEW, 1)
    print(f"\nFile patch plan:\n  app/api/admin/groups.py: OK ({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w") as f:
        f.write(new_content)
    print("Patched app/api/admin/groups.py")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub

  B) No functional change expected for normal admin use -- confirm
     Settings -> Groups -> attaching a policy still works exactly as
     before (you're using it as admin, which this change doesn't
     affect).

  C) Review, commit, push:
       git diff app/api/admin/groups.py
       git add app/api/admin/groups.py apply_harden_group_policy_scope_check.py
       git commit -m "security(rbac): add defense-in-depth scope_within check to add_group_policy, matching the equivalent protection users.py already has for individual scope grants. Not a live-bug fix -- this endpoint is admin-only by design today -- but removes a single point of failure if groups.update is ever granted to a non-admin role in the future."
       git push origin main
""")


if __name__ == "__main__":
    main()
