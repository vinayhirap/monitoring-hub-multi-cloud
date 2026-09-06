#!/usr/bin/env python3
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
