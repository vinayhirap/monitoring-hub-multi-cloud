#!/usr/bin/env python3
"""
check_catalog_metrics.py

Diagnoses why AWS/EBS VolumeReadBytes / VolumeWriteBytes are missing from
the generated YACE config for an account. Two possible causes, checked
separately:

  1. metric_catalog gap  - the rows don't exist in metric_catalog at all
     for namespace 'AWS/EBS', so no account could ever enable them.
  2. selection gap        - the rows exist in metric_catalog, but this
     account's account_metric_selections row for them is missing or
     enabled=0, so the yace-config generator skips them for this account.

Must be run from the repo root (same folder as app/db.py), with the venv
activated, so app.db.get_connection() resolves the same MySQL connection
the running app uses.

Usage:
    python check_catalog_metrics.py [account_id]

    account_id is optional. If omitted, prints the metric_catalog check
    only (namespace-wide, not account-specific). If given, also checks
    that account's selection state.
"""
import sys
from dotenv import load_dotenv
load_dotenv()  # must happen before importing app.db, same rule as main.py

from app.db import get_connection

TARGET_METRICS = ["VolumeReadBytes", "VolumeWriteBytes"]


def main():
    account_id = int(sys.argv[1]) if len(sys.argv) > 1 else None

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    print("=== Step 1: metric_catalog check (namespace = 'AWS/EBS') ===\n")
    cur.execute("""
        SELECT id, service, namespace, display_service, metric_name,
               statistic, unit, category, is_default
        FROM metric_catalog
        WHERE namespace = 'AWS/EBS'
        ORDER BY metric_name
    """)
    catalog_rows = cur.fetchall()

    if not catalog_rows:
        print("No AWS/EBS rows in metric_catalog at all. That's the root cause -"
              " the EBS namespace has never been seeded into the catalog.")
        cur.close(); conn.close()
        return

    found = {}
    for row in catalog_rows:
        marker = " <-- TARGET" if row["metric_name"] in TARGET_METRICS else ""
        print(f'  id={row["id"]:<5} metric_name={row["metric_name"]:<22} '
              f'statistic={row["statistic"]:<10} category={row["category"]:<10} '
              f'is_default={row["is_default"]}{marker}')
        if row["metric_name"] in TARGET_METRICS:
            found[row["metric_name"]] = row["id"]

    print()
    for m in TARGET_METRICS:
        if m in found:
            print(f'  OK: "{m}" exists in metric_catalog (id={found[m]}).')
        else:
            print(f'  MISSING: "{m}" is NOT in metric_catalog for AWS/EBS. '
                  f'This is the root cause for that metric - it must be INSERTed '
                  f'into metric_catalog before any account can enable it.')

    if account_id is None:
        cur.close(); conn.close()
        print("\nNo account_id given - skipping selection check. "
              "Re-run with an account_id to check enablement for a specific account.")
        return

    print(f"\n=== Step 2: account_metric_selections check (account_id = {account_id}) ===\n")
    cur.execute("SELECT id FROM aws_accounts WHERE id = %s", (account_id,))
    if not cur.fetchone():
        print(f"Account {account_id} not found in aws_accounts.")
        cur.close(); conn.close()
        return

    for m, metric_id in found.items():
        cur.execute("""
            SELECT enabled, source FROM account_metric_selections
            WHERE aws_account_id = %s AND metric_id = %s
        """, (account_id, metric_id))
        sel = cur.fetchone()
        if sel is None:
            print(f'  MISSING: no account_metric_selections row for "{m}" '
                  f'(metric_id={metric_id}) on account {account_id}. '
                  f'It was never added to this account\'s selection - that\'s '
                  f'why the generated config skips it.')
        elif sel["enabled"] == 0:
            print(f'  DISABLED: "{m}" (metric_id={metric_id}) has a selection row '
                  f'but enabled=0 (source={sel["source"]}). Needs enabled=1.')
        else:
            print(f'  ENABLED: "{m}" (metric_id={metric_id}) is enabled '
                  f'(source={sel["source"]}) - if it\'s still missing from the '
                  f'generated YACE config, the bug is in generate_yace_config() '
                  f'itself, not the data.')

    cur.close(); conn.close()

    print("\nNext step depends on what printed above:")
    print(" - If MISSING from metric_catalog: need an INSERT into metric_catalog first.")
    print(" - If MISSING or DISABLED in account_metric_selections: call")
    print(f"     PUT /api/account-metrics/{account_id}  "
          f'body: {{"enabled_metric_ids": [...existing ids..., <the missing metric_id(s)>]}}')
    print("   (fetch current enabled ids first via GET on the same endpoint), then")
    print(f"     GET /api/account-metrics/{account_id}/yace-config?tier=critical")
    print("   to regenerate, then push+reload YACE on the server as usual.")
    print(" - If ENABLED but still missing from output: bug is in the config-generation")
    print("   query/template itself - paste generate_yace_config()'s SELECT and the")
    print("   Jinja2 template and I'll find it.")


if __name__ == "__main__":
    main()
