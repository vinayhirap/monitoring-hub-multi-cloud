#!/usr/bin/env python3
"""
apply_alert_evaluation_hardening_migration.py — applies
db/migrations/012_alert_evaluation_hardening.sql

Same convention as apply_access_scopes_migration.py: idempotent (safe to
re-run), takes a backup first, applies, verifies.

This migration is additive only — two new nullable/defaulted columns on
`alerts`, one new table (`alert_pending`) — no existing column is dropped
or retyped, and no existing alert row is resolved or deleted by this
migration itself (that's a deliberate design choice — see
db/migrations/008_revert_falsely_resolved_alerts.sql for why silent
resolves on a schema change are exactly what NOT to do here).

Usage:
    python apply_alert_evaluation_hardening_migration.py --dry-run
    python apply_alert_evaluation_hardening_migration.py
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
MIGRATION_SQL_PATH = REPO_ROOT / "db" / "migrations" / "012_alert_evaluation_hardening.sql"
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


def column_exists(conn, table, column) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
        (DB_NAME, table, column),
    )
    count = cursor.fetchone()[0]
    cursor.close()
    return count > 0


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
    backup_path = BACKUP_DIR / f"pre_alert_hardening_{timestamp}.sql"

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
              "This migration is additive-only, so risk is low, but proceed "
              "with that in mind.")
        return None


def apply_migration(conn, dry_run: bool):
    raw_sql = MIGRATION_SQL_PATH.read_text(encoding="utf-8")
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
    ok = True
    for col in ("last_seen_at", "healthy_streak"):
        if not column_exists(conn, "alerts", col):
            print(f"MISSING: alerts.{col}")
            ok = False
        else:
            print(f"OK: alerts.{col} present")
    if not table_exists(conn, "alert_pending"):
        print("MISSING: alert_pending table")
        ok = False
    else:
        print("OK: alert_pending table present")
    if not ok:
        raise MigrationError("verification failed — see MISSING lines above")


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
        already_done = (
            column_exists(conn, "alerts", "last_seen_at")
            and column_exists(conn, "alerts", "healthy_streak")
            and table_exists(conn, "alert_pending")
        )
        if already_done:
            print("Already applied — nothing to do.")
            verify(conn)
            return

        take_backup(args.dry_run)
        apply_migration(conn, args.dry_run)

        if not args.dry_run:
            verify(conn)
            print("\n=== Done. alerts hardened with last_seen_at / healthy_streak, "
                  "alert_pending created. ===")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except (MigrationError, mysql.connector.Error) as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
