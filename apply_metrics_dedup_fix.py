#!/usr/bin/env python3
"""
apply_metrics_dedup_fix.py

One-time fix for duplicate rows in `metrics` (resource_id, metric_name)
before applying the uniq_metrics_resource_metric unique key.

FIX (2026-08-26, AuroGov Mumbai incident): this script previously ran the
`ALTER TABLE ... ADD UNIQUE KEY` unconditionally, with no existence check —
unlike every other apply_*.py script in this project. On a DB where the key
was already applied (e.g. via an earlier partial run, or copied in from
another server's state), re-running this script failed with:

  ERROR 1061 (42000): Duplicate key name 'uniq_metrics_resource_metric'

Both setup.sh and update.sh document this script as "no-op once already
applied" / "safe to re-run every deploy" — that was only true for the
DELETE step, not the ALTER step. This version adds an index_exists() check
so the ALTER is skipped (and reported) when the key is already present,
matching the idempotency pattern used everywhere else in this project.

Uses the `mysql` CLI client via subprocess (no Python DB driver dependency).
Reads DB credentials directly from the app's .env file rather than relying
on inherited shell environment variables — `sudo -u hcsadmin` starts a fresh
shell, so anything sourced under your own login (e.g. `source .env`) does
not carry through the sudo boundary.

Usage:
    python3 apply_metrics_dedup_fix.py --dry-run
    python3 apply_metrics_dedup_fix.py
"""

import os
import sys
import subprocess
import datetime
import argparse

DB_HOST = "127.0.0.1"
DB_PORT = "3306"
DB_NAME = "monitoring_hub"
DB_USER = "monitor"

RECENCY_CANDIDATES = ["recorded_at", "timestamp", "ts", "updated_at", "created_at"]
UNIQUE_KEY_NAME = "uniq_metrics_resource_metric"

# Same search order apply_fresh_schema_migrations.py uses: script directory first,
# since it's normally run from /opt/monitoring-hub/app where .env lives.
ENV_FILE_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    "/opt/monitoring-hub/app/.env",
    "/opt/monitoring-hub/.env",
]

PASSWORD_KEYS = ("MONITOR_DB_PASSWORD", "DB_PASSWORD")


def load_env_password():
    # Env var takes precedence if it's actually set (e.g. this script is run
    # non-interactively with it exported some other way).
    for key in PASSWORD_KEYS:
        if os.environ.get(key):
            return os.environ[key]

    for path in ENV_FILE_CANDIDATES:
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key in PASSWORD_KEYS and value:
                    return value
    return None


DB_PASS = load_env_password()

MYSQL_BASE = ["mysql", f"-u{DB_USER}", "-h", DB_HOST, "-P", DB_PORT, "-N", "-B"]
if DB_PASS:
    MYSQL_BASE.append(f"-p{DB_PASS}")


def run_sql(sql, database=DB_NAME):
    """Run a SQL statement via the mysql CLI, return stdout (tab-separated rows)."""
    cmd = MYSQL_BASE + [database, "-e", sql]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"mysql error: {result.stderr.strip()}")
    return result.stdout


def detect_recency_column():
    out = run_sql("SHOW COLUMNS FROM metrics")
    cols = {line.split("\t")[0] for line in out.strip().splitlines() if line}
    for cand in RECENCY_CANDIDATES:
        if cand in cols:
            return cand
    return None


def index_exists(index_name):
    out = run_sql(f"SHOW INDEX FROM metrics WHERE Key_name = '{index_name}'")
    return bool(out.strip())


def build_keep_ids_subquery(order_col):
    # Extra derived-table layer forces MySQL to materialize the result before
    # the DELETE touches `metrics` again — avoids error 1093
    # "You can't specify target table 'metrics' for update in FROM clause".
    return f"""
        SELECT id FROM (
            SELECT m1.id
            FROM metrics m1
            INNER JOIN (
                SELECT resource_id, metric_name, MAX({order_col}) AS max_val
                FROM metrics
                GROUP BY resource_id, metric_name
            ) m2
              ON m1.resource_id = m2.resource_id
             AND m1.metric_name = m2.metric_name
             AND m1.{order_col} = m2.max_val
            GROUP BY m1.resource_id, m1.metric_name
        ) AS keep_ids
    """


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"Target DB: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    if not DB_PASS:
        print("WARNING: no DB password found via env vars or .env file — "
              "connection will likely fail with access denied.")

    key_already_present = index_exists(UNIQUE_KEY_NAME)
    if key_already_present:
        print(f"'{UNIQUE_KEY_NAME}' already exists on metrics — skipping the ALTER step.")

    recency_col = detect_recency_column()
    if recency_col:
        order_col = recency_col
        print(f"Using '{recency_col}' to determine newest row per group.")
    else:
        order_col = "id"
        print("No recency column found (recorded_at/timestamp/ts/updated_at/created_at) "
              "— falling back to MAX(id) as 'newest' (highest auto-increment = most recently inserted).")

    dup_out = run_sql("""
        SELECT resource_id, metric_name, COUNT(*) c
        FROM metrics
        GROUP BY resource_id, metric_name
        HAVING c > 1
    """)
    dup_lines = [l for l in dup_out.strip().splitlines() if l]
    dup_groups = [tuple(l.split("\t")) for l in dup_lines]
    print(f"{len(dup_groups)} (resource_id, metric_name) group(s) have duplicate rows.")
    if dup_groups:
        examples = dup_groups[:10]
        print(f"Examples: {examples}")

    total_rows = int(run_sql("SELECT COUNT(*) FROM metrics").strip())

    keep_ids_subquery = build_keep_ids_subquery(order_col)
    keep_count = int(run_sql(f"SELECT COUNT(*) FROM ({keep_ids_subquery}) AS x").strip())
    would_delete = total_rows - keep_count

    print("Plan:")
    print("  1. DELETE every metrics row NOT IN the 'keep newest per group' set")
    if key_already_present:
        print(f"  2. (skipped — {UNIQUE_KEY_NAME} already exists)")
    else:
        print(f"  2. ALTER TABLE metrics ADD UNIQUE KEY {UNIQUE_KEY_NAME} (resource_id, metric_name)")
    print(f"  Total rows currently in metrics: {total_rows}")
    print(f"  Rows that WOULD be deleted: {would_delete}")

    if args.dry_run:
        print("--dry-run: no changes made.")
        return

    if would_delete == 0 and key_already_present:
        print("Nothing to do — no duplicates to delete and the unique key already exists.")
        return

    # Backup before touching data
    os.makedirs("db_backups", exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"db_backups/pre_metrics_dedup_{ts}.sql"
    dump_cmd = ["mysqldump", f"-u{DB_USER}"]
    if DB_PASS:
        dump_cmd.append(f"-p{DB_PASS}")
    dump_cmd += ["-h", DB_HOST, "-P", DB_PORT, DB_NAME, "metrics"]
    with open(backup_path, "w") as f:
        result = subprocess.run(dump_cmd, stdout=f, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"ERROR: backup failed: {result.stderr.strip()}")
        sys.exit(1)
    print(f"Backup written to {backup_path}")

    print("Applying...")
    try:
        if would_delete > 0:
            delete_sql = f"DELETE FROM metrics WHERE id NOT IN ({keep_ids_subquery})"
            run_sql(delete_sql)

        if not key_already_present:
            alter_sql = (
                "ALTER TABLE metrics "
                f"ADD UNIQUE KEY {UNIQUE_KEY_NAME} (resource_id, metric_name)"
            )
            run_sql(alter_sql)
            print(f"Added unique key {UNIQUE_KEY_NAME}.")

        remaining = int(run_sql("SELECT COUNT(*) FROM metrics").strip())
        print(f"Deleted {total_rows - remaining} row(s). {remaining} row(s) remain.")
        print("Done.")
    except Exception as e:
        print(f"ERROR: {e}")
        print("Restore from the backup just taken if you need to undo anything already applied.")
        sys.exit(1)


if __name__ == "__main__":
    main()
