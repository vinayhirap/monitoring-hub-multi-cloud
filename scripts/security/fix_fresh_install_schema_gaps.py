#!/usr/bin/env python3
"""
fix_fresh_install_schema_gaps.py
===================================
Monitoring Hub -- fixes two real "a fresh install is broken" bugs found
while auditing db/schema.sql against the live production schema.

BUG 1: metric_catalog's base table is never created anywhere
----------------------------------------------------------------
Every script that touches metric_catalog (apply_multi_cloud_migration.py,
apply_fresh_schema_migrations.py, scripts/seed_metric_catalog.py) only
ever ALTERs it or inserts into it -- none of them, nor db/schema.sql,
nor any db/migrations/*.sql file, ever runs the initial CREATE TABLE.
It exists in every environment today only because someone created it
manually, outside of anything tracked in git. A genuinely fresh clone +
fresh database would fail on the very FIRST migration script
(apply_multi_cloud_migration.py) with "Table 'metric_catalog' doesn't
exist", and everything after it in the chain cascades from there
(account_metric_selections has a foreign key straight to it).

Fix: adds apply_ensure_metric_catalog_base_table.py, which creates ONLY
the foundational pre-migration-003 columns (id, service, metric_name,
statistic, unit, default_interval, enabled) if the table doesn't exist
at all. It deliberately does NOT add provider/namespace/display_service/
category/description/is_default -- those are already correctly owned
and added by the existing, already-tested ALTER-based scripts that run
right after it. On every environment that already has metric_catalog
(i.e. everywhere this app currently runs), this is a silent no-op.

BUG 2: setup.sh's migration list is missing the entire RBAC chain
----------------------------------------------------------------------
deploy.sh and update.sh both run:
    apply_org_group_rbac.py, apply_group_level_role_fix.py,
    apply_default_org_groups_seed.py, apply_permission_rbac_migration.py,
    then `migrate.py baseline --all-except-rollbacks`
setup.sh (the TRUE fresh-install path) stops right after
apply_alert_evaluation_hardening_migration.py + seed_metric_catalog.py
-- it never runs any of the above. This is the exact same class of bug
deploy.sh's own comments say already caused a real incident once before
(setup.sh's migration list silently drifting from deploy.sh's).

Concretely, a fresh install via setup.sh today would produce a database
with NO org_groups/group_policies/user_group_memberships tables and NO
permissions/role_permissions tables or seed data -- which, combined
with TODAY'S earlier fix wiring require_permission() into every route,
means EVERY request to those routes would fail outright (the
permission-lookup query would hit tables that don't exist).

Fix: adds the same 5 missing steps to setup.sh, in the same relative
order and with the same wording deploy.sh/update.sh already use.

WHAT THIS SCRIPT DOES
------------------------
  1. Creates scripts/security/../ -- no, creates
     apply_ensure_metric_catalog_base_table.py at the repo root (matching
     where every other apply_*.py script already lives).
  2. Adds a run_migration call for it as the very FIRST migration step
     in ALL THREE of setup.sh, deploy.sh, and update.sh (it must run
     before apply_multi_cloud_migration.py, which is currently first
     and already assumes metric_catalog exists).
  3. Adds the 5 missing RBAC-chain steps to setup.sh ONLY (deploy.sh and
     update.sh already have them), matching deploy.sh's wording/order
     exactly, so all three scripts are back in sync -- the exact kind
     of drift that caused a documented past incident.

WHAT THIS SCRIPT DOES NOT DO
--------------------------------
  - Does NOT run any migration itself. It only edits the three shell
     scripts and adds one new Python script -- you run setup.sh/
     deploy.sh/update.sh yourself, whenever you actually need to.
  - Does NOT touch this server's OWN database (metric_catalog already
     exists here -- the new script would just no-op if you ran it,
     but this fix is really for the NEXT fresh install/DR scenario,
     not for the box you're reading this on right now).

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_fresh_install_schema_gaps.py --dry-run
    python3 fix_fresh_install_schema_gaps.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

NEW_SCRIPT_NAME = "apply_ensure_metric_catalog_base_table.py"

NEW_SCRIPT_CONTENT = '''#!/usr/bin/env python3
"""
apply_ensure_metric_catalog_base_table.py

FIXES A REAL "FRESH INSTALL IS BROKEN" BUG.

Every migration script that touches metric_catalog -- apply_multi_cloud_
migration.py (adds `provider`), apply_fresh_schema_migrations.py (adds
`namespace`/`display_service`/`category`/`description`/`is_default` +
indexes), scripts/seed_metric_catalog.py (seeds rows) -- ASSUMES the base
metric_catalog table already exists and only ever ALTERs it. Nothing in
this repository -- not db/schema.sql, not any db/migrations/*.sql file,
not any apply_*.py script -- ever actually CREATEs it.

On every environment this project currently runs on, metric_catalog
exists because it was created manually at some point in the past,
outside of anything tracked in git. On a genuinely fresh clone + fresh
database, apply_multi_cloud_migration.py (the FIRST migration script run
by setup.sh/deploy.sh/update.sh) would fail immediately with "Table
'metric_catalog' doesn't exist", and everything after it in the chain
would cascade-fail too -- account_metric_selections's CREATE TABLE has
a foreign key straight to metric_catalog(id).

This script must run BEFORE apply_multi_cloud_migration.py (first in
the run_migration list, in all three of setup.sh/deploy.sh/update.sh).
It creates ONLY the foundational pre-migration-003 columns (id, service,
metric_name, statistic, unit, enabled, default_interval) -- deliberately
NOT provider/namespace/display_service/category/description/is_default,
since those are already correctly owned and added by the existing,
already-tested ALTER-based scripts later in the chain. This preserves
the intended incremental layering instead of duplicating logic that
already exists elsewhere.

Idempotent: if metric_catalog already exists (every real environment
today), this is a silent no-op -- CREATE TABLE IF NOT EXISTS, no ALTERs,
nothing that could conflict with or duplicate what's already there.

Usage:
    python3 apply_ensure_metric_catalog_base_table.py --dry-run
    python3 apply_ensure_metric_catalog_base_table.py
"""
import argparse
import os
import sys

from dotenv import load_dotenv
load_dotenv()

import mysql.connector

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", 3306))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root123")
DB_NAME = os.getenv("DB_NAME", "monitoring_hub")

CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS metric_catalog (
      id               BIGINT AUTO_INCREMENT PRIMARY KEY,
      service          VARCHAR(50) DEFAULT NULL,
      metric_name      VARCHAR(100) DEFAULT NULL,
      statistic        VARCHAR(20) DEFAULT NULL,
      unit             VARCHAR(20) DEFAULT NULL,
      default_interval INT DEFAULT NULL,
      enabled          TINYINT(1) DEFAULT 1
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""


def get_connection():
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME, use_pure=True,
    )


def table_exists(cursor, table) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
        (DB_NAME, table),
    )
    return cursor.fetchone()[0] > 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    cursor = conn.cursor()

    if table_exists(cursor, "metric_catalog"):
        print("metric_catalog already exists -- nothing to do (expected on every "
              "existing environment; this script exists for fresh installs).")
        cursor.close()
        conn.close()
        return

    print("metric_catalog does NOT exist -- this is a genuinely fresh database.")
    if args.dry_run:
        print("[dry-run] Would run:")
        print(CREATE_SQL)
        cursor.close()
        conn.close()
        return

    cursor.execute(CREATE_SQL)
    conn.commit()
    print("Created metric_catalog (base columns only -- provider/namespace/"
          "display_service/category/description/is_default + indexes are "
          "added next by apply_multi_cloud_migration.py and "
          "apply_fresh_schema_migrations.py, as already designed).")
    cursor.close()
    conn.close()


if __name__ == "__main__":
    main()
'''

FIRST_MIGRATION_OLD = '''run_migration apply_multi_cloud_migration.py \\
    "009: aws_accounts/resources/metric_catalog provider columns"'''

FIRST_MIGRATION_NEW = '''run_migration apply_ensure_metric_catalog_base_table.py \\
    "FOUNDATIONAL: create metric_catalog base table if this is a truly fresh DB (no-op otherwise) -- must run before everything below, which only ever ALTERs it"
run_migration apply_multi_cloud_migration.py \\
    "009: aws_accounts/resources/metric_catalog provider columns"'''

SETUP_SH_RBAC_GAP_OLD = '''run_migration apply_alert_evaluation_hardening_migration.py \\
    "012: alerts.last_seen_at/healthy_streak + alert_pending table"
run_migration scripts/seed_metric_catalog.py \\
    "seed: metric_catalog curated + directory entries"'''

SETUP_SH_RBAC_GAP_NEW = '''run_migration apply_alert_evaluation_hardening_migration.py \\
    "012: alerts.last_seen_at/healthy_streak + alert_pending table"
run_migration apply_org_group_rbac.py \\
    "013: org_groups/group_policies/user_group_memberships -- required or ANY non-admin login 500s on scoped endpoints"
run_migration apply_group_level_role_fix.py \\
    "guard: re-assert GROUP_LEVEL_ROLE in authorization.py after 013's full rewrite drops it"
run_migration apply_default_org_groups_seed.py \\
    "seed: default L1 Monitoring / L2 Operations / L3 Administrator org groups (must run AFTER apply_org_groups_ui_and_role_sync_fix.py)"
run_migration apply_permission_rbac_migration.py \\
    "015: permissions/role_permissions seed data (must run AFTER apply_permission_rbac_system.py)"
run_migration scripts/seed_metric_catalog.py \\
    "seed: metric_catalog curated + directory entries"

echo "--- db/migrations/*.sql tracking (migrate.py) ---"
sudo -u "$REAL_USER" "$VENV_DIR/bin/python3" migrate.py baseline --all-except-rollbacks'''


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


def patch_shell_file(path, old, new, label):
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    n = content.count(old)
    if n != 1:
        die(f"{label}: expected exactly 1 match, found {n}. Aborting before "
            f"changing anything -- file may differ from what this script expects.")
    return content.replace(old, new, 1)


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

    new_script_path = os.path.join(repo_root, NEW_SCRIPT_NAME)
    if os.path.exists(new_script_path):
        print(f"{NEW_SCRIPT_NAME} already exists -- will not overwrite.")
        new_script_path = None

    shell_files = {
        "setup.sh": os.path.join(repo_root, "setup.sh"),
        "deploy/deploy.sh": os.path.join(repo_root, "deploy", "deploy.sh"),
        "deploy/update.sh": os.path.join(repo_root, "deploy", "update.sh"),
    }
    for label, path in shell_files.items():
        if not os.path.exists(path):
            die(f"{label} not found at {path}.")

    results = {}
    for label, path in shell_files.items():
        with open(path, "r", encoding="utf-8") as fh:
            original = fh.read()
        patched = patch_shell_file(path, FIRST_MIGRATION_OLD, FIRST_MIGRATION_NEW,
                                    f"{label} (insert new first migration step)")
        if label == "setup.sh":
            n = patched.count(SETUP_SH_RBAC_GAP_OLD)
            if n != 1:
                die(f"setup.sh (RBAC-chain gap): expected exactly 1 match, found {n}.")
            patched = patched.replace(SETUP_SH_RBAC_GAP_OLD, SETUP_SH_RBAC_GAP_NEW, 1)
        results[label] = (path, original, patched)

    print("\nAll patches matched expected content exactly:")
    for label, (path, original, patched) in results.items():
        print(f"  {label}: OK ({len(patched) - len(original):+d} bytes)")
    if new_script_path:
        print(f"  {NEW_SCRIPT_NAME}: OK (new file, {len(NEW_SCRIPT_CONTENT)} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    if new_script_path:
        with open(new_script_path, "w", encoding="utf-8") as fh:
            fh.write(NEW_SCRIPT_CONTENT)
        os.chmod(new_script_path, 0o755)
        print(f"Created {NEW_SCRIPT_NAME}")

    for label, (path, original, patched) in results.items():
        bpath = backup(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(patched)
        print(f"Patched {label} (backup: {os.path.basename(bpath)})")

    print("""
[Manual follow-up]

  A) This does NOT touch this server's own database (metric_catalog
     already exists here) or restart anything -- it only fixes the
     deploy SCRIPTS for the next fresh install / DR rebuild. Nothing
     to restart on THIS box because of this specific change.

  B) If you want to actually prove this works, the real test is
     spinning up a genuinely fresh box/VM and running setup.sh there --
     not something to do against this running dev server. Flagging
     that as a real validation step worth doing at some point, not
     required right now.

  C) Review, commit, push:
       git status
       git diff setup.sh deploy/deploy.sh deploy/update.sh
       git add setup.sh deploy/deploy.sh deploy/update.sh apply_ensure_metric_catalog_base_table.py
       git commit -m "fix(deploy): fresh install was broken -- metric_catalog never created, setup.sh missing RBAC migration chain"
       git push origin main
""")


if __name__ == "__main__":
    main()
