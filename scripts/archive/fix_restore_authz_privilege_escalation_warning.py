#!/usr/bin/env python3
"""
fix_restore_authz_privilege_escalation_warning.py
======================================================
Monitoring Hub -- reconciliation, not a new bug fix.

BACKGROUND
----------
A teammate's independent edit to app/auth/authorization.py did two
unrelated things in one uncommitted change:
  1. Added `created_at` to get_group()'s SELECT -- correct, matches this
     session's own independent diagnosis of the GET /api/groups/{id}
     500 exactly, KEEP this.
  2. Deleted two comment blocks documenting a previously-fixed
     privilege-escalation bug (an old GROUP_LEVEL_ROLE mapping that
     auto-granted full Admin on joining an L3 group, with no revoke on
     leaving -- confirmed as a real, already-fixed bug by the comments
     themselves). No executable code changed in this second part, only
     the documentation of why a dangerous pattern was rejected.

Removing documentation of a rejected dangerous pattern makes it
meaningfully easier for someone (maybe with good intentions, maybe
following the "tiered support" framing the new comment introduces) to
reintroduce that exact bug later without knowing it already happened
once. This script restores that warning while leaving the created_at
fix and the new framing text both in place -- this is a merge, not a
revert.

FIX
---
Re-inserts the two removed comment blocks, positioned exactly where
they were (right after the existing AWS IAM Identity Center analogy
paragraph, and right after GROUP_PARENT_LEVEL). Does not touch
get_group()'s query or anything else in the file.

TESTED: exact-match verified against the file's current (already-edited)
content; the insertion points are anchored on text that survived the
teammate's edit unchanged, so this applies cleanly on top of it.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_restore_authz_privilege_escalation_warning.py --dry-run
    python3 fix_restore_authz_privilege_escalation_warning.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

ANCHOR_1_OLD = '''  levels all apply to a principal beneath them, and matches the
  "tiered support" mental model (L1/L2/L3) this was built for: an L3
  on-call engineer should see everything their L2 team and L1 org see,
  plus whatever extra the L3 tier itself was granted -- never less.

  Every group policy, at any level, is itself account/region specific'''

ANCHOR_1_NEW = '''  levels all apply to a principal beneath them, and matches the
  "tiered support" mental model (L1/L2/L3) this was built for: an L3
  on-call engineer should see everything their L2 team and L1 org see,
  plus whatever extra the L3 tier itself was granted -- never less.

  Groups are a scope container ONLY. Membership never changes a
  user's role (admin/editor/viewer) -- role is always assigned
  deliberately and independently of which group(s) someone belongs
  to. (An earlier version of this system auto-set role from group
  level via a GROUP_LEVEL_ROLE mapping; that was removed because it
  silently granted full Admin on joining an L3 group and never
  revoked it on leaving one -- a real, permanent privilege-escalation
  bug, not just a naming confusion.)

  Every group policy, at any level, is itself account/region specific'''

ANCHOR_2_OLD = '''GROUP_PARENT_LEVEL = {"L1": None, "L2": "L1", "L3": "L2"}


@dataclass
class ScopeGrant:'''

ANCHOR_2_NEW = '''GROUP_PARENT_LEVEL = {"L1": None, "L2": "L1", "L3": "L2"}

# NOTE: there is deliberately no L1/L2/L3 -> role mapping here anymore.
# A prior version (GROUP_LEVEL_ROLE) auto-set a user's role based on
# which group they joined, which meant joining an L3 group silently
# granted full system Admin with no corresponding revoke when removed
# -- a real privilege-escalation bug. Groups now only ever grant scope
# (via group_policies, resolved in get_effective_scope below); role is
# always assigned deliberately via app/api/admin/users.py.


@dataclass
class ScopeGrant:'''


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

    path = os.path.join(repo_root, "app", "auth", "authorization.py")
    if not os.path.exists(path):
        die(f"app/auth/authorization.py not found at {path}.")

    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    if "silently granted full Admin on joining an L3 group" in content:
        print("app/auth/authorization.py already has the warning comments -- nothing to do.")
        return

    for old, label in [(ANCHOR_1_OLD, "module docstring warning"), (ANCHOR_2_OLD, "GROUP_PARENT_LEVEL warning")]:
        n = content.count(old)
        if n != 1:
            die(f"{label}: expected exactly 1 match, found {n}. "
                f"File may differ from what this script expects -- check manually.")

    new_content = content.replace(ANCHOR_1_OLD, ANCHOR_1_NEW, 1)
    new_content = new_content.replace(ANCHOR_2_OLD, ANCHOR_2_NEW, 1)

    print(f"\nPatch matched expected content exactly: "
          f"app/auth/authorization.py ({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("Patched app/auth/authorization.py")

    print("""
[Manual follow-up]

  A) No restart needed -- comment-only change, doesn't affect running
     behavior.

  B) Review the full picture before committing -- this file now has
     BOTH the created_at fix (from your teammate) AND the restored
     warning comments (from this script). Also review migrate.py's
     rollback-file-exclusion change and the package-lock.json diff
     while you're at it, since all of it needs to go into one clean
     commit together:
       git status
       git diff app/auth/authorization.py
       git diff migrate.py

  C) Once everything looks right:
       git add app/auth/authorization.py migrate.py frontend/package-lock.json \\
               fix_restore_authz_privilege_escalation_warning.py
       git commit -m "fix(groups): add missing created_at select (teammate's fix) + restore privilege-escalation warning comments removed alongside it"
       git push origin main

  D) migrate.py.backup is untracked clutter from whatever process edited
     migrate.py -- safe to remove once migrate.py's diff is confirmed
     correct:
       rm migrate.py.backup
""")


if __name__ == "__main__":
    main()
