#!/usr/bin/env python3
"""
scripts/check_ec2_network_stat.py  (READ-ONLY)

Settles one question with data instead of screenshots: does the "Network in /
Network out (bytes)" chart agree with what the EC2 console shows (Statistic =
Sum, Period = 5 minutes)?

CloudOps collects NetworkIn/NetworkOut with Statistic=Average (polling_model.py).
The EC2 console graphs them as Sum. If a 5-minute datapoint holds more than one
sample (SampleCount > 1), Average = Sum / SampleCount and our chart reads that
many times LOWER than the console. If SampleCount is 1, the two are identical
and nothing needs to change.

What it does: ONE CloudWatch GetMetricStatistics call (a few cents per million)
for the last 90 min, plus a read of the stored rows from metric_history.

Run from /opt/monitoring-hub/app:
  /opt/monitoring-hub/venv/bin/python3 scripts/check_ec2_network_stat.py <aws_accounts.id> <i-xxxx> [NetworkIn|NetworkOut]
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from app.db import get_connection
from app.aws.collector_direct import get_session, _metric_history_query_range


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    account_db_id, instance_id = int(argv[1]), argv[2]
    metric = argv[3] if len(argv) > 3 else "NetworkIn"
    if metric not in ("NetworkIn", "NetworkOut"):
        print("metric must be NetworkIn or NetworkOut")
        return 2

    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM aws_accounts WHERE id = %s", (account_db_id,))
        acc = cur.fetchone()
        cur.close()
    finally:
        conn.close()
    if not acc:
        print(f"no aws_accounts row with id={account_db_id}")
        return 1
    region = acc.get("default_region")

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=90)
    cw = get_session(region, account=acc).client("cloudwatch")
    resp = cw.get_metric_statistics(
        Namespace="AWS/EC2", MetricName=metric,
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start, EndTime=end, Period=300,
        Statistics=["Sum", "Average", "SampleCount"], Unit="Bytes",
    )
    pts = sorted(resp.get("Datapoints", []), key=lambda d: d["Timestamp"])
    stored = _metric_history_query_range("ec2", instance_id, metric.lower(), start, end,
                                         account_id=acc.get("id"))
    stored_ts = []
    for s_ in stored:
        try:
            stored_ts.append((datetime.fromisoformat(s_["t"].replace("Z", "+00:00")), s_["v"]))
        except Exception:
            pass

    def nearest(ts):
        # stored timestamps are collection times, not CloudWatch bucket starts
        best = min(stored_ts, key=lambda x: abs((x[0] - ts).total_seconds()), default=None)
        return best[1] if best and abs((best[0] - ts).total_seconds()) <= 450 else None

    print(f"{metric} {instance_id} region={region} (AWS = live CloudWatch, CloudOps = stored metric_history)\n")
    print(f"{'UTC time':<17}{'AWS Sum':>14}{'AWS Average':>14}{'SampleCount':>12}{'CloudOps':>14}{'Sum/CloudOps':>14}")
    ratios, counts = [], []
    for d in pts:
        key = d["Timestamp"].strftime("%Y-%m-%dT%H:%M")
        mine = nearest(d["Timestamp"])
        counts.append(d["SampleCount"])
        ratio = (d["Sum"] / mine) if mine else None
        if ratio:
            ratios.append(ratio)
        print(f"{key.replace('T', ' '):<17}{d['Sum']:>14.0f}{d['Average']:>14.0f}{d['SampleCount']:>12.0f}"
              f"{(mine if mine is not None else float('nan')):>14.0f}{(ratio if ratio else float('nan')):>14.2f}")

    print()
    if not pts:
        print("No datapoints returned: check the instance id / that it is running.")
        return 1
    avg_n = sum(counts) / len(counts)
    if avg_n <= 1.01:
        print("VERDICT: SampleCount is 1 -> Average == Sum. The chart already matches the console. No change needed.")
    else:
        print(f"VERDICT: SampleCount is about {avg_n:.0f} -> Average = Sum / {avg_n:.0f}. "
              f"The chart reads about {avg_n:.0f}x LOWER than the console's Sum. Send me this output.")
    if ratios:
        print(f"(median Sum / CloudOps ratio over matching points: {sorted(ratios)[len(ratios)//2]:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
