#!/usr/bin/env python3
"""
verify_ebs_metrics.py

Directly queries VictoriaMetrics (bypassing the app entirely) for all 6
EBS metric series and reports, per series, whether it has any data points
in the last N hours and what the most recent value is.

Confirms independently of the app UI whether apply_ebs_fix.py's changes
actually line up with live data on VM, and specifically checks whether
read_bytes/write_bytes exist at all (per the label-values output, they
currently do not - this script will show that plainly).

Usage:
    python verify_ebs_metrics.py http://3.109.181.40 [hours]

    hours defaults to 6.
"""
import sys
import time
import requests

METRICS = [
    "aws_ebs_volume_read_ops_average",
    "aws_ebs_volume_write_ops_average",
    "aws_ebs_volume_read_bytes_average",
    "aws_ebs_volume_write_bytes_average",
    "aws_ebs_volume_queue_length_average",
    "aws_ebs_burst_balance_average",
]


def main():
    if len(sys.argv) < 2:
        print("Usage: python verify_ebs_metrics.py <vm_base_url> [hours]")
        sys.exit(1)

    vm_url = sys.argv[1].rstrip("/")
    hours = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0

    now = int(time.time())
    start = now - int(hours * 3600)

    print(f"Querying {vm_url} for the last {hours}h\n")
    print(f"{'metric':45s} {'series':>7s} {'samples':>8s}  latest_value")
    print("-" * 90)

    for metric in METRICS:
        try:
            resp = requests.get(
                f"{vm_url}/api/v1/query_range",
                params={
                    "query": metric,
                    "start": start,
                    "end": now,
                    "step": "60s",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("data", {}).get("result", [])

            if not result:
                print(f"{metric:45s} {'0':>7s} {'0':>8s}  NO SERIES FOUND ON VM")
                continue

            series_count = len(result)
            total_samples = sum(len(r.get("values", [])) for r in result)

            latest_val = None
            for r in result:
                vals = r.get("values", [])
                if vals:
                    latest_val = vals[-1][1]

            status = latest_val if latest_val is not None else "series exists, 0 samples"
            print(f"{metric:45s} {series_count:>7d} {total_samples:>8d}  {status}")

        except requests.RequestException as e:
            print(f"{metric:45s}  ERROR: {e}")

    print("\nDone. 'NO SERIES FOUND ON VM' means YACE is not exporting that metric at all")
    print("(check the deployed YACE config on the server), not a backend code issue.")


if __name__ == "__main__":
    main()
