#!/usr/bin/env python3
"""
apply_group_level_role_fix.py
==============================
Guarantees GROUP_LEVEL_ROLE exists in app/auth/authorization.py,
regardless of what ran before it in this deploy/update.

WHY THIS EXISTS (root cause, found 2026-09-05):
apply_org_group_rbac.py does a FULL REWRITE of authorization.py from a
template embedded in that script -- a template written before
GROUP_LEVEL_ROLE existed. It rewrites unconditionally, every single
run, regardless of what's already in the file. Without this guard,
every fresh deploy or update loses GROUP_LEVEL_ROLE, which crashes
every POST /api/groups/{id}/members call with AttributeError.

This is now a permanent, tracked file in the repo (not copied in from
outside it) -- `git clone`/`git pull` bring it automatically, same as
any other file, on both the dev box and any production box pulling
via update.sh.

The inserted text is BYTE-IDENTICAL to what's committed in this repo's
own app/auth/authorization.py right now. That matters: it means after
apply_org_group_rbac.py regresses the file and this guard restores it,
the working tree is byte-for-byte identical to git HEAD again --
`git status` comes back clean, with no manual reset step needed before
the next `git pull`. If this ever needs to change, update BOTH this
script's INSERT text and the real file in the same commit, so they
never drift apart again.

Uses Path.cwd() (not this file's own location) to find the repo root,
so it works correctly regardless of where it's invoked from, as long
as cwd is the repo root -- which deploy.sh/update.sh already guarantee
via `cd "$REPO_DIR"` before any migration runs.
"""
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path.cwd()
TARGET = REPO_ROOT / "app" / "auth" / "authorization.py"

ANCHOR = 'GROUP_PARENT_LEVEL = {"L1": None, "L2": "L1", "L3": "L2"}\n'
INSERT = (
    '\n'
    '# The role a user is given automatically when added as a member of a\n'
    '# group at each level. L1 = Viewer (least access), L2 = Editor (mid),\n'
    '# L3 = Admin (full access) -- referenced by app/api/admin/groups.py\'s\n'
    '# add_group_members() and mirrored client-side in UserManagement.jsx\n'
    '# purely for instant UI feedback; this dict here is the one and only\n'
    '# authoritative source. (Previously referenced from three places in\n'
    '# this codebase but never actually defined -- every group-membership\n'
    '# write has been crashing with AttributeError until this fix.)\n'
    'GROUP_LEVEL_ROLE = {"L1": "viewer", "L2": "editor", "L3": "admin"}\n'
)


def main():
    if not TARGET.exists():
        print(f"ERROR: {TARGET} does not exist (cwd={REPO_ROOT}) -- run the "
              f"Phase 1 / org-group RBAC migrations first.", file=sys.stderr)
        sys.exit(1)

    text = TARGET.read_text(encoding="utf-8")

    if "GROUP_LEVEL_ROLE = " in text:
        print("GROUP_LEVEL_ROLE already present in authorization.py -- nothing to do.")
        return

    if ANCHOR not in text:
        print(f"ERROR: expected anchor not found in {TARGET} -- authorization.py "
              f"has changed shape beyond what this guard expects. Add this by hand:\n"
              f"{INSERT}", file=sys.stderr)
        sys.exit(1)

    backup = TARGET.with_suffix(TARGET.suffix + ".bak.pre-group-level-role-fix")
    if not backup.exists():
        shutil.copy2(TARGET, backup)

    text = text.replace(ANCHOR, ANCHOR + INSERT, 1)
    TARGET.write_text(text, encoding="utf-8")
    print(f"PATCHED {TARGET} -- added GROUP_LEVEL_ROLE (backup: {backup.name})")


if __name__ == "__main__":
    main()
