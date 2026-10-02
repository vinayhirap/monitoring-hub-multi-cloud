#!/usr/bin/env python3
"""
scripts/anomaly_shadow_report.py -- READ-ONLY report on the (UI-hidden) multivariate anomaly
alerts, so detector changes can be judged with numbers (AI/ML audit Phase 2, 2026-10-02).

For the last N days (default 7) it reports: how many `multivariate_anomaly` alerts fired, per
day and per resource, how long they lasted, and a precision PROXY -- the share that were
corroborated by a real (visible) alert on the same resource starting within 30 minutes before
to 60 minutes after the anomaly. A low corroborated share is not proof of false positives
(the anomaly may be early, or real but sub-threshold), but a detector that is mostly
uncorroborated and fires constantly is not ready to be shown to users.

Run it BEFORE deploying detector changes and again after a few days:
    cd /opt/monitoring-hub/app && /opt/monitoring-hub/venv/bin/python3 scripts/anomaly_shadow_report.py --days 7

Only SELECTs are issued. No writes, no AWS calls, no LLM.
"""
import argparse
import os
import statistics
import sys
from collections import Counter

QUERY = """
    SELECT a.id, a.aws_account_id, a.resource_id, a.status, a.triggered_at, a.resolved_at,
           EXISTS (
               SELECT 1 FROM alerts v
               WHERE v.aws_account_id = a.aws_account_id AND v.resource_id = a.resource_id
                 AND v.metric_name NOT IN ('multivariate_anomaly', 'synthetic_uptime')
                 AND v.triggered_at BETWEEN DATE_SUB(a.triggered_at, INTERVAL 30 MINUTE)
                                        AND DATE_ADD(a.triggered_at, INTERVAL 60 MINUTE)
           ) AS corroborated
    FROM alerts a
    WHERE a.metric_name = 'multivariate_anomaly'
      AND a.triggered_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s DAY)
    ORDER BY a.triggered_at DESC
    LIMIT 5000
"""


def build_report(cursor, days: int) -> dict:
    cursor.execute(QUERY, (days,))
    rows = cursor.fetchall() or []
    total = len(rows)
    corroborated = sum(1 for r in rows if r.get("corroborated"))
    durations = [
        (r["resolved_at"] - r["triggered_at"]).total_seconds() / 60.0
        for r in rows if r.get("resolved_at") and r.get("triggered_at")
    ]
    per_day = Counter(str(r["triggered_at"])[:10] for r in rows)
    per_resource = Counter(r["resource_id"] for r in rows)
    return {
        "days": days,
        "total": total,
        "still_active": sum(1 for r in rows if r.get("status") in ("active", "acknowledged")),
        "corroborated": corroborated,
        "corroborated_pct": round(100.0 * corroborated / total, 1) if total else None,
        "median_duration_min": round(statistics.median(durations), 1) if durations else None,
        "distinct_resources": len(per_resource),
        "per_day": sorted(per_day.items()),
        "top_resources": per_resource.most_common(10),
    }


def format_report(rep: dict) -> str:
    out = [f"multivariate_anomaly alerts, last {rep['days']} day(s)  (UTC)", "-" * 60,
           f"total fired            : {rep['total']}",
           f"still active           : {rep['still_active']}",
           f"distinct resources     : {rep['distinct_resources']}",
           f"median duration (min)  : {rep['median_duration_min']}",
           f"corroborated by a real alert (-30/+60 min): {rep['corroborated']}"
           + (f" ({rep['corroborated_pct']}%)" if rep["corroborated_pct"] is not None else ""),
           "", "per day:"]
    out += [f"  {d}  {n}" for d, n in rep["per_day"]] or ["  (none)"]
    out += ["", "most frequent resources:"]
    out += [f"  {n:4d}  {rid[:90]}" for rid, n in rep["top_resources"]] or ["  (none)"]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from app.db import get_connection
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        print(format_report(build_report(cursor, args.days)))
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()
