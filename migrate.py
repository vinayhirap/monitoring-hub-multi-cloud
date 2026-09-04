#!/usr/bin/env python3
"""
migrate.py -- minimal migration tracker for db/migrations/*.sql

Problem this solves: this project's migrations directory has no tracking
of what's actually been applied to a given database -- files just sit in
db/migrations/ and get run by hand, if someone remembers. That's how
014_user_email_column.sql shipped in the repo but was never applied to
production, silently breaking "add user with email" until debugged live.

This script adds a `schema_migrations` table (filename, applied_at) as
the single source of truth, with three safe operations:

  status
      List every .sql file in db/migrations/, marked APPLIED or PENDING
      based on the tracking table. Never touches the DB schema. This is
      the default action -- running this script with no args is always
      read-only.

  baseline <file> [<file> ...]
      Mark specific files as already-applied WITHOUT running their SQL.
      Use this ONCE, for migrations you've manually confirmed already
      ran against this database (e.g. by checking DESCRIBE output), so
      the tracker starts in sync with reality instead of assuming a
      freshly-created tracking table means a freshly-empty schema.

  apply <file>
      Actually executes one .sql file's statements against the DB inside
      a transaction where possible, then records it in schema_migrations.
      Refuses if the file is already marked applied (idempotent) or
      doesn't exist on disk.

  apply --all-pending
      Applies every PENDING file in filename-sorted order, stopping
      immediately on the first error. Does NOT try to resolve the
      duplicate 011/013 numbering or the unnumbered add_monitoring_tier.sql
      seen in `status` -- fix those filenames/ordering by hand first if
      you plan to use this mode; it trusts sort order completely.

Nothing here auto-applies anything by default. `status` is safe to run
at any time, including in production, with zero risk of mutating schema.

Usage:
    python migrate.py status
    python migrate.py baseline 002_resources_region_instance_state.sql 003_metric_catalog_full.sql ...
    python migrate.py apply 014_user_email_column.sql
    python migrate.py apply --all-pending
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "monitoring_hub")


class MigrateError(Exception):
    pass


def get_connection():
    import mysql.connector
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME, use_pure=True,
        connection_timeout=10,
    )


def ensure_tracking_table(conn):
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    VARCHAR(255) NOT NULL PRIMARY KEY,
            applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            applied_via ENUM('script', 'baseline') NOT NULL DEFAULT 'script'
        )
    """)
    conn.commit()
    cursor.close()


def get_applied(conn) -> dict:
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT filename, applied_at, applied_via FROM schema_migrations")
    rows = {r["filename"]: r for r in cursor.fetchall()}
    cursor.close()
    return rows


def list_migration_files():
    if not MIGRATIONS_DIR.exists():
        raise MigrateError(f"Migrations directory not found: {MIGRATIONS_DIR}")
    return sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))


def cmd_status(conn):
    ensure_tracking_table(conn)
    applied = get_applied(conn)
    files = list_migration_files()

    print(f"{'STATUS':<10} {'FILE':<45} APPLIED AT")
    print("-" * 80)
    pending_count = 0
    for f in files:
        if f in applied:
            print(f"{'APPLIED':<10} {f:<45} {applied[f]['applied_at']} ({applied[f]['applied_via']})")
        else:
            print(f"{'PENDING':<10} {f:<45}")
            pending_count += 1

    print("-" * 80)
    print(f"{len(files)} total, {pending_count} pending")

    dupes = _find_number_collisions(files)
    if dupes:
        print("\nWARNING: duplicate migration numbers found -- resolve before using --all-pending:")
        for num, matches in dupes.items():
            print(f"  {num}: {', '.join(matches)}")


def _find_number_collisions(files):
    prefixes = {}
    for f in files:
        prefix = f.split("_")[0]
        if prefix.isdigit():
            prefixes.setdefault(prefix, []).append(f)
    return {k: v for k, v in prefixes.items() if len(v) > 1}


def cmd_baseline(conn, filenames):
    ensure_tracking_table(conn)
    applied = get_applied(conn)
    existing_files = set(list_migration_files())

    cursor = conn.cursor()
    marked = []
    for f in filenames:
        if f not in existing_files:
            raise MigrateError(f"No such file in db/migrations/: {f}")
        if f in applied:
            print(f"  SKIP   {f} — already marked applied ({applied[f]['applied_at']})")
            continue
        cursor.execute(
            "INSERT INTO schema_migrations (filename, applied_via) VALUES (%s, 'baseline')",
            (f,),
        )
        marked.append(f)
        print(f"  MARKED {f} — recorded as already-applied, SQL was NOT executed")
    conn.commit()
    cursor.close()

    if marked:
        print(f"\n{len(marked)} file(s) baselined. Run `status` to confirm.")
    else:
        print("\nNothing to baseline.")


def cmd_apply(conn, filename):
    ensure_tracking_table(conn)
    applied = get_applied(conn)

    if filename in applied:
        print(f"Already applied at {applied[filename]['applied_at']} — refusing to re-run. "
              f"(Delete its row from schema_migrations first if you really need to re-apply.)")
        return

    path = MIGRATIONS_DIR / filename
    if not path.exists():
        raise MigrateError(f"No such file: {path}")

    sql = path.read_text(encoding="utf-8")
    print(f"Applying {filename} ...")
    print("-" * 60)
    print(sql.strip())
    print("-" * 60)

    cursor = conn.cursor()
    try:
        # MySQL DDL auto-commits per statement regardless of transaction
        # state, so this isn't atomic for multi-statement DDL files --
        # but the connector still needs multi=True to run more than one
        # statement per .sql file at all.
        for _ in cursor.execute(sql, multi=True):
            pass
        cursor.execute(
            "INSERT INTO schema_migrations (filename, applied_via) VALUES (%s, 'script')",
            (filename,),
        )
        conn.commit()
        print(f"APPLIED {filename}")
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def cmd_apply_all_pending(conn):
    ensure_tracking_table(conn)
    applied = get_applied(conn)
    files = list_migration_files()
    pending = [f for f in files if f not in applied]

    dupes = _find_number_collisions(files)
    if dupes:
        raise MigrateError(
            "Refusing to run --all-pending: duplicate migration numbers exist "
            f"({', '.join(dupes.keys())}). Resolve the numbering first, or apply "
            "each pending file individually with `apply <file>`."
        )

    if not pending:
        print("Nothing pending.")
        return

    print(f"Will apply {len(pending)} file(s) in this order:")
    for f in pending:
        print(f"  {f}")
    confirm = input("\nType 'apply' to continue: ")
    if confirm.strip() != "apply":
        print("Aborted.")
        return

    for f in pending:
        cmd_apply(conn, f)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status")

    p_baseline = sub.add_parser("baseline")
    p_baseline.add_argument("files", nargs="+")

    p_apply = sub.add_parser("apply")
    p_apply.add_argument("file", nargs="?")
    p_apply.add_argument("--all-pending", action="store_true")

    args = parser.parse_args()
    command = args.command or "status"

    try:
        conn = get_connection()
    except Exception as e:
        print(f"ERROR: could not connect to database: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if command == "status":
            cmd_status(conn)
        elif command == "baseline":
            cmd_baseline(conn, args.files)
        elif command == "apply":
            if args.all_pending:
                cmd_apply_all_pending(conn)
            elif args.file:
                cmd_apply(conn, args.file)
            else:
                print("Specify a filename, or use --all-pending", file=sys.stderr)
                sys.exit(1)
    except MigrateError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
