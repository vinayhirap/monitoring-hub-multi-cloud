#!/usr/bin/env python3
"""
scripts/verify_account_health_fix.py

Diagnoses exactly why the Overview dashboard might still show
"7 CRITICAL" in the alert banner while an account's status pill says
Healthy and the Critical tile says 0 -- even after
apply_account_health_rollup_fix.py has been applied to the repo.

THE #1 CAUSE, in practice: the backend process was never actually
restarted after the file was patched. FastAPI/Uvicorn does not hot-
reload Python changes unless it's running with --reload (and even
then, --reload is not meant for production). A patched .py file on
disk does nothing until the process running app.main:app is restarted.

This script tells you, concretely, which side of that gap you're on:

  1. Calls GET /api/live/accounts on the running backend and checks
     whether each account row actually contains "critical_alerts" /
     "warning_alerts" keys. If they're ABSENT, the process serving
     that endpoint is still running the OLD code -- restart it.

  2. Independently queries the database directly (bypassing the API
     entirely) with the exact same query the fix uses, and prints what
     each account's status SHOULD be. Compare this against what the
     API actually returned in step 1 -- if step 1's numbers don't
     match step 2 even though the new fields ARE present, that's a
     different, real bug worth reporting rather than a restart issue.

Usage:
    python scripts/verify_account_health_fix.py
    python scripts/verify_account_health_fix.py --api-url http://127.0.0.1:8000
"""
import argparse
import json
import sys

import requests
from dotenv import load_dotenv
load_dotenv()

from app.db import get_connection


def check_api(api_url: str):
    print("=== Step 1: what is the running backend ACTUALLY returning? ===")
    try:
        resp = requests.get(f"{api_url}/api/live/accounts", timeout=15)
        resp.raise_for_status()
        accounts = resp.json()
    except Exception as e:
        print(f"  Could not reach {api_url}/api/live/accounts: {e}")
        print("  (Is the backend running? Is api-url correct?)")
        return None

    if not accounts:
        print("  API returned zero accounts. Nothing to check here directly --")
        print("  see Step 2 below for what the database itself has.")
        return accounts

    missing_new_fields = [a for a in accounts if "critical_alerts" not in a or "warning_alerts" not in a]

    if missing_new_fields:
        print("  ✗ The API response is MISSING critical_alerts/warning_alerts.")
        print("    This means the backend process is still running the OLD")
        print("    app/api/live_data.py -- the file on disk may be patched,")
        print("    but the running process hasn't picked it up.")
        print()
        print("    FIX: restart the backend process properly, e.g.:")
        print("      sudo systemctl restart monitoring-hub      # if run via systemd")
        print("      # or, if run manually:")
        print("      pkill -f 'uvicorn app.main:app'")
        print("      uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2")
        print("    NOT `--reload` in production, and NOT just re-running the apply")
        print("    script again -- that only rewrites the .py file, it can't restart")
        print("    a process for you.")
    else:
        print("  ✓ critical_alerts/warning_alerts ARE present in the API response --")
        print("    the fix is live. Printing what each account currently reports:")
        for a in accounts:
            print(f"    - {a.get('account_name')}: status={a.get('status')} "
                  f"critical_alerts={a.get('critical_alerts')} warning_alerts={a.get('warning_alerts')}")
    print()
    return accounts


def check_database():
    print("=== Step 2: what does the database say, independent of the API? ===")
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT id, account_name, account_id, default_region, status
        FROM aws_accounts
        WHERE status = 'active'
        ORDER BY created_at DESC
    """)
    accounts = cursor.fetchall()

    if not accounts:
        print("  No active rows in aws_accounts at all.")
        cursor.close()
        conn.close()
        return

    cursor.execute("""
        SELECT r.aws_account_id, a.severity, COUNT(DISTINCT a.resource_id) AS cnt,
               GROUP_CONCAT(DISTINCT a.resource_id ORDER BY a.resource_id SEPARATOR ', ') AS resource_ids
        FROM alerts a
        JOIN resources r ON r.resource_id = a.resource_id
        WHERE a.status = 'active'
          AND a.resolved_at IS NULL
          AND a.triggered_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)
        GROUP BY r.aws_account_id, a.severity
    """)
    counts_by_account = {}
    for row in cursor.fetchall():
        bucket = counts_by_account.setdefault(row["aws_account_id"], {})
        bucket[row["severity"]] = row

    # Also check for active alerts whose resource_id has NO matching row in
    # `resources` at all -- these would be silently excluded by the fix's
    # INNER JOIN (and, separately, are exactly the kind of orphaned alert
    # alert_evaluator.py's _auto_resolve_stale_alerts is supposed to clean
    # up automatically -- if there are many of these, that cleanup job may
    # not be running, which is worth knowing regardless of this fix).
    cursor.execute("""
        SELECT a.resource_id, a.severity, a.metric_name, a.triggered_at
        FROM alerts a
        LEFT JOIN resources r ON r.resource_id = a.resource_id
        WHERE a.status = 'active' AND a.resolved_at IS NULL AND r.id IS NULL
    """)
    orphaned = cursor.fetchall()

    cursor.close()
    conn.close()

    for acc in accounts:
        bucket = counts_by_account.get(acc["id"], {})
        crit = bucket.get("CRITICAL", {}).get("cnt", 0)
        warn = bucket.get("WARNING", {}).get("cnt", 0)
        expected_status = "critical" if crit > 0 else "warning" if warn > 0 else "healthy (or avg_cpu-derived)"
        print(f"  - {acc['account_name']} (aws_accounts.id={acc['id']}): "
              f"critical={crit} warning={warn} -> expected status: {expected_status}")
        if "CRITICAL" in bucket:
            print(f"      critical resource_ids: {bucket['CRITICAL']['resource_ids']}")
        if "WARNING" in bucket:
            print(f"      warning  resource_ids: {bucket['WARNING']['resource_ids']}")

    if orphaned:
        print(f"\n  NOTE: {len(orphaned)} active alert(s) reference a resource_id with NO")
        print("  matching row in `resources` at all -- these are invisible to ANY")
        print("  account-level rollup (this fix included), since there's no account to")
        print("  attribute them to. This usually means the resource was deleted/renamed")
        print("  in AWS after the alert fired, and app/collector/alert_evaluator.py's")
        print("  stale-alert auto-resolve hasn't caught up yet (it runs on the same")
        print("  5-minute cycle as evaluation). Sample:")
        for o in orphaned[:10]:
            print(f"    - resource_id={o['resource_id']} severity={o['severity']} "
                  f"metric={o['metric_name']} triggered_at={o['triggered_at']}")
        if len(orphaned) > 10:
            print(f"    ... and {len(orphaned) - 10} more")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    check_api(args.api_url)
    check_database()

    print("=== Summary ===")
    print("If Step 1 showed missing fields -> restart the backend, re-run this script.")
    print("If Step 1's fields ARE present and match Step 2 -> the fix is working correctly.")
    print("If Step 1's fields are present but DISAGREE with Step 2 -> that's a genuine")
    print("bug, not a deployment gap; share this script's full output.")


if __name__ == "__main__":
    main()
