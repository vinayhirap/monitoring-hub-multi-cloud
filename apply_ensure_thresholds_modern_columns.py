#!/usr/bin/env python3
"""
apply_ensure_thresholds_modern_columns.py

FIXES A REAL "FRESH INSTALL IS BROKEN" BUG -- the same class as
apply_ensure_metric_catalog_base_table.py (see that script's own
docstring), found while auditing scripts/sync_cloudwatch_alarm_
thresholds.py against db/migrations/.

db/schema.sql's `thresholds` table is the ORIGINAL, ancient shape:
    id, metric_name, environment_id, warning, critical
The table this app actually runs on today -- read/written by
app/api/settings.py, app/collector/alert_evaluator.py, and every
script in scripts/security/ and scripts/ that touches thresholds --
has a completely different shape: aws_account_id, resource_type,
metric_id, warning_value, critical_value, comparison,
evaluation_period, enabled (plus use_dynamic/dynamic_k, which
db/migrations/020_metric_baseline_dynamic_thresholds.sql DOES add via
`ALTER TABLE thresholds ADD COLUMN use_dynamic ... AFTER enabled` --
note that ALTER already assumes `enabled` exists).

Exhaustively confirmed (grep across every db/migrations/*.sql file for
"comparison", "warning_value", "critical_value", and any
"ALTER TABLE thresholds" beyond 020's use_dynamic/dynamic_k addition):
nothing in this repository ever adds aws_account_id/resource_type/
metric_id/warning_value/critical_value/comparison/evaluation_period/
enabled to this table. It exists in every environment this app
currently runs on only because someone added these columns manually
at some point, exactly like metric_catalog's missing base CREATE TABLE
-- outside anything tracked in git.

On a genuinely fresh clone + fresh database, db/schema.sql creates the
ANCIENT thresholds shape, then migration 020 (the first migration to
touch this table) fails immediately with "Unknown column 'enabled' in
'thresholds'" -- and even if it didn't, every route/script that reads
warning_value/critical_value/comparison/etc. would fail the same way.

This script must run before db/migrations/020_metric_baseline_dynamic_
thresholds.sql (first in the run_migration list, in all three of
setup.sh/deploy.sh/update.sh, alongside the metric_catalog fix this
mirrors). It only ADDs columns/keys that are missing -- every
ADD COLUMN uses IF NOT EXISTS (the same syntax db/migrations/003_
metric_catalog_full.sql already uses elsewhere in this repo), and the
unique key uses the information_schema-existence-check +
PREPARE/EXECUTE idiom db/migrations/045_fix_resources_cross_account_
unique_key.sql already established for exactly this "idempotent
ADD KEY" need (plain "ADD UNIQUE KEY IF NOT EXISTS" isn't supported by
MySQL 8 the way ADD COLUMN IF NOT EXISTS is).

Types/widths were chosen to match this exact codebase's own established
conventions, not guessed in isolation: resource_type VARCHAR(50) matches
metric_catalog.service's own width (they're compared directly, e.g. in
sync_cloudwatch_alarm_thresholds.py's _lookup_metric_id: "WHERE
service=%s" using a resource_type value); metric_id/aws_account_id
BIGINT match every other such column in this schema; comparison
VARCHAR(5) fits ">"/">="/"<"/"<=" with margin; evaluation_period INT
DEFAULT 5 and enabled TINYINT(1) DEFAULT 1 match the literal defaults
scripts/sync_cloudwatch_alarm_thresholds.py's _upsert_threshold already
inserts for a brand-new row ("VALUES (%s,%s,%s,%s,%s,%s,5,1)").

Idempotent: on every environment that already has these columns (i.e.
everywhere this app currently runs), every clause below is a silent
no-op.

Usage:
    python3 apply_ensure_thresholds_modern_columns.py --dry-run
    python3 apply_ensure_thresholds_modern_columns.py
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
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME", "monitoring_hub")

if not DB_PASSWORD:
    print("DB_PASSWORD is not set -- there is no default. Set it in .env.", file=sys.stderr)
    sys.exit(1)

ADD_COLUMNS_SQL = """
    ALTER TABLE thresholds
      ADD COLUMN IF NOT EXISTS aws_account_id     BIGINT       NULL AFTER id,
      ADD COLUMN IF NOT EXISTS resource_type       VARCHAR(50)  NULL AFTER aws_account_id,
      ADD COLUMN IF NOT EXISTS metric_id           BIGINT       NULL AFTER metric_name,
      ADD COLUMN IF NOT EXISTS warning_value       DOUBLE       NULL AFTER metric_id,
      ADD COLUMN IF NOT EXISTS critical_value      DOUBLE       NULL AFTER warning_value,
      ADD COLUMN IF NOT EXISTS comparison          VARCHAR(5)   NULL AFTER critical_value,
      ADD COLUMN IF NOT EXISTS evaluation_period   INT          NOT NULL DEFAULT 5 AFTER comparison,
      ADD COLUMN IF NOT EXISTS enabled             TINYINT(1)   NOT NULL DEFAULT 1 AFTER evaluation_period
"""

# db/schema.sql's ANCIENT thresholds shape declared metric_name/
# environment_id/warning/critical all NOT NULL with no default. The
# app's current INSERT statements (e.g. _upsert_threshold above) never
# provide any of the four -- on every environment this app actually
# runs on today, these columns must already have been relaxed to allow
# NULL as part of the same undocumented manual change that added the
# modern columns above (otherwise every INSERT would already be
# failing there right now, which it isn't). Relaxed here too so a
# fresh install's INSERTs don't immediately fail on
# "Field 'metric_name' doesn't have a default value" -- a MODIFY
# COLUMN to a type/nullability it may already have is a safe no-op.
RELAX_LEGACY_COLUMNS_SQL = """
    ALTER TABLE thresholds
      MODIFY COLUMN metric_name   VARCHAR(100) NULL,
      MODIFY COLUMN environment_id BIGINT      NULL,
      MODIFY COLUMN warning        DOUBLE      NULL,
      MODIFY COLUMN critical       DOUBLE      NULL
"""

UNIQUE_KEY_NAME = "uniq_threshold_scope"
ADD_UNIQUE_KEY_SQL = (
    f"ALTER TABLE thresholds ADD UNIQUE KEY {UNIQUE_KEY_NAME} "
    f"(aws_account_id, resource_type, metric_id)"
)


def get_connection():
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME, use_pure=True,
    )


def unique_key_exists(cursor) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'thresholds' AND INDEX_NAME = %s",
        (DB_NAME, UNIQUE_KEY_NAME),
    )
    return cursor.fetchone()[0] > 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    cursor = conn.cursor()
    try:
        if args.dry_run:
            print("[dry-run] Would run (each clause a silent no-op if already present):")
            print(ADD_COLUMNS_SQL)
            print(RELAX_LEGACY_COLUMNS_SQL)
            if not unique_key_exists(cursor):
                print(ADD_UNIQUE_KEY_SQL)
            else:
                print(f"-- {UNIQUE_KEY_NAME} already exists, would skip")
            return

        cursor.execute(ADD_COLUMNS_SQL)
        conn.commit()
        print("thresholds: modern columns ensured (aws_account_id, resource_type, "
              "metric_id, warning_value, critical_value, comparison, "
              "evaluation_period, enabled) -- no-op for any that already existed.")

        cursor.execute(RELAX_LEGACY_COLUMNS_SQL)
        conn.commit()
        print("thresholds: legacy metric_name/environment_id/warning/critical "
              "columns relaxed to NULL (harmless no-op if already relaxed).")

        if unique_key_exists(cursor):
            print(f"thresholds.{UNIQUE_KEY_NAME} already exists, skipping.")
        else:
            cursor.execute(ADD_UNIQUE_KEY_SQL)
            conn.commit()
            print(f"thresholds: added {UNIQUE_KEY_NAME} unique key -- required for "
                  f"every INSERT ... ON DUPLICATE KEY UPDATE against this table "
                  f"(e.g. POST /api/settings/thresholds, sync_cloudwatch_alarm_"
                  f"thresholds.py's _upsert_threshold) to update an existing row "
                  f"instead of silently inserting a duplicate.")
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()
