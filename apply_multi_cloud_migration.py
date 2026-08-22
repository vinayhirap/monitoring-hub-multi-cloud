"""
apply_multi_cloud_migration.py

Applies db/migrations/009_multi_cloud_provider_columns.sql safely.

Usage:
    $env:PYTHONPATH = "."
    python apply_multi_cloud_migration.py --dry-run     # show what would run, change nothing
    python apply_multi_cloud_migration.py                # actually apply

Safety properties (matches the pattern used for every other patch script
on this project):
  - --dry-run prints every ALTER it would issue and exits without touching
    the database.
  - Takes a mysqldump backup of the three affected tables (aws_accounts,
    resources, metric_catalog) to
    ./db_backups/pre_migration_009_<timestamp>.sql before changing anything.
  - Idempotent: checks information_schema.COLUMNS before each ALTER. If a
    column already exists (e.g. a previous partial run), that ALTER is
    skipped rather than erroring out — safe to re-run after an interruption.
  - Verifies row counts on all four tables are unchanged after the run
    (this migration adds columns, never rows) and prints a DESCRIBE-style
    summary of the new columns for you to eyeball before trusting it.
  - No table renames, no drops, no FK changes — nothing here can corrupt
    or lose existing AWS data.

Requires: mysql-connector-python (already a project dependency, see
requirements.txt) and the `mysqldump` client binary on PATH for the
backup step. If mysqldump isn't available, the script refuses to proceed
past --dry-run rather than skip the backup silently.
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys

from dotenv import load_dotenv
load_dotenv()

import mysql.connector

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", 3307))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root123")
DB_NAME = os.getenv("DB_NAME", "monitoring_hub")

# Verified 2026-08-22 against the real local DB (`SHOW TABLES` /
# `DESCRIBE` output pasted by Vinay) — NOT the GitHub clone's schema,
# which turned out to diverge (no `enabled_metrics` table locally; the
# per-account selection table is `account_metric_selections` instead,
# and `resources`/`metric_catalog` both have extra local-only columns).
AFFECTED_TABLES = ["aws_accounts", "resources", "metric_catalog"]

# (table, column, DDL to add it)
COLUMNS_TO_ADD = [
    ("aws_accounts", "provider",
     "ALTER TABLE aws_accounts ADD COLUMN provider ENUM('aws','azure','gcp') NOT NULL DEFAULT 'aws' AFTER id"),
    ("aws_accounts", "tenant_id",
     "ALTER TABLE aws_accounts ADD COLUMN tenant_id VARCHAR(100) DEFAULT NULL AFTER external_id"),
    ("aws_accounts", "subscription_id",
     "ALTER TABLE aws_accounts ADD COLUMN subscription_id VARCHAR(100) DEFAULT NULL AFTER tenant_id"),
    ("aws_accounts", "client_id",
     "ALTER TABLE aws_accounts ADD COLUMN client_id VARCHAR(100) DEFAULT NULL AFTER subscription_id"),
    ("aws_accounts", "project_id",
     "ALTER TABLE aws_accounts ADD COLUMN project_id VARCHAR(100) DEFAULT NULL AFTER client_id"),
    ("aws_accounts", "service_account_email",
     "ALTER TABLE aws_accounts ADD COLUMN service_account_email VARCHAR(255) DEFAULT NULL AFTER project_id"),
    ("aws_accounts", "credential_ref",
     "ALTER TABLE aws_accounts ADD COLUMN credential_ref VARCHAR(255) DEFAULT NULL AFTER service_account_email"),
    ("resources", "normalized_resource_type",
     "ALTER TABLE resources ADD COLUMN normalized_resource_type VARCHAR(50) DEFAULT NULL AFTER resource_type"),
    ("metric_catalog", "provider",
     "ALTER TABLE metric_catalog ADD COLUMN provider ENUM('aws','azure','gcp') NOT NULL DEFAULT 'aws' AFTER service"),
]


def get_connection():
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME, use_pure=True,
    )


def column_exists(cursor, table, column) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        (DB_NAME, table, column),
    )
    return cursor.fetchone()[0] > 0


def row_counts(cursor):
    counts = {}
    for t in AFFECTED_TABLES:
        cursor.execute(f"SELECT COUNT(*) FROM {t}")
        counts[t] = cursor.fetchone()[0]
    return counts


def take_backup():
    if not shutil.which("mysqldump"):
        print("ERROR: mysqldump not found on PATH. Refusing to apply without a backup.")
        print("       Install the MySQL client tools, or run with --dry-run to preview only.")
        sys.exit(1)

    os.makedirs("db_backups", exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join("db_backups", f"pre_migration_009_{ts}.sql")

    cmd = [
        "mysqldump",
        f"-h{DB_HOST}", f"-P{DB_PORT}", f"-u{DB_USER}", f"-p{DB_PASSWORD}",
        DB_NAME, *AFFECTED_TABLES,
    ]
    with open(backup_path, "w", encoding="utf-8") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        print(f"ERROR: mysqldump failed:\n{result.stderr}")
        sys.exit(1)

    print(f"Backup written to {backup_path}")
    return backup_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"Target DB: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    print()

    conn = get_connection()
    cursor = conn.cursor()

    to_apply = []
    for table, column, ddl in COLUMNS_TO_ADD:
        if column_exists(cursor, table, column):
            print(f"SKIP  (already present): {table}.{column}")
        else:
            to_apply.append((table, column, ddl))

    if not to_apply:
        print("\nNothing to do — all columns already present.")
        cursor.close()
        conn.close()
        return

    print(f"\n{len(to_apply)} column(s) to add:")
    for table, column, ddl in to_apply:
        print(f"  + {table}.{column}")
        print(f"    {ddl};")

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        cursor.close()
        conn.close()
        return

    before_counts = row_counts(cursor)
    take_backup()

    print("\nApplying...")
    applied = []
    try:
        for table, column, ddl in to_apply:
            cursor.execute(ddl)
            applied.append((table, column))
            print(f"  OK: {table}.{column}")
        conn.commit()
    except mysql.connector.Error as e:
        print(f"\nERROR applying {table}.{column}: {e}")
        print(f"Columns successfully applied before the failure: {applied}")
        print("MySQL DDL auto-commits per statement and cannot be rolled back in a "
              "transaction. Use 009_multi_cloud_provider_columns_rollback.sql to undo "
              "the columns listed above, or restore from the backup just taken.")
        cursor.close()
        conn.close()
        sys.exit(1)

    after_counts = row_counts(cursor)
    if before_counts != after_counts:
        print(f"\nWARNING: row counts changed unexpectedly!\n  before: {before_counts}\n  after:  {after_counts}")
        print("This migration should never change row counts — investigate before trusting the result.")
    else:
        print(f"\nRow counts unchanged (expected): {after_counts}")

    print("\nVerification — new columns now present:")
    for table, column, _ in to_apply:
        cursor.execute(f"SHOW COLUMNS FROM {table} LIKE %s", (column,))
        row = cursor.fetchone()
        print(f"  {table}.{column}: {row}")

    cursor.close()
    conn.close()
    print("\nDone. All existing rows backfilled with provider='aws' by DEFAULT — "
          "no AWS behavior changes until application code starts reading these columns.")


if __name__ == "__main__":
    main()
