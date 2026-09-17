#!/usr/bin/env python3
"""
tools/audit_cross_account_keys.py -- manual, read-only sweep of every
unique key in the live schema, looking for the exact bug shape behind
the 2026-09-16 AuroGov Mumbai/U4RAD incident: a unique key on a raw AWS
identifier column (resource_id and friends) with NO account-scoping
column in the same key. That shape lets two different accounts sharing
an identifier (a stock CloudWatch Logs group name, a repeated backup
plan name, etc.) silently upsert into the SAME row -- no error, no
duplicate ever visible in the data, just quietly wrong numbers.

This is the ad-hoc script from the original 045/046 investigation,
reconstructed and committed here so it's a permanent, versioned part of
the repo instead of a loose file that can vanish from a box between
sessions (it did, on dev, once already). It deliberately does NOT
replace app.collector.integrity_check.check_unscoped_identity_keys(),
which runs automatically every discovery cycle -- that one only knows
about tables already enumerated in _EXPECTED_SCOPED_UNIQUE_KEYS, so it
can't catch a brand-new table introducing this same bug shape for the
first time. This script has no such allowlist: it scans EVERY unique
key in the schema, so it's the right tool to re-run by hand after any
schema change, or periodically as a general health check.

Deliberately does NOT import from app.collector.integrity_check, even
though the two lists below overlap with its _EXPECTED_RESOURCE_ID_WIDTHS
-- that module is imported by app/db.py, whose module-level pool
creation would make this standalone read-only tool silently open a full
pooled connection (with the running app's pool settings, not this
script's one-shot needs) just to read a config dict, and turn any
transient DB hiccup into a confusing pool-init traceback instead of a
clean audit error. Keep IDENTIFIER_COLUMN_NAMES below in sync by hand
with integrity_check.py's _EXPECTED_RESOURCE_ID_WIDTHS if that list
ever changes.

Usage:
    /opt/monitoring-hub/venv/bin/python3 tools/audit_cross_account_keys.py

Read-only. Never modifies the schema or any data. A key flagged here is
a SUSPECT, not a confirmed bug -- exactly like the false positives this
script correctly identifies (metrics/metric_history key off a BIGINT
foreign key to resources.id, already safely scoped through that FK, not
the raw string). Every hit needs a human to check the column's actual
type and what it references before treating it as a real finding.
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Same identifier-column vocabulary as app.collector.integrity_check's
# _EXPECTED_RESOURCE_ID_WIDTHS -- kept as a plain local list rather than
# imported (see module docstring above for why) -- update both places
# together if a new resource_id-shaped column is ever added.
IDENTIFIER_COLUMN_NAMES = {
    "resource_id", "source_resource_id", "target_resource_id",
}

# Columns that, if present in the SAME unique key as an identifier
# column above, mean the key IS properly account-scoped -- no bug.
ACCOUNT_SCOPE_COLUMN_NAMES = {
    "aws_account_id", "account_id", "org_id", "organization_id", "tenant_id",
}

DB_NAME = os.getenv("DB_NAME", "monitoring_hub")


def get_connection():
    import mysql.connector
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=DB_NAME,
        use_pure=True,
        connection_timeout=10,
    )


def scan_unique_keys(cursor):
    """
    Returns (total_index_count, [{table, index_name, columns: [...]}])
    -- total_index_count is every index (unique and non-unique) in the
    schema, matching how the original ad-hoc version of this script
    reported its "Scanned N indexes" line; the suspect list below only
    ever considers unique keys, since a non-unique index can't enforce
    the identity constraint this script is checking for.
    """
    cursor.execute(
        """
        SELECT COUNT(DISTINCT TABLE_NAME, INDEX_NAME)
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
        """
    )
    total = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND NON_UNIQUE = 0
          AND INDEX_NAME != 'PRIMARY'
        ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
        """
    )
    keys = {}
    for table, index_name, column in cursor.fetchall():
        keys.setdefault((table, index_name), []).append(column)

    return total, [
        {"table": table, "index_name": index_name, "columns": columns}
        for (table, index_name), columns in sorted(keys.items())
    ]


def find_suspects(keys):
    suspects = []
    for key in keys:
        cols = set(key["columns"])
        has_identifier = bool(cols & IDENTIFIER_COLUMN_NAMES)
        has_account_scope = bool(cols & ACCOUNT_SCOPE_COLUMN_NAMES)
        if has_identifier and not has_account_scope:
            suspects.append(key)
    return suspects


def main():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        total, keys = scan_unique_keys(cursor)
    finally:
        conn.close()

    suspects = find_suspects(keys)

    print(f"Scanned {total} indexes across {DB_NAME}.")
    print()
    if not suspects:
        print("0 suspect unique keys. All identifier-bearing unique keys are "
              "properly account-scoped.")
        return

    print(f"{len(suspects)} SUSPECT unique key(s) -- identifier column(s) "
          f"with NO account-scoping column in the same key:")
    print()
    for s in suspects:
        print(f"  {s['table']}.{s['index_name']}: ({', '.join(s['columns'])})")
    print()
    print("Each of these should be reviewed by hand -- it does NOT automatically mean")
    print("the same bug exists (some tables may be legitimately global, e.g. a lookup")
    print("catalog), but every one of them CAN silently merge two accounts' rows the")
    print("same way resources.uniq_resource did if the table is meant to be per-account.")
    print()
    print("If the flagged column is a BIGINT foreign key into an already-scoped table")
    print("(e.g. resources.id) rather than the raw AWS identifier string, it's a false")
    print("positive -- check the column's actual type and what it references before")
    print("treating this as a real finding.")


if __name__ == "__main__":
    main()
