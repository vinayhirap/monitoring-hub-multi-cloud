#!/usr/bin/env python3
"""
apply_permission_rbac_migration.py — executes
db/migrations/015_permissions_rbac.sql against the database.

apply_permission_rbac_system.py only WRITES this SQL file to disk (it's
a code generator for app/auth/permissions.py etc.) -- it deliberately
does not touch the DB itself. This script is the missing other half:
same idempotent, .env-driven pattern every other apply_*.py migration
in this repo uses (table-exists check, safe to re-run every deploy).

Usage:
    python apply_permission_rbac_migration.py --dry-run
    python apply_permission_rbac_migration.py
"""
import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parent
SQL_PATH = REPO_ROOT / "db" / "migrations" / "015_permissions_rbac.sql"

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "monitor")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "monitoring_hub")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not SQL_PATH.exists():
        print(f"MISSING: {SQL_PATH} -- run apply_permission_rbac_system.py first.")
        sys.exit(1)

    try:
        import mysql.connector
    except ImportError:
        print("mysql-connector-python is not installed. Install it (pip install "
              "mysql-connector-python) or apply the SQL file manually:")
        print(f"  mysql -u{DB_USER} -p {DB_NAME} < {SQL_PATH}")
        sys.exit(1)

    conn = mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME,
    )
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (DB_NAME, "permissions"),
        )
        already = cur.fetchone()[0] > 0

        if already:
            print("permissions table already exists -- 015 migration already applied. Nothing to do.")
        else:
            sql_text = SQL_PATH.read_text(encoding="utf-8")
            code_lines = [
                line for line in sql_text.splitlines()
                if not line.strip().startswith("--")
            ]
            sql = "\n".join(code_lines)
            statements = [s.strip() for s in sql.split(";") if s.strip()]

            if args.dry_run:
                print(f"[DRY RUN] would execute {len(statements)} statement(s) from {SQL_PATH.name}")
            else:
                for stmt in statements:
                    cur.execute(stmt)
                conn.commit()
                print(f"Applied {SQL_PATH.name}")

        if not args.dry_run:
            cur.execute("SELECT role, COUNT(*) FROM role_permissions GROUP BY role")
            rows = cur.fetchall()
            if rows:
                for role, count in rows:
                    print(f"  role_permissions: {role} -> {count} permission(s)")
            else:
                print("  WARNING: role_permissions has zero rows -- migration may not have seeded correctly.")
        cur.close()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
