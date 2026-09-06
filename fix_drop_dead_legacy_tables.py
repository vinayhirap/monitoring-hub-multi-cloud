#!/usr/bin/env python3
"""
fix_drop_dead_legacy_tables.py
===================================
Monitoring Hub -- makes db/migrations/006_drop_dead_tables.sql actually run.

BACKGROUND
----------
006_drop_dead_tables.sql already exists in db/migrations/ and correctly
identifies 10 dead tables (metric_configs, metric_definitions,
enabled_metrics, alert_rules, dashboards, dashboard_panels,
account_permissions, user_accounts, user_roles, roles) as unreferenced by
any app code. Re-verified via grep across app/ and frontend/src/ during this
pass -- confirmed zero live references to any of them.

But this project's real migration mechanism is NOT db/migrations/*.sql --
it's the curated apply_*.py chain that deploy.sh/update.sh run directly.
`migrate.py baseline --all-except-rollbacks` only RECORDS .sql files as
already-applied for bookkeeping; it never executes their contents. Without
an apply_*.py counterpart wired into the run_migration chain, 006 would
never actually run against any real database -- these 10 tables would sit
there forever regardless of how many times deploy.sh/update.sh run.

(Note: 006's own comment also mentions "user_account_access" as the
replacement design for account_permissions/user_accounts/user_roles. That
table doesn't actually exist -- the real replacement is access_scopes.
Cosmetic inaccuracy in the migration's comment, doesn't affect the drop.)

WHAT THIS SCRIPT DOES
-----------------------
  1. Adds apply_drop_dead_tables.py at the repo root (matching where every
     other apply_*.py script already lives). It is idempotent and refuses
     to drop any table that has row content -- see its own docstring.
  2. Adds one run_migration call for it, right after the existing
     scripts/seed_metric_catalog.py step, in BOTH deploy/deploy.sh and
     deploy/update.sh (the two files confirmed as the ones actually used
     for this project's real deploy/update path -- NOT the root-level
     setup.sh/update.sh, which are a separate, drifted, production-specific
     fork being left alone for now and handled in the later deploy-script
     consolidation pass).

WHAT THIS SCRIPT DOES NOT DO
--------------------------------
  - Does NOT touch root-level setup.sh or update.sh. Those are the
    "AuroGov Mumbai (PRODUCTION)" labeled scripts and are explicitly out of
    scope for this pass.
  - Does NOT run the new script itself. You run deploy.sh/update.sh (or
    apply_drop_dead_tables.py directly) yourself, whenever you actually
    want the drop to happen.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_drop_dead_legacy_tables.py --dry-run
    python3 fix_drop_dead_legacy_tables.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

NEW_SCRIPT_NAME = "apply_drop_dead_tables.py"

NEW_SCRIPT_CONTENT = '''#!/usr/bin/env python3
"""
apply_drop_dead_tables.py

Drops the tables identified in db/migrations/006_drop_dead_tables.sql as
dead -- unreferenced by any application code:

  metric_configs, metric_definitions, enabled_metrics   -> superseded by metric_catalog
  alert_rules                                            -> superseded by thresholds
  dashboards, dashboard_panels                           -> unfinished feature, no route ever reads/writes them
  account_permissions, user_accounts, user_roles, roles  -> superseded by access_scopes + users.role

(Re-verified via grep across app/ and frontend/src/ during this pass -- zero
live references to any of the 10. Note: 006's own comment mentions
"user_account_access" as the replacement design, but the real current table
is named access_scopes -- user_account_access does not exist and is not one
of the 10 being dropped here.)

WHY THIS SCRIPT EXISTS
-----------------------
006_drop_dead_tables.sql already exists and describes this, but this
project's real migration mechanism is NOT db/migrations/*.sql -- it's the
curated apply_*.py chain run by deploy.sh/update.sh (`migrate.py baseline`
only records .sql files as applied, it never executes them). Without an
apply_*.py counterpart, 006 would never actually run anywhere.

SAFETY
------
Refuses to drop any of these tables if it has row content. Each is
expected to be empty (0 rows) on every real environment -- the 006
migration comment itself says so, but this script re-verifies live rather
than trusting that. A non-empty table is left alone and reported loudly;
nothing is ever silently deleted based on row count alone.

Backs up full structure+data for every present table into ./db_backups/
before dropping anything, matching the convention used by every other
migration script in this project.

Idempotent: tables already absent are silently skipped (safe to re-run,
safe on a fresh install where they were never created in the first place).

Uses the `mysql` CLI client via subprocess (no extra Python DB driver
dependency) and reads DB credentials directly from the app's .env file --
same pattern as apply_metrics_dedup_fix.py, since `sudo -u <user>` starts a
fresh shell that doesn't inherit anything sourced under your own login.

Usage:
    python3 apply_drop_dead_tables.py --dry-run
    python3 apply_drop_dead_tables.py
"""

import os
import sys
import subprocess
import datetime
import argparse

DB_HOST = "127.0.0.1"
DB_PORT = "3306"
DB_NAME = "monitoring_hub"
DB_USER = "monitor"

DEAD_TABLES = [
    "metric_configs",
    "metric_definitions",
    "enabled_metrics",
    "alert_rules",
    "dashboard_panels",
    "dashboards",
    "account_permissions",
    "user_accounts",
    "user_roles",
    "roles",
]

ENV_FILE_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    "/opt/monitoring-hub/app/.env",
    "/opt/monitoring-hub/.env",
]

PASSWORD_KEYS = ("MONITOR_DB_PASSWORD", "DB_PASSWORD")


def load_env_password():
    for key in PASSWORD_KEYS:
        if os.environ.get(key):
            return os.environ[key]

    for path in ENV_FILE_CANDIDATES:
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key in PASSWORD_KEYS and value:
                    return value
    return None


DB_PASS = load_env_password()

MYSQL_BASE = ["mysql", f"-u{DB_USER}", "-h", DB_HOST, "-P", DB_PORT, "-N", "-B"]
if DB_PASS:
    MYSQL_BASE.append(f"-p{DB_PASS}")


def run_sql(sql, database=DB_NAME):
    """Run a SQL statement via the mysql CLI, return stdout (tab-separated rows)."""
    cmd = MYSQL_BASE + [database, "-e", sql]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"SQL failed: {sql!r}\\n{result.stderr}")
    return result.stdout


def table_exists(table):
    out = run_sql(
        "SELECT COUNT(*) FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA = '{DB_NAME}' AND TABLE_NAME = '{table}'"
    )
    return int(out.strip()) > 0


def row_count(table):
    out = run_sql(f"SELECT COUNT(*) FROM `{table}`")
    return int(out.strip())


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    present = []
    for t in DEAD_TABLES:
        if table_exists(t):
            present.append((t, row_count(t)))

    if not present:
        print("None of the 10 dead tables exist -- nothing to do "
              "(expected on a fresh install, or a box this already ran on).")
        return

    print(f"Found {len(present)} of {len(DEAD_TABLES)} dead tables present:")
    droppable = []
    blocked = []
    for t, n in present:
        if n == 0:
            print(f"  {t}: 0 rows -- will drop")
            droppable.append(t)
        else:
            print(f"  {t}: {n} row(s) -- NOT dropping, needs manual review")
            blocked.append((t, n))

    if args.dry_run:
        print("--dry-run: no changes made.")
        if blocked:
            print("Would exit non-zero due to blocked (non-empty) table(s) above.")
        return

    if not droppable:
        print("Nothing droppable (every present table has unexpected rows). "
              "Exiting without changes.")
        sys.exit(1)

    os.makedirs("db_backups", exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"db_backups/pre_drop_dead_tables_{ts}.sql"
    present_names = [t for t, _ in present]
    dump_cmd = ["mysqldump", f"-u{DB_USER}"]
    if DB_PASS:
        dump_cmd.append(f"-p{DB_PASS}")
    dump_cmd += ["-h", DB_HOST, "-P", DB_PORT, DB_NAME] + present_names
    with open(backup_path, "w") as f:
        result = subprocess.run(dump_cmd, stdout=f, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"Backup failed, aborting before dropping anything:\\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"Backed up {len(present_names)} table(s) to {backup_path}")

    for t in droppable:
        run_sql(f"DROP TABLE IF EXISTS `{t}`")
        print(f"Dropped {t}")

    if blocked:
        print("\\nWARNING: the following table(s) were left in place because they "
              "contain data -- review manually, this needs a human decision, not "
              "an automatic drop:")
        for t, n in blocked:
            print(f"  {t}: {n} row(s)")
        sys.exit(1)

    print(f"\\nDone. Dropped {len(droppable)} table(s). Backup: {backup_path}")


if __name__ == "__main__":
    main()
'''

MIGRATION_STEP_OLD = '''run_migration scripts/seed_metric_catalog.py \\
    "seed: metric_catalog curated + directory entries"'''

MIGRATION_STEP_NEW = '''run_migration scripts/seed_metric_catalog.py \\
    "seed: metric_catalog curated + directory entries"
run_migration apply_drop_dead_tables.py \\
    "006 (now actually executed): drop metric_configs/metric_definitions/enabled_metrics/alert_rules/dashboards/dashboard_panels/account_permissions/user_accounts/user_roles/roles -- confirmed unreferenced by app/ or frontend/src/, refuses to drop any table with rows"'''


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
        "deploy/deploy.sh": os.path.join(repo_root, "deploy", "deploy.sh"),
        "deploy/update.sh": os.path.join(repo_root, "deploy", "update.sh"),
    }
    for label, path in shell_files.items():
        if not os.path.exists(path):
            die(f"{label} not found at {path}.")

    results = {}
    already_done = []
    for label, path in shell_files.items():
        with open(path, "r", encoding="utf-8") as fh:
            original = fh.read()
        if "run_migration apply_drop_dead_tables.py" in original:
            print(f"{label} already has the apply_drop_dead_tables.py migration step -- will not re-patch.")
            already_done.append(label)
            continue
        patched = patch_shell_file(path, MIGRATION_STEP_OLD, MIGRATION_STEP_NEW,
                                    f"{label} (insert drop-dead-tables migration step)")
        results[label] = (path, original, patched)

    if results:
        print("\nAll patches matched expected content exactly:")
        for label, (path, original, patched) in results.items():
            print(f"  {label}: OK ({len(patched) - len(original):+d} bytes)")
    if new_script_path:
        print(f"  {NEW_SCRIPT_NAME}: OK (new file, {len(NEW_SCRIPT_CONTENT)} bytes)")

    if not results and not new_script_path:
        print("\nNothing to do -- everything this script would add is already present.")
        return

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

  A) This does NOT touch this server's own database yet -- it only adds
     apply_drop_dead_tables.py and wires it into the deploy/update chain.
     To actually run the drop against THIS box's database right now:

       sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_drop_dead_tables.py --dry-run
       sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_drop_dead_tables.py

     Review the dry-run output first -- it will tell you if any of the 10
     tables unexpectedly have rows (none should).

  B) No service restart is needed for this change by itself (it's a DB
     schema change only, not app code) -- but if you run the drop as part
     of a broader deploy, follow the usual post-deploy checks anyway.

  C) Review, commit, push:
       git status
       git diff deploy/deploy.sh deploy/update.sh
       git add deploy/deploy.sh deploy/update.sh apply_drop_dead_tables.py
       git commit -m "fix(db): wire up 006_drop_dead_tables.sql -- it was never actually executed by anything"
       git push origin main

  D) Known, deliberately out of scope for this fix: root-level setup.sh
     and update.sh (the AuroGov Mumbai PRODUCTION-labeled scripts) are a
     separate, already-drifted fork of these deploy scripts and were not
     touched here. That drift (and this same drop) will be addressed in
     the later deploy-script consolidation pass.
""")


if __name__ == "__main__":
    main()
