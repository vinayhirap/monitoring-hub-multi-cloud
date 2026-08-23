"""
apply_multi_cloud_credentials.py

Applies db/migrations/010_multi_cloud_credentials.sql safely.
Same pattern as apply_multi_cloud_migration.py (009): idempotent
column-existence checks, mysqldump backup before altering, row-count
verification after.

Usage:
    python apply_multi_cloud_credentials.py --dry-run
    python apply_multi_cloud_credentials.py
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

AFFECTED_TABLES = ["aws_accounts"]

COLUMNS_TO_ADD = [
    ("aws_accounts", "client_secret",
     "ALTER TABLE aws_accounts ADD COLUMN client_secret VARCHAR(500) DEFAULT NULL AFTER client_id"),
    ("aws_accounts", "gcp_service_account_key",
     "ALTER TABLE aws_accounts ADD COLUMN gcp_service_account_key TEXT DEFAULT NULL AFTER service_account_email"),
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
        sys.exit(1)

    os.makedirs("db_backups", exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join("db_backups", f"pre_migration_010_{ts}.sql")

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
        cursor.close()
        conn.close()
        sys.exit(1)

    after_counts = row_counts(cursor)
    if before_counts != after_counts:
        print(f"\nWARNING: row counts changed unexpectedly!\n  before: {before_counts}\n  after:  {after_counts}")
    else:
        print(f"\nRow counts unchanged (expected): {after_counts}")

    print("\nVerification — new columns now present:")
    for table, column, _ in to_apply:
        cursor.execute(f"SHOW COLUMNS FROM {table} LIKE %s", (column,))
        row = cursor.fetchone()
        print(f"  {table}.{column}: {row}")

    cursor.close()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
