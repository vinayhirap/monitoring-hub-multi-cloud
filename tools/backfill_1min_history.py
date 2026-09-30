#!/usr/bin/env python3
"""
tools/backfill_1min_history.py -- one-off repair of metric_history rows that
were frozen while still filling (ALB / RDS / Lambda, Period 60).

Before 2026-09-30 the collector requested end=now and stored the newest,
still-filling minute with INSERT IGNORE, so e.g. ActiveConnectionCount showed
89 in CloudOps while CloudWatch's settled value was 167. The collector now
requests settled minute-aligned windows and overwrites on re-seen buckets;
this script re-fetches the last N hours ONCE so history written by the old
code is corrected too.

Usage (inside /opt/monitoring-hub/app, on the box that serves the UI):
    /opt/monitoring-hub/venv/bin/python3 tools/backfill_1min_history.py --hours 6
    /opt/monitoring-hub/venv/bin/python3 tools/backfill_1min_history.py --hours 24 --account-id 10 --types elb

--hours max 24 (GetMetricData returns <=1440 one-minute points per query).
Writes only metric_history/metrics rows for the given account(s); read-only on AWS.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app.collector.metrics import runner
from app.collector import polling_model
from app.db import get_connection  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=6)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--types", default="elb,rds,lambda")
    a = ap.parse_args()
    hours = max(1, min(a.hours, 24))
    types = [t.strip() for t in a.types.split(",") if t.strip()]

    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""SELECT id, account_name, account_id, role_arn, auth_mode, external_id,
                  default_region, provider
           FROM aws_accounts WHERE status = 'active'""")
    accounts = [x for x in cur.fetchall()
                if (x.get("provider") or "aws") == "aws" and (not a.account_id or x["id"] == a.account_id)]
    cur.close(); conn.close()

    for acct in accounts:
        try:
            session = runner.get_boto3_session(acct)
        except Exception as e:
            print(f"skip {acct.get('account_name')}: {e}")
            continue
        grouped = runner._get_resources_for_account(acct["id"], "low")
        for (rtype, region), resources in grouped.items():
            if rtype not in types:
                continue
            cw = session.client("cloudwatch", region_name=region, config=runner.STANDARD_RETRY)
            by_gate = {}
            for m in polling_model.AWS_CORE_METRICS:
                if m.resource_type == rtype and m.period_sec == 60:
                    by_gate.setdefault(m.gate, []).append(
                        (m.cw_name, m.db_name, m.stat, m.namespace, m.period_sec))
            for gate, defs in by_gate.items():
                res = [r for r in resources if runner._passes_gate(r, gate)]
                if not res:
                    continue
                n = runner._run_gmd(cw, res, defs, minutes=hours * 60)
                print(f"{acct['account_name']} {rtype}/{region} gate={gate}: {n} series rewritten")


if __name__ == "__main__":
    main()
