#!/usr/bin/env python3
"""
fix_rbac_decouple_role_from_groups.py
========================================
Monitoring Hub -- decouples `users.role` from L1/L2/L3 group
membership.

THE BUG THIS FIXES
-------------------
app/api/admin/groups.py's add_group_members() ran:
    UPDATE users SET role = 'admin' WHERE id IN (...)
whenever someone was added to an L3 group (via GROUP_LEVEL_ROLE =
{"L1": "viewer", "L2": "editor", "L3": "admin"}). This is a real
privilege-escalation bug, not a style issue:

  - remove_group_member() never reverses this -- removing someone from
    an L3 group does NOT revoke the admin role that joining it granted.
    Once promoted, a user stays `role='admin'` FOREVER, even after
    being removed from every group that "justified" it, with zero
    visible indication anywhere that this happened.
  - Groups are meant as an org/geographic scoping hierarchy (the
    codebase's own example: APAC -> India-NOC -> L3-OnCall), not a
    permission-tier system, so an admin creating an ordinary-sounding
    L3 team has no obvious reason to expect that adding people to it
    silently makes them full system Admins.

THE FIX
-------
Per explicit decision: admin stays a deliberately-assigned, full-access
role. Editor/viewer continue to be scoped by account/region exactly as
they already are today (via access_scopes + group_policies +
get_effective_scope -- UNCHANGED by this script). Groups become PURE
scope containers: joining a group can only ever grant additional
account/region access, never change what role you hold.

Concretely:
  1. app/api/admin/groups.py: remove the `UPDATE users SET role = ...`
     block from add_group_members() entirely. Membership add/remove
     now only ever touches user_group_memberships, never users.role.
  2. app/auth/authorization.py: remove the now-unused GROUP_LEVEL_ROLE
     dict and correct the module/permissions docstrings that describe
     role as derived from group level.
  3. app/auth/permissions.py: correct its docstring reference to the
     same (comment-only change, no logic here to begin with).
  4. frontend/src/pages/UserManagement.jsx: remove the client-side
     mirror of GROUP_LEVEL_ROLE and the UI that locked/auto-set the
     Role dropdown based on the selected group (it would now show a
     role change that no longer actually happens on the backend) and
     the "role hint" badge shown next to each group in the tree view.

WHAT THIS SCRIPT DOES NOT DO
------------------------------
  - Does NOT change anyone's CURRENT role in the database. Any user
    who was previously auto-promoted to admin via this bug keeps
    whatever role they currently have -- this is a going-forward fix,
    not a retroactive one. See the printed follow-up for a query to
    audit who that might affect, so you can review and decide by hand.
  - Does NOT touch access_scopes, group_policies, or how editor/viewer
    scope resolution works at all -- that is unchanged, per your
    explicit instruction to leave it "as is".

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_rbac_decouple_role_from_groups.py --dry-run
    python3 fix_rbac_decouple_role_from_groups.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

FILES = {
    "groups_py": "app/api/admin/groups.py",
    "authz_py": "app/auth/authorization.py",
    "permissions_py": "app/auth/permissions.py",
    "jsx": "frontend/src/pages/UserManagement.jsx",
}


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


def replace_exact(content, old, new, label):
    n = content.count(old)
    if n != 1:
        die(f"{label}: expected exactly 1 match, found {n}. "
            f"File may have changed since this script was written -- "
            f"aborting rather than guessing. No files were modified.")
    return content.replace(old, new, 1)


# ---------------------------------------------------------------------------
# app/api/admin/groups.py
# ---------------------------------------------------------------------------

GROUPS_PY_OLD = '''    # Group level sets the member's role tier -- L1 members become
    # Viewer, L2 Editor, L3 Admin (see authz.GROUP_LEVEL_ROLE). This is
    # what makes "which group is this person in" the single source of
    # truth for both scope (via get_effective_scope) and capability
    # tier, instead of the two being picked independently and
    # potentially disagreeing. Applied to every id in user_ids, not
    # just newly-inserted ones, so re-adding an existing member still
    # reconciles their role if it had drifted.
    synced_role = authz.GROUP_LEVEL_ROLE.get(g["level"])
    if synced_role:
        cursor.execute(
            f"UPDATE users SET role = %s WHERE id IN ({placeholders})",
            (synced_role, *user_ids),
        )

    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(
        current_user["username"], "Group membership added",
        f"{g['name']}: +{len(added)} user(s)" + (f", {len(already)} already member" if already else "")
        + (f" -- role synced to {synced_role}" if synced_role else ""),
    )
    return {"status": "updated", "group_id": group_id, "added": added, "already_member": already, "role_synced": synced_role}'''

GROUPS_PY_NEW = '''    # Groups are a PURE scope container -- membership grants
    # account/region access (via group_policies + get_effective_scope)
    # and nothing else. It deliberately does NOT touch users.role.
    #
    # Previously this ran `UPDATE users SET role = ...` based on the
    # group's L1/L2/L3 level (GROUP_LEVEL_ROLE), which meant adding
    # someone to an L3 group silently made them a full system Admin --
    # and removing them from that group never reversed it, since this
    # was the only place that ever wrote users.role from group
    # membership. That was a real privilege-escalation bug (permanent,
    # silent admin promotion with no corresponding revoke path), fixed
    # by removing the auto-sync entirely. Role is now only ever changed
    # deliberately (see app/api/admin/users.py's role-update endpoint).
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(
        current_user["username"], "Group membership added",
        f"{g['name']}: +{len(added)} user(s)" + (f", {len(already)} already member" if already else ""),
    )
    return {"status": "updated", "group_id": group_id, "added": added, "already_member": already}'''


def patch_groups_py(content):
    return replace_exact(content, GROUPS_PY_OLD, GROUPS_PY_NEW, "app/api/admin/groups.py")


# ---------------------------------------------------------------------------
# app/auth/authorization.py
# ---------------------------------------------------------------------------

AUTHZ_DOCSTRING_OLD = '''  A user placed in an L3 group inherits that L3 group's OWN policy
  PLUS its L2 parent's PLUS its L1 grandparent's -- ADDITIVE (union),
  not restrictive (not an SCP-style narrowing). This mirrors how
  AWS IAM Identity Center permission sets attached at different OU
  levels all apply to a principal beneath them, and matches the
  "tiered support" mental model (L1/L2/L3) this was built for: an L3
  on-call engineer should see everything their L2 team and L1 org see,
  plus whatever extra the L3 tier itself was granted -- never less.'''

AUTHZ_DOCSTRING_NEW = '''  A user placed in an L3 group inherits that L3 group's OWN policy
  PLUS its L2 parent's PLUS its L1 grandparent's -- ADDITIVE (union),
  not restrictive (not an SCP-style narrowing). This mirrors how
  AWS IAM Identity Center permission sets attached at different OU
  levels all apply to a principal beneath them.

  Groups are a scope container ONLY. Membership never changes a
  user's role (admin/editor/viewer) -- role is always assigned
  deliberately and independently of which group(s) someone belongs
  to. (An earlier version of this system auto-set role from group
  level via a GROUP_LEVEL_ROLE mapping; that was removed because it
  silently granted full Admin on joining an L3 group and never
  revoked it on leaving one -- a real, permanent privilege-escalation
  bug, not just a naming confusion.)'''

GROUP_LEVEL_ROLE_LINE_OLD = '''GROUP_PARENT_LEVEL = {"L1": None, "L2": "L1", "L3": "L2"}

# The role a user is given automatically when added as a member of a
# group at each level. L1 = Viewer (least access), L2 = Editor (mid),
# L3 = Admin (full access) -- referenced by app/api/admin/groups.py's
# add_group_members() and mirrored client-side in UserManagement.jsx
# purely for instant UI feedback; this dict here is the one and only
# authoritative source. (Previously referenced from three places in
# this codebase but never actually defined -- every group-membership
# write has been crashing with AttributeError until this fix.)
GROUP_LEVEL_ROLE = {"L1": "viewer", "L2": "editor", "L3": "admin"}'''

GROUP_LEVEL_ROLE_LINE_NEW = '''GROUP_PARENT_LEVEL = {"L1": None, "L2": "L1", "L3": "L2"}

# NOTE: there is deliberately no L1/L2/L3 -> role mapping here anymore.
# A prior version (GROUP_LEVEL_ROLE) auto-set a user's role based on
# which group they joined, which meant joining an L3 group silently
# granted full system Admin with no corresponding revoke when removed
# -- a real privilege-escalation bug. Groups now only ever grant scope
# (via group_policies, resolved in get_effective_scope below); role is
# always assigned deliberately via app/api/admin/users.py.'''


def patch_authz_py(content):
    content = replace_exact(content, AUTHZ_DOCSTRING_OLD, AUTHZ_DOCSTRING_NEW,
                             "app/auth/authorization.py (module docstring)")
    content = replace_exact(content, GROUP_LEVEL_ROLE_LINE_OLD, GROUP_LEVEL_ROLE_LINE_NEW,
                             "app/auth/authorization.py (GROUP_LEVEL_ROLE definition)")
    return content


# ---------------------------------------------------------------------------
# app/auth/permissions.py
# ---------------------------------------------------------------------------

PERMISSIONS_DOC_OLD = '''Granular permission-identifier RBAC, layered ON TOP OF the existing
role system (admin/editor/viewer) rather than replacing it --
role_permissions (db/migrations/015_permissions_rbac.sql) maps each of
the 3 existing roles to a set of permission codes. Nothing about how a
user GETS a role changes here -- direct assignment, or via L1/L2/L3
group membership through app.auth.authorization.GROUP_LEVEL_ROLE --
this only makes what that role can DO expressible as named permissions
(users.create, groups.manage, ...) instead of role checks scattered
through every endpoint.'''

PERMISSIONS_DOC_NEW = '''Granular permission-identifier RBAC, layered ON TOP OF the existing
role system (admin/editor/viewer) rather than replacing it --
role_permissions (db/migrations/015_permissions_rbac.sql) maps each of
the 3 existing roles to a set of permission codes. A user's role is
always assigned deliberately (app/api/admin/users.py) and is never
derived from L1/L2/L3 group membership -- groups only ever grant
account/region SCOPE, never role. This only makes what a role can DO
expressible as named permissions (users.create, groups.manage, ...)
instead of role checks scattered through every endpoint.'''


def patch_permissions_py(content):
    return replace_exact(content, PERMISSIONS_DOC_OLD, PERMISSIONS_DOC_NEW,
                          "app/auth/permissions.py (module docstring)")


# ---------------------------------------------------------------------------
# frontend/src/pages/UserManagement.jsx
# ---------------------------------------------------------------------------

JSX_CONST_OLD = '''// Mirrors app/auth/authorization.py's GROUP_LEVEL_ROLE exactly -- the
// role a user is given automatically when assigned to a group at each
// level. Kept in sync here purely so the Role dropdown can show/lock
// to the right value the instant a group is picked, without waiting
// on a round trip; the backend applies the same mapping authoritatively
// when the membership is actually created, so this can never drift
// into being the source of truth.
const GROUP_LEVEL_ROLE = { L1: "viewer", L2: "editor", L3: "admin" };

'''

JSX_CONST_NEW = ''''''

JSX_GROUP_SELECT_OLD = '''                  onChange={e => {
                    const groupId = e.target.value;
                    const selected = groups.find(g => String(g.id) === groupId);
                    const impliedRole = selected ? GROUP_LEVEL_ROLE[selected.level] : null;
                    setForm(f => ({
                      ...f,
                      groupId,
                      role: impliedRole || f.role,
                      accountIds: impliedRole ? [] : f.accountIds,
                    }));
                  }}'''

JSX_GROUP_SELECT_NEW = '''                  onChange={e => {
                    const groupId = e.target.value;
                    // Group membership only ever grants account/region
                    // scope (see app/auth/authorization.py) -- it never
                    // changes role, so picking a group here doesn't
                    // touch form.role or clear accountIds.
                    setForm(f => ({ ...f, groupId }));
                  }}'''

JSX_ROLE_FIELD_OLD = '''                <label>Role{form.groupId ? " (set by group)" : ""}</label>
                <select
                  value={form.role}
                  disabled={!!form.groupId}
                  onChange={e => setForm(f => ({ ...f, role: e.target.value, accountIds: [] }))}
                >
                  <option value="viewer">Viewer — read-only</option>
                  <option value="editor">Editor — view + configure alerts</option>
                  <option value="admin">Admin — full access</option>
                </select>
                {form.groupId && (
                  <span className="field-hint">
                    Role is locked to this group's level. Choose "No group" above to set a role manually instead.
                  </span>
                )}'''

JSX_ROLE_FIELD_NEW = '''                <label>Role</label>
                <select
                  value={form.role}
                  onChange={e => setForm(f => ({ ...f, role: e.target.value, accountIds: [] }))}
                >
                  <option value="viewer">Viewer — read-only</option>
                  <option value="editor">Editor — view + configure alerts</option>
                  <option value="admin">Admin — full access</option>
                </select>
                {form.groupId && (
                  <span className="field-hint">
                    Group membership grants this user additional account/region access; it does not change their role.
                  </span>
                )}'''

JSX_GROUP_ROLE_HINT_OLD = '''                      <span className="group-role-hint">{{ L1: "viewer", L2: "editor", L3: "admin" }[g.level]}</span>
'''

JSX_GROUP_ROLE_HINT_NEW = ''''''


def patch_jsx(content):
    content = replace_exact(content, JSX_CONST_OLD, JSX_CONST_NEW,
                             "UserManagement.jsx (GROUP_LEVEL_ROLE const)")
    content = replace_exact(content, JSX_GROUP_SELECT_OLD, JSX_GROUP_SELECT_NEW,
                             "UserManagement.jsx (group select onChange)")
    content = replace_exact(content, JSX_ROLE_FIELD_OLD, JSX_ROLE_FIELD_NEW,
                             "UserManagement.jsx (role field)")
    content = replace_exact(content, JSX_GROUP_ROLE_HINT_OLD, JSX_GROUP_ROLE_HINT_NEW,
                             "UserManagement.jsx (group tree role-hint badge)")
    return content


# ---------------------------------------------------------------------------

PATCHERS = {
    "groups_py": patch_groups_py,
    "authz_py": patch_authz_py,
    "permissions_py": patch_permissions_py,
    "jsx": patch_jsx,
}


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

    results = {}
    for key, rel_path in FILES.items():
        full_path = os.path.join(repo_root, rel_path)
        if not os.path.exists(full_path):
            die(f"{rel_path} not found at {full_path}. Aborting before changing anything else.")
        with open(full_path, "r", encoding="utf-8") as fh:
            original = fh.read()
        try:
            patched = PATCHERS[key](original)
        except SystemExit:
            raise
        results[key] = (full_path, original, patched)

    print("\nAll 4 files matched expected content exactly. Planned changes:")
    for key, (full_path, original, patched) in results.items():
        changed = "CHANGED" if original != patched else "NO CHANGE (unexpected)"
        print(f"  {FILES[key]}: {changed}")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    for key, (full_path, original, patched) in results.items():
        bpath = backup(full_path)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(patched)
        print(f"Patched {FILES[key]} (backup: {os.path.basename(bpath)})")

    print("""
[Manual follow-up]

  A) Rebuild the frontend (JSX changed, needs a build step -- confirm
     your actual build command, this is the common one for this repo's
     deploy scripts):
       cd frontend && npm run build && cd ..

  B) Restart the backend:
       sudo systemctl restart monitoring-hub
       sudo systemctl status monitoring-hub --no-pager

  C) Verify in the browser:
       - Create/edit a user, pick a group -- confirm the Role dropdown
         stays editable and is NOT forced/locked by the group choice.
       - Add an existing user to an L3 group, then check their role in
         the user list -- it should NOT change to admin.
       - Remove a user from a group -- their role (whatever it already
         was) should be unaffected either way, since role is no longer
         tied to membership at all.

  D) OPTIONAL -- audit who may have been auto-promoted by the OLD
     buggy behavior, so you can review and decide by hand whether any
     of them should be demoted (this script does NOT do this for you):
       mysql -u monitor -p -h 127.0.0.1 monitoring_hub -e "
         SELECT actor, action, payload, created_at FROM audit_logs
         WHERE action = 'Group membership added'
           AND payload LIKE '%role synced to admin%'
         ORDER BY created_at DESC;
       "
     Cross-reference the usernames mentioned there against the CURRENT
     users.role column and each user's CURRENT group memberships
     (GET /api/groups/users/{id}/groups) to see who's still admin
     despite no longer being in (or never having deserved) an L3 group.

  E) Then review, commit, push:
       git status
       git diff app/api/admin/groups.py app/auth/authorization.py app/auth/permissions.py frontend/src/pages/UserManagement.jsx
       git add app/api/admin/groups.py app/auth/authorization.py app/auth/permissions.py frontend/src/pages/UserManagement.jsx
       git commit -m "security(rbac): decouple role from group membership, fix permanent admin-escalation bug"
       git push origin main
""")


if __name__ == "__main__":
    main()
