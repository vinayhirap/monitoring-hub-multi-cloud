#!/usr/bin/env python3
"""
backfill_alert_priority.py -- give every OPEN alert its P1-P4 priority (phase 4b).

DRY RUN by default: prints what would change and writes nothing. Add --apply to write.
Needs migration 101 (alerts.priority). Does not need PRIORITY_ENABLED: it is how you fill the column
before you switch the flag on. Changes no severity, status, escalation or notification.

    cd /opt/monitoring-hub/app
    /opt/monitoring-hub/venv/bin/python3 scripts/backfill_alert_priority.py
    /opt/monitoring-hub/venv/bin/python3 scripts/backfill_alert_priority.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the priorities (default: dry run)")
    args = ap.parse_args()

    # app.db refuses to import without DB_PASSWORD, and the environment flags decide which label is used,
    # so load the service's .env first, like the other scripts.
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

    from app.db import get_connection
    from app import environment_classifier as envc
    from app.collector import priority_backfill

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        res = priority_backfill.run(cursor, apply=args.apply, use_resolved_env=envc.flag_enabled())
        if args.apply:
            conn.commit()
    finally:
        cursor.close()
        conn.close()

    print(f"Open alerts checked : {res['checked']}")
    print(f"Would change / changed: {res['changed']}  ({'APPLIED' if args.apply else 'dry run, nothing written'})")
    print("Open alerts by priority after this run:")
    for p, n in res["by_priority"].items():
        print(f"  P{p}: {n}")
    if res["changes"]:
        print("First changes (alert id: old -> new):")
        for aid, old, new in res["changes"][:15]:
            print(f"  {aid}: {'-' if old is None else 'P' + str(old)} -> P{new}")


if __name__ == "__main__":
    main()
