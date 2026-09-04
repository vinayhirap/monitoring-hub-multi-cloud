#!/usr/bin/env python3
"""
apply_default_org_groups_seed.py  (v2 -- fixes dry-run parent-chain bug)

1. FRONTEND POLISH: the Add Organization Group modal's Name field had
   a placeholder of "e.g. APAC, India-NOC, L3-OnCall" -- leftover
   copy from before the org's actual naming convention was settled.
   Replaced with a neutral, non-prescriptive hint so it doesn't
   suggest names that don't match what's actually being seeded below.

2. DEFAULT GROUPS SEED (backend/DB): seeds the three groups every
   deployment should start with, one per level, chained correctly
   (L2's parent is the L1 row, L3's parent is the L2 row -- required
   by authz.validate_group_level_and_parent / GROUP_PARENT_LEVEL):

     L1 Monitoring     (level L1, no parent)  -> members become Viewer
     L2 Operations      (level L2, parent = L1 Monitoring) -> Editor
     L3 Administrator   (level L3, parent = L2 Operations) -> Admin

   This mirrors GROUP_LEVEL_ROLE (L1->viewer, L2->editor, L3->admin)
   one-for-one, so the names describe exactly what a member of each
   group gets.

   This direction is intentional, not arbitrary: get_group_chain() /
   get_user_effective_groups() make a member inherit every ANCESTOR's
   policies. Putting the least-privileged group (L1) at the root and
   the most-privileged group (L3) at the leaf means Admins accumulate
   everything below them -- which is what "Admin" should mean. Reversing
   this (most-privileged at the root) would make Viewers inherit
   Administrator's policies -- do not invert parent/child direction.

   IDEMPOTENT BY DESIGN, safe to re-run: each group is looked up by
   its unique `name` (org_groups.name has a UNIQUE constraint) before
   inserting, so re-running this after an admin has already renamed,
   deleted, or kept these groups does not resurrect or duplicate
   anything -- it only fills in whichever of the three (if any) are
   still missing. If an admin deletes "L1 Monitoring" and re-runs this
   script, it comes back; if that's not wanted, don't re-run it, or
   rename instead of deleting.

   Creation is attributed to the lowest-id existing admin user
   (org_groups.created_by is NOT NULL / FK'd to users) -- there is
   necessarily at least one, since this script assumes the RBAC system
   is already live and someone is running it. Aborts cleanly with no
   DB writes if no admin exists.

   Both group creation (backend: require_permission("groups.create"),
   granted only to the "admin" role per db/migrations/015_permissions_
   rbac.sql) and the Add Group button itself (frontend: gated behind
   `isAdmin` at UserManagement.jsx) were already admin-only before this
   script -- editors can view the Groups tab (read-only) but cannot
   create, edit, or delete groups. No permission changes needed here;
   this script only adds default DATA and cleans up placeholder text.

   v2 FIX: in --dry-run mode, a "would create" group is now recorded
   in the in-memory name->id map (with a placeholder id) so that
   dependent rows further down the chain (L2 depending on L1, L3 on
   L2) can resolve their parent within the same dry run instead of
   failing with "parent not found" -- that was a bug in v1's dry-run
   path only; real (non-dry-run) runs were never affected, since real
   inserts were always recorded correctly.

Usage:
    python apply_default_org_groups_seed.py --dry-run
    python apply_default_org_groups_seed.py
    python apply_default_org_groups_seed.py --skip-db     # code-only
    python apply_default_org_groups_seed.py --skip-code   # DB-only
"""
import argparse
import shutil
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-default-org-groups-seed"

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3307"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root123")
DB_NAME = os.getenv("DB_NAME", "monitoring_hub")

# name, level, parent_name (None for L1), description
DEFAULT_GROUPS = [
    ("L1 Monitoring",    "L1", None,               "Top-level group -- members get Viewer access."),
    ("L2 Operations",    "L2", "L1 Monitoring",     "Mid-level group -- members get Editor access."),
    ("L3 Administrator", "L3", "L2 Operations",     "Leaf-level group -- members get Admin access."),
]


class PatchError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────
# 1. Frontend: placeholder text cleanup
# ─────────────────────────────────────────────────────────────────────
JSX_PLACEHOLDER_OLD = r'''placeholder="e.g. APAC, India-NOC, L3-OnCall"'''
JSX_PLACEHOLDER_NEW = r'''placeholder="Group name"'''

CODE_PATCHES = [
    ("frontend/src/pages/UserManagement.jsx", [(JSX_PLACEHOLDER_OLD, JSX_PLACEHOLDER_NEW)]),
]


def code_preflight():
    print("=== Pre-flight: verifying anchors match exactly ===")
    problems = []
    for rel_path, replacements in CODE_PATCHES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        for old, _new in replacements:
            count = text.count(old)
            if count == 0:
                problems.append(f"{rel_path}: anchor not found (0 matches) — {old[:70]!r}")
            elif count > 1:
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1 — {old[:70]!r}")
            else:
                print(f"  OK  {rel_path}: anchor matched exactly once")

    if problems:
        print("\n".join(problems))

        def _already(rel, new_text):
            p = REPO_ROOT / rel
            return p.exists() and new_text in p.read_text(encoding="utf-8")

        already_applied = all(_already(rel, new) for rel, repls in CODE_PATCHES for _old, new in repls)
        if already_applied:
            print("\nCode change already present — nothing to patch.")
            return "already_applied"
        raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")
    print("Pre-flight OK.\n")
    return "ok"


def apply_code(dry_run: bool):
    changed_files = []
    for rel_path, replacements in CODE_PATCHES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        original_text = text
        for old, new in replacements:
            if new in text:
                continue
            if old not in text:
                raise PatchError(f"{rel_path}: expected anchor vanished mid-patch — aborting")
            text = text.replace(old, new, 1)

        if text == original_text:
            continue

        if dry_run:
            print(f"[DRY RUN] would patch: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(text, encoding="utf-8")
            print(f"PATCHED: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)
    return changed_files


# ─────────────────────────────────────────────────────────────────────
# 2. DB: seed the 3 default groups
# ─────────────────────────────────────────────────────────────────────
def get_connection():
    import mysql.connector
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME, use_pure=True,
        connection_timeout=10,
    )


def seed_groups(dry_run: bool):
    try:
        import mysql.connector  # noqa: F401
    except ImportError:
        raise PatchError(
            "mysql-connector-python is not installed. Install it "
            "(pip install mysql-connector-python) or re-run with --skip-db."
        )

    print("\n=== DB: seeding default org groups ===")
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
        admin = cursor.fetchone()
        if not admin:
            raise PatchError(
                "No user with role='admin' exists — refusing to seed groups "
                "(org_groups.created_by requires a real admin user id). "
                "Create an admin user first, then re-run."
            )
        admin_id = admin["id"]

        name_to_id = {}
        for name, level, parent_name, description in DEFAULT_GROUPS:
            cursor.execute("SELECT id FROM org_groups WHERE name = %s", (name,))
            existing = cursor.fetchone()
            if existing:
                name_to_id[name] = existing["id"]
                print(f"  SKIP   {name} ({level}) — already exists (id {existing['id']})")
                continue

            parent_id = name_to_id.get(parent_name) if parent_name else None
            if parent_name and parent_id is None:
                cursor.execute("SELECT id FROM org_groups WHERE name = %s", (parent_name,))
                row = cursor.fetchone()
                if not row:
                    raise PatchError(
                        f"Cannot seed '{name}': parent group '{parent_name}' "
                        f"not found and wasn't created in this run either."
                    )
                parent_id = row["id"]

            if dry_run:
                print(f"  [DRY RUN] would create {name} ({level}, parent={parent_name or 'none'})")
                name_to_id[name] = f"<dry-run-id:{name}>"
                continue

            cursor.execute(
                "INSERT INTO org_groups (name, level, parent_group_id, description, created_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (name, level, parent_id, description, admin_id),
            )
            new_id = cursor.lastrowid
            name_to_id[name] = new_id
            print(f"  CREATED {name} ({level}, id {new_id}, parent={parent_name or 'none'})")

        if not dry_run:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-db", action="store_true", help="Only apply the frontend placeholder patch.")
    parser.add_argument("--skip-code", action="store_true", help="Only seed the default groups in the DB.")
    args = parser.parse_args()

    try:
        if not args.skip_code:
            status = code_preflight()
            if status == "ok":
                apply_code(args.dry_run)

        if not args.skip_db:
            seed_groups(args.dry_run)

        if not args.dry_run:
            print("\n=== Done. ===")
            print("Next steps on the server:")
            print("  1) cd frontend && npm install --silent && npm run build   # picks up placeholder change")
            print("  2) Hard-refresh the browser")
            print("  (no backend restart needed -- this run only touched the DB and a frontend string)")
    except PatchError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
