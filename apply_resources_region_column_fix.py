#!/usr/bin/env python3
"""
apply_resources_region_column_fix.py

Root cause (found 2026-08-26): app/collector/scheduler.py's standard-tier
query selects r.region and fails every single cycle with "Unknown column
'r.region' in 'field list'". This isn't a query bug to work around --
DESCRIBE resources confirms the column genuinely does not exist on this
DB, while app/collector/discovery/runner.py's _upsert_resource() has
always written to it (INSERT INTO resources (..., region) VALUES (...)),
meaning the code has expected this column all along. It's a migration gap:
whatever migration was supposed to add resources.region never ran (or
never existed) for this specific database, the same class of issue
apply_fresh_schema_migrations.py exists to catch -- this one slipped
through because that script's plan_statements() never checked for it.

This script:
  1. Adds resources.region (VARCHAR(20), matching aws_accounts.default_region's
     format, e.g. 'ap-south-1') if it doesn't already exist.
  2. Backfills existing rows: resources with no region get their owning
     account's default_region (a resource's region is virtually always
     its account's default_region in this app's current single-region-
     per-account model -- if that ever changes, discovery's own writes
     going forward will carry the real per-resource region regardless).
  3. Does NOT touch discovery/runner.py or scheduler.py -- both already
     read/write this column correctly; they just needed it to exist.

Same conventions as this project's other patch scripts: dry-run, mysqldump
backup of the affected table before altering, existence-checked (safe to
re-run), no drops, no renames.

Usage:
    python apply_resources_region_column_fix.py --dry-run
    python apply_resources_region_column_fix.py
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import mysql.connector

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", 3306))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root123")
DB_NAME = os.getenv("DB_NAME", "monitoring_hub")


def get_connection():
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME, use_pure=True,
    )


def column_exists(cursor, table, column) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
        (DB_NAME, table, column),
    )
    return cursor.fetchone()[0] > 0


def take_backup():
    if not shutil.which("mysqldump"):
        print("ERROR: mysqldump not found on PATH. Refusing to apply without a backup.")
        print("       Install the MySQL client tools, or run with --dry-run to preview only.")
        sys.exit(1)

    os.makedirs("db_backups", exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join("db_backups", f"pre_resources_region_fix_{ts}.sql")

    cmd = ["mysqldump", f"-h{DB_HOST}", f"-P{DB_PORT}", f"-u{DB_USER}", f"-p{DB_PASSWORD}", DB_NAME, "resources"]
    with open(backup_path, "w", encoding="utf-8") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        print(f"ERROR: mysqldump failed:\n{result.stderr}")
        sys.exit(1)

    print(f"Backup written to {backup_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"Target DB: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")

    conn = get_connection()
    cursor = conn.cursor()

    if column_exists(cursor, "resources", "region"):
        print("resources.region already exists — nothing to do.")
        cursor.execute("SELECT COUNT(*) FROM resources WHERE region IS NULL")
        null_count = cursor.fetchone()[0]
        if null_count:
            print(f"NOTE: {null_count} resources row(s) still have region=NULL — "
                  f"if this script already ran once, that's unusual; check manually.")
        cursor.close(); conn.close()
        return

    print("Plan:")
    print("  1. ALTER TABLE resources ADD COLUMN region VARCHAR(20) AFTER account_id")
    print("  2. UPDATE resources r JOIN aws_accounts a ON a.id=r.aws_account_id")
    print("     SET r.region = a.default_region WHERE r.region IS NULL")

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        cursor.close(); conn.close()
        return

    take_backup()

    print("\nApplying...")
    try:
        cursor.execute("ALTER TABLE resources ADD COLUMN region VARCHAR(20) AFTER account_id")
        print("  OK: resources.region added")
        cursor.execute("""
            UPDATE resources r
            JOIN aws_accounts a ON a.id = r.aws_account_id
            SET r.region = a.default_region
            WHERE r.region IS NULL
        """)
        backfilled = cursor.rowcount
        conn.commit()
        print(f"  OK: backfilled region on {backfilled} existing row(s)")
    except mysql.connector.Error as e:
        print(f"\nERROR applying migration: {e}")
        print("MySQL DDL auto-commits per statement — restore from the backup just taken "
              "if you need to undo anything already applied.")
        cursor.close(); conn.close()
        sys.exit(1)

    cursor.execute("SELECT COUNT(*) FROM resources WHERE region IS NULL")
    remaining_null = cursor.fetchone()[0]
    print(f"\nVerification: {remaining_null} resources row(s) still have region=NULL "
          f"(expected: 0, unless an aws_accounts row itself has no default_region set)")

    cursor.close(); conn.close()
    print("\nDone. Restart the service to pick this up: sudo systemctl restart monitoring-hub")


if __name__ == "__main__":
    main()
