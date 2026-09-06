#!/usr/bin/env python3
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
        raise RuntimeError(f"SQL failed: {sql!r}\n{result.stderr}")
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
        print(f"Backup failed, aborting before dropping anything:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"Backed up {len(present_names)} table(s) to {backup_path}")

    for t in droppable:
        run_sql(f"DROP TABLE IF EXISTS `{t}`")
        print(f"Dropped {t}")

    if blocked:
        print("\nWARNING: the following table(s) were left in place because they "
              "contain data -- review manually, this needs a human decision, not "
              "an automatic drop:")
        for t, n in blocked:
            print(f"  {t}: {n} row(s)")
        sys.exit(1)

    print(f"\nDone. Dropped {len(droppable)} table(s). Backup: {backup_path}")


if __name__ == "__main__":
    main()
