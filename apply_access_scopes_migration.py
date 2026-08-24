#!/usr/bin/env python3
"""
apply_access_scopes_migration.py — applies db/migrations/011_access_scopes.sql

Follows the same convention as the existing multi-cloud migration
wrapper (009/010): check current state first (idempotent — safe to
re-run), take a backup before touching anything, apply, then verify.

This migration is purely additive — one new table, zero ALTERs on
existing tables — so the blast radius if something goes wrong is
"the new table didn't get created", not "an existing table got
corrupted". Backup is still taken because it's cheap and this is a
production account database.

Reads DB connection settings the same way the rest of the app does:
DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME from .env (or real
environment variables, which take precedence).

Usage:
    python apply_access_scopes_migration.py --dry-run
    python apply_access_scopes_migration.py
"""
import argparse
import datetime
import os
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import mysql.connector

REPO_ROOT = Path(__file__).resolve().parent
MIGRATION_SQL_PATH = REPO_ROOT / "db" / "migrations" / "011_access_scopes.sql"
BACKUP_DIR = REPO_ROOT / "db" / "backups"

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "monitoring_hub")


class MigrationError(Exception):
    pass


def connect():
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME,
    )


def table_exists(conn, table_name: str) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        (DB_NAME, table_name),
    )
    count = cursor.fetchone()[0]
    cursor.close()
    return count > 0


def take_backup(dry_run: bool) -> Path | None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"pre_access_scopes_{timestamp}.sql"

    if dry_run:
        print(f"[DRY RUN] would back up {DB_NAME} to {backup_path}")
        return None

    mysqldump_cmd = [
        "mysqldump",
        f"--host={DB_HOST}", f"--port={DB_PORT}", f"--user={DB_USER}",
    ]
    if DB_PASSWORD:
        mysqldump_cmd.append(f"--password={DB_PASSWORD}")
    mysqldump_cmd += ["--default-character-set=utf8mb4", DB_NAME]

    try:
        with open(backup_path, "w", encoding="utf-8") as f:
            result = subprocess.run(mysqldump_cmd, stdout=f, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            raise MigrationError(f"mysqldump failed: {result.stderr}")
        print(f"Backup written: {backup_path}")
        return backup_path
    except FileNotFoundError:
        print("WARNING: 'mysqldump' not found on PATH — skipping backup. "
              "This migration is additive-only (new table, no ALTERs), "
              "so risk is low, but proceed with that in mind.")
        return None


def apply_migration(conn, dry_run: bool):
    raw_sql = MIGRATION_SQL_PATH.read_text(encoding="utf-8")
    # Strip SQL line-comments BEFORE splitting on ';' — the leading
    # comment block in this file has no semicolons in it, so a naive
    # "split on ; then drop segments that start with --" leaves the
    # comment block glued onto the real statement as one blob.
    code_lines = [
        line for line in raw_sql.splitlines()
        if not line.strip().startswith("--")
    ]
    sql = "\n".join(code_lines)
    statements = [s.strip() for s in sql.split(";") if s.strip()]

    if dry_run:
        print(f"[DRY RUN] would execute {len(statements)} statement(s) from {MIGRATION_SQL_PATH.name}")
        return

    cursor = conn.cursor()
    for stmt in statements:
        cursor.execute(stmt)
    conn.commit()
    cursor.close()
    print(f"Applied {MIGRATION_SQL_PATH.name}")


def verify(conn):
    cursor = conn.cursor()
    cursor.execute("DESCRIBE access_scopes")
    rows = cursor.fetchall()
    cursor.close()
    expected_columns = {
        "id", "user_id", "cloud", "account_ref_id", "regions",
        "resource_groups", "resource_types", "resource_ids",
        "granted_by", "created_at", "updated_at",
    }
    actual_columns = {row[0] for row in rows}
    missing = expected_columns - actual_columns
    if missing:
        raise MigrationError(f"access_scopes is missing expected columns: {missing}")
    print(f"Verified: access_scopes has all {len(expected_columns)} expected columns.")
    for row in rows:
        print(f"    {row[0]:<20} {row[1]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not MIGRATION_SQL_PATH.exists():
        print(f"ABORTED: migration file not found: {MIGRATION_SQL_PATH}", file=sys.stderr)
        sys.exit(1)

    try:
        conn = connect()
    except mysql.connector.Error as e:
        print(f"ABORTED: could not connect to database ({DB_HOST}:{DB_PORT}/{DB_NAME}): {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if table_exists(conn, "access_scopes"):
            print("access_scopes already exists — migration already applied. Nothing to do.")
            verify(conn)
            return

        take_backup(args.dry_run)
        apply_migration(conn, args.dry_run)

        if not args.dry_run:
            verify(conn)
            print("\n=== Done. access_scopes table created. ===")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except (MigrationError, mysql.connector.Error) as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
