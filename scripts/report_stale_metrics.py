#!/usr/bin/env python3
"""
List everything that is genuinely STALE right now, judged exactly as the alert engine judges it.

    cd /opt/monitoring-hub/app && /opt/monitoring-hub/venv/bin/python3 scripts/report_stale_metrics.py
    ... --account U4RAD        only one account
    ... --all                  also show the metrics that are merely "late"

A metric is judged against how often it is COLLECTED (app/alert_rules.stale_minutes_sql, i.e. polling_model.
STALE_MIN_BY_INTERVAL: 20 min for 2/5-minute metrics, 45 for 15-minute, 180 for hourly, 3000 for daily), never against a
flat 15 minutes. "late" is up to twice that allowance, "stale" beyond it.

Read-only: it only runs SELECTs. Prints four sections:
  1. last-value metrics that are stale / late        (the `metrics` cache the evaluator and the charts read)
  2. resources the discovery job has not seen         (resources.last_seen_at)
  3. ACTIVE alerts waiting for data                   (the "Stale" tab on the Alerts page)
  4. accounts whose collection has stopped            (newest datapoint per account)
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Same as migrate.py and the other scripts: read DB_* from the app's .env BEFORE app.db is imported (it refuses to start
# without DB_PASSWORD). The path is explicit, so the script works from any directory, not only from the repo root.
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, ".env"))

from app.alert_rules import stale_minutes_sql  # noqa: E402
from app.db import get_db_cursor  # noqa: E402


def _rows(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


def _print_table(title, rows, columns):
    print(f"\n== {title} ({len(rows)})")
    if not rows:
        print("   none")
        return
    widths = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns]
    print("   " + "  ".join(c.upper().ljust(w) for c, w in zip(columns, widths)))
    for r in rows[:60]:
        print("   " + "  ".join(str(r.get(c, "")).ljust(w) for c, w in zip(columns, widths)))
    if len(rows) > 60:
        print(f"   ... and {len(rows) - 60} more")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", help="account name to limit the report to")
    ap.add_argument("--all", action="store_true", help="also list metrics that are only 'late'")
    args = ap.parse_args()
    acct_sql = " AND aa.account_name = %s" if args.account else ""
    acct_params = (args.account,) if args.account else ()
    allowance = stale_minutes_sql("r", "aa", "m.metric_name")

    with get_db_cursor(dictionary=True, commit=False) as (_c, cur):
        stale = _rows(cur, f"""
            SELECT aa.account_name AS account, r.resource_type AS type, m.metric_name AS metric, COUNT(*) AS resources,
                   MAX(TIMESTAMPDIFF(MINUTE, m.metric_timestamp, UTC_TIMESTAMP())) AS oldest_min,
                   MAX({allowance}) AS allowed_min,
                   CASE WHEN MAX(TIMESTAMPDIFF(MINUTE, m.metric_timestamp, UTC_TIMESTAMP())) > 2 * MAX({allowance})
                        THEN 'STALE' ELSE 'late' END AS state
            FROM metrics m
            JOIN resources r ON r.id = m.resource_id
            JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
            WHERE TIMESTAMPDIFF(MINUTE, m.metric_timestamp, UTC_TIMESTAMP()) > {allowance}
              {acct_sql}
            GROUP BY aa.account_name, r.resource_type, m.metric_name
            ORDER BY oldest_min DESC
        """, acct_params)
        if not args.all:
            stale = [s for s in stale if s["state"] == "STALE"]
        _print_table("Metrics with no new datapoint inside their allowance", stale,
                     ["account", "type", "metric", "resources", "oldest_min", "allowed_min", "state"])

        unseen = _rows(cur, f"""
            SELECT aa.account_name AS account, r.resource_type AS type, r.name AS name,
                   TIMESTAMPDIFF(HOUR, r.last_seen_at, UTC_TIMESTAMP()) AS hours_unseen
            FROM resources r JOIN aws_accounts aa ON aa.id = r.aws_account_id AND aa.status = 'active'
            WHERE r.last_seen_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 2 HOUR) {acct_sql}
            ORDER BY hours_unseen DESC
        """, acct_params)
        _print_table("Resources the discovery job has not seen for 2 h or more", unseen,
                     ["account", "type", "name", "hours_unseen"])

        waiting = _rows(cur, f"""
            SELECT aa.account_name AS account, a.metric_name AS metric, a.resource_id AS resource, a.severity,
                   TIMESTAMPDIFF(MINUTE, COALESCE(a.last_seen_at, a.triggered_at), UTC_TIMESTAMP()) AS minutes_without_data
            FROM alerts a
            JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
            JOIN aws_accounts aa ON aa.id = a.aws_account_id
            WHERE a.status = 'active'
              AND COALESCE(a.last_seen_at, a.triggered_at) < DATE_SUB(UTC_TIMESTAMP(), INTERVAL {stale_minutes_sql('r', 'aa', 'a.metric_name')} MINUTE)
              {acct_sql}
            ORDER BY minutes_without_data DESC
        """, acct_params)
        _print_table("Active alerts waiting for fresh data (Alerts > Stale tab)", waiting,
                     ["account", "metric", "resource", "severity", "minutes_without_data"])

        stopped = _rows(cur, f"""
            SELECT aa.account_name AS account, MAX(m.metric_timestamp) AS newest_datapoint_utc,
                   TIMESTAMPDIFF(MINUTE, MAX(m.metric_timestamp), UTC_TIMESTAMP()) AS minutes_ago
            FROM aws_accounts aa
            LEFT JOIN resources r ON r.aws_account_id = aa.id
            LEFT JOIN metrics m ON m.resource_id = r.id
            WHERE aa.status = 'active' {acct_sql}
            GROUP BY aa.account_name
            HAVING minutes_ago IS NULL OR minutes_ago > 30
        """, acct_params)
        _print_table("Accounts whose newest datapoint is more than 30 minutes old (collection stopped?)", stopped,
                     ["account", "newest_datapoint_utc", "minutes_ago"])
    print("\nLegend: allowed_min is how long that metric may go without a new datapoint (it depends on how often it is "
          "collected); STALE = more than twice that.")


if __name__ == "__main__":
    main()
