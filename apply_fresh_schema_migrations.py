#!/usr/bin/env python3
"""
apply_fresh_schema_migrations.py

Committed to the repo 2026-08-26 — this file existed and ran successfully
on the CloudOps_Main / AuroGov Mumbai servers but had never actually been
added to git (`git log -- apply_fresh_schema_migrations.py` returned
nothing before this commit). apply_fresh_schema_migrations_fk_type_fix.py
patches this file and has been committed for a while, so any fresh clone
that ran it was failing with "file not found" the whole time. This commit
also bakes the BIGINT fix directly into the CREATE TABLE below, so
apply_fresh_schema_migrations_fk_type_fix.py is now a no-op safety net on
a fresh clone (it still matters for any existing environment that has an
older, uncommitted copy of this file with the INT bug).

Closes the gaps left after apply_multi_cloud_migration.py (009),
apply_multi_cloud_credentials.py (010 columns), apply_access_scopes_migration.py
(011_access_scopes.sql) and apply_alert_evaluation_hardening_migration.py (012)
have run. Those four scripts exist and are safe/idempotent already — this
script covers the migrations that were never given a proper existence-checked
Python wrapper:

  1. db/migrations/003_metric_catalog_full.sql
     - metric_catalog: namespace / display_service / category / description /
       is_default columns + uniq_catalog_entry(namespace, metric_name) unique
       key + idx_catalog_service / idx_catalog_category indexes.
       (`namespace` may already exist on some DBs — checked individually.)
     - account_metric_selections table.
     NOTE: seed_metric_catalog.py's ON DUPLICATE KEY UPDATE logic depends on
     uniq_catalog_entry existing — without it, seeding silently produces
     duplicate rows instead of upserting.

  2. db/migrations/010_provider_credentials_table.sql
     - provider_credentials table. apply_multi_cloud_credentials.py only adds
       client_secret / gcp_service_account_key columns to aws_accounts; it
       does NOT create this table, but app/credentials.py requires it for
       every Azure/GCP save_credential() / load_credential() call.

  3. db/migrations/005_password_reset_tokens.sql
     - password_reset_tokens table, used by the live
       POST /api/auth/forgot-password and /api/auth/reset-password endpoints.

  4. db/migrations/011_widen_resource_id.sql
     - resources.resource_id VARCHAR(100) -> VARCHAR(512), required for
       Azure ARM resource IDs (120-160+ chars) to insert without truncation.

  5. db/migrations/004_metrics_last_value_only.sql
     - metrics: drop partitioning if present, collapse to latest row per
       (resource_id, metric_name), add uniq_metrics_resource_metric.
       On a fresh/empty table this is a no-op past the existence checks.

Same conventions as the rest of this project's patch scripts:
  - --dry-run prints every statement it would run and changes nothing.
  - Idempotent: every column/table/key is existence-checked first, so it's
    safe to re-run on every deploy (including on a DB that already has some
    of these applied).
  - Takes a mysqldump backup of every affected table that already exists,
    before making any change, into ./db_backups/.
  - No table renames, no drops (of anything other than partitioning), no FK
    changes to existing tables. account_metric_selections / password_reset_tokens
    / provider_credentials are new tables — nothing existing can be corrupted
    by creating them.

Usage:
    python apply_fresh_schema_migrations.py --dry-run
    python apply_fresh_schema_migrations.py
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

AFFECTED_TABLES = [
    "metric_catalog", "account_metric_selections",
    "provider_credentials", "password_reset_tokens",
    "resources", "metrics",
]


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


def column_exists(cursor, table, column) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (DB_NAME, table, column),
    )
    return cursor.fetchone()[0] > 0


def index_exists(cursor, table, index_name) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = %s",
        (DB_NAME, table, index_name),
    )
    return cursor.fetchone()[0] > 0


def column_type(cursor, table, column) -> str:
    cursor.execute(
        "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (DB_NAME, table, column),
    )
    row = cursor.fetchone()
    return row[0] if row else ""


def has_partitions(cursor, table) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.PARTITIONS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND PARTITION_NAME IS NOT NULL",
        (DB_NAME, table),
    )
    return cursor.fetchone()[0] > 0


def take_backup():
    if not shutil.which("mysqldump"):
        print("ERROR: mysqldump not found on PATH. Refusing to apply without a backup.")
        print("       Install the MySQL client tools, or run with --dry-run to preview only.")
        sys.exit(1)

    conn = get_connection()
    cursor = conn.cursor()
    existing = [t for t in AFFECTED_TABLES if table_exists(cursor, t)]
    cursor.close()
    conn.close()

    if not existing:
        print("No affected tables exist yet — nothing to back up.")
        return None

    os.makedirs("db_backups", exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join("db_backups", f"pre_fresh_schema_migrations_{ts}.sql")

    cmd = [
        "mysqldump",
        f"-h{DB_HOST}", f"-P{DB_PORT}", f"-u{DB_USER}", f"-p{DB_PASSWORD}",
        DB_NAME, *existing,
    ]
    with open(backup_path, "w", encoding="utf-8") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        print(f"ERROR: mysqldump failed:\n{result.stderr}")
        sys.exit(1)

    print(f"Backup written to {backup_path}")
    return backup_path


def plan_statements(cursor):
    """Return list of (label, sql) statements that still need to run."""
    plan = []

    # ── 1. metric_catalog columns (003) ──────────────────────────
    mc_columns = [
        ("namespace", "ALTER TABLE metric_catalog ADD COLUMN namespace VARCHAR(100) AFTER service"),
        ("display_service", "ALTER TABLE metric_catalog ADD COLUMN display_service VARCHAR(150) AFTER namespace"),
        ("category", "ALTER TABLE metric_catalog ADD COLUMN category ENUM('core','extended','directory') NOT NULL DEFAULT 'extended' AFTER unit"),
        ("description", "ALTER TABLE metric_catalog ADD COLUMN description VARCHAR(255) AFTER category"),
        ("is_default", "ALTER TABLE metric_catalog ADD COLUMN is_default TINYINT(1) NOT NULL DEFAULT 0 AFTER description"),
    ]
    for col, ddl in mc_columns:
        if not column_exists(cursor, "metric_catalog", col):
            plan.append((f"metric_catalog.{col}", ddl))

    if not index_exists(cursor, "metric_catalog", "uniq_catalog_entry"):
        plan.append((
            "metric_catalog uniq_catalog_entry",
            "ALTER TABLE metric_catalog ADD UNIQUE KEY uniq_catalog_entry (namespace, metric_name)",
        ))
    if not index_exists(cursor, "metric_catalog", "idx_catalog_service"):
        plan.append((
            "metric_catalog idx_catalog_service",
            "ALTER TABLE metric_catalog ADD INDEX idx_catalog_service (service)",
        ))
    if not index_exists(cursor, "metric_catalog", "idx_catalog_category"):
        plan.append((
            "metric_catalog idx_catalog_category",
            "ALTER TABLE metric_catalog ADD INDEX idx_catalog_category (category)",
        ))

    # ── 2. account_metric_selections table (003) ─────────────────
    if not table_exists(cursor, "account_metric_selections"):
        plan.append(("account_metric_selections (table)", """
            CREATE TABLE account_metric_selections (
              id             BIGINT AUTO_INCREMENT PRIMARY KEY,
              aws_account_id BIGINT NOT NULL,
              metric_id      BIGINT NOT NULL,
              enabled        TINYINT(1) NOT NULL DEFAULT 1,
              source         ENUM('template','manual','discovered') NOT NULL DEFAULT 'template',
              created_at     TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at     TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              UNIQUE KEY uniq_account_metric (aws_account_id, metric_id),
              KEY idx_ams_account (aws_account_id),
              KEY idx_ams_metric (metric_id),
              CONSTRAINT fk_ams_account FOREIGN KEY (aws_account_id) REFERENCES aws_accounts(id) ON DELETE CASCADE,
              CONSTRAINT fk_ams_metric  FOREIGN KEY (metric_id)      REFERENCES metric_catalog(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        """))

    # ── 3. provider_credentials table (010) ───────────────────────
    if not table_exists(cursor, "provider_credentials"):
        plan.append(("provider_credentials (table)", """
            CREATE TABLE provider_credentials (
              -- aws_account_id must match aws_accounts.id's type (BIGINT) --
              -- fix: 2026-08-26, was INT, which fails FK creation with
              -- "Referencing column ... and referenced column ... are
              -- incompatible" (error 3780) and blocked every later
              -- statement in this script's single-loop apply.
              aws_account_id     BIGINT NOT NULL PRIMARY KEY,
              provider           ENUM('azure','gcp') NOT NULL,
              credential_ref     VARCHAR(64) NOT NULL,
              secret_encrypted   MEDIUMBLOB NOT NULL,
              created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              CONSTRAINT fk_provider_credentials_account
                FOREIGN KEY (aws_account_id) REFERENCES aws_accounts(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        """))

    # ── 4. password_reset_tokens table (005) ───────────────────────
    if not table_exists(cursor, "password_reset_tokens"):
        plan.append(("password_reset_tokens (table)", """
            CREATE TABLE password_reset_tokens (
              id         BIGINT AUTO_INCREMENT PRIMARY KEY,
              user_id    BIGINT NOT NULL,
              token      VARCHAR(255) NOT NULL,
              expires_at DATETIME NOT NULL,
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              UNIQUE KEY uniq_prt_token (token),
              KEY idx_prt_user (user_id),
              CONSTRAINT fk_prt_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """))

    # ── 5. resources.resource_id widen (011_widen_resource_id) ────
    current_type = column_type(cursor, "resources", "resource_id")
    if current_type and current_type.lower() != "varchar(512)":
        plan.append((
            "resources.resource_id widen -> VARCHAR(512)",
            "ALTER TABLE resources MODIFY COLUMN resource_id VARCHAR(512) NOT NULL",
        ))

    # ── 6. metrics last-value-only (004) ───────────────────────────
    if table_exists(cursor, "metrics"):
        if has_partitions(cursor, "metrics"):
            plan.append(("metrics (remove partitioning)", "ALTER TABLE metrics REMOVE PARTITIONING"))
        if not index_exists(cursor, "metrics", "uniq_metrics_resource_metric"):
            # Safe here even though normally paired with a dedup DELETE: on a
            # fresh table there are no duplicate rows to collide on, and on
            # an existing table we intentionally do NOT delete data — if this
            # ALTER fails because of live duplicates, that means metrics.py's
            # own upsert logic isn't dedup'd yet and needs the DELETE step
            # from 004_metrics_last_value_only.sql run manually first (or
            # run apply_metrics_dedup_fix.py, which does the DELETE + this
            # same ALTER together).
            plan.append((
                "metrics uniq_metrics_resource_metric",
                "ALTER TABLE metrics ADD UNIQUE KEY uniq_metrics_resource_metric (resource_id, metric_name)",
            ))

    return plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"Target DB: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    print()

    conn = get_connection()
    cursor = conn.cursor()

    plan = plan_statements(cursor)

    if not plan:
        print("Nothing to do — all migrations already applied.")
        cursor.close()
        conn.close()
        return

    print(f"{len(plan)} statement(s) to apply:")
    for label, ddl in plan:
        print(f"  + {label}")

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        cursor.close()
        conn.close()
        return

    take_backup()

    print("\nApplying...")
    applied = []
    try:
        for label, ddl in plan:
            cursor.execute(ddl)
            applied.append(label)
            print(f"  OK: {label}")
        conn.commit()
    except mysql.connector.Error as e:
        print(f"\nERROR applying '{label}': {e}")
        print(f"Statements successfully applied before the failure: {applied}")
        print("MySQL DDL auto-commits per statement. Restore from the backup just taken "
              "if you need to undo anything already applied.")
        cursor.close()
        conn.close()
        sys.exit(1)

    print("\nVerification:")
    for t in ("account_metric_selections", "provider_credentials", "password_reset_tokens"):
        print(f"  table {t}: {'present' if table_exists(cursor, t) else 'MISSING'}")
    for col in ("namespace", "display_service", "category", "description", "is_default"):
        print(f"  metric_catalog.{col}: {'present' if column_exists(cursor, 'metric_catalog', col) else 'MISSING'}")

    cursor.close()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
