#!/usr/bin/env python3
"""
tools/verify_cloudwatch_parity.py -- read-only check that what CloudOps shows
for an EC2 instance matches what CloudWatch itself reports.

For each metric it compares three layers, latest datapoint in each:

  CW   live GetMetricData, same account credentials, same dimensions and
       the same Windows "100 - free%" inversion the collector uses
  DB   the `metrics` last-value cache (drives alerts and the list view) and
       the newest `metric_history` row (drives the CPU/Network charts)
  API  what get_ec2_metric_series() returns, i.e. exactly the JSON the
       frontend chart receives (mem/disk are live CloudWatch; cpu/network
       come from metric_history)

The frontend then only does two things to that JSON: network_in/out are
divided by 1024 (label "KB", bytes per 5-min period, NOT per second) and
percent series are drawn as-is. The report prints the value the UI will
therefore show for network so you can eyeball it against the screen.

Usage (run inside /opt/monitoring-hub/app on the box that serves the UI):
    /opt/monitoring-hub/venv/bin/python3 tools/verify_cloudwatch_parity.py \\
        --account-id 10 --instance-id i-0424cb66e22e05a21
    /opt/monitoring-hub/venv/bin/python3 tools/verify_cloudwatch_parity.py \\
        --account-id 10 --all-running

Read-only: only CloudWatch Get/List calls and SELECTs. Never writes.
Exit code 0 when nothing is DIFF or MISSING, 1 otherwise (STALE is a warning).
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Datapoints are only comparable when they describe the same time window.
MAX_TS_SKEW_SECONDS = 600          # 10 min: CW period is 5 min, collector lags
# Absolute tolerance per metric family (same datapoint, different fetch time).
TOLERANCE = {"percent": 1.0, "bytes": 0.05}   # bytes: 5 % relative


def classify(cw, other, kind, max_skew=MAX_TS_SKEW_SECONDS):
    """cw / other are (value, datetime_utc) or None. Returns
    OK | DIFF | STALE | MISSING. STALE = the two datapoints are not from the
    same window, so a value difference proves nothing."""
    if cw is None and other is None:
        return "MISSING"
    if cw is None or other is None:
        return "MISSING"
    (cv, ct), (ov, ot) = cw, other
    if abs((ct - ot).total_seconds()) > max_skew:
        return "STALE"
    if kind == "percent":
        return "OK" if abs(cv - ov) <= TOLERANCE["percent"] else "DIFF"
    denom = max(abs(cv), abs(ov), 1.0)
    return "OK" if abs(cv - ov) / denom <= TOLERANCE["bytes"] else "DIFF"


def _parse_ts(t):
    if isinstance(t, datetime):
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _last_point(series):
    """series: [{"t": iso, "v": float}, ...] oldest->newest."""
    for p in reversed(series or []):
        ts = _parse_ts(p.get("t"))
        if ts is not None and p.get("v") is not None:
            return float(p["v"]), ts
    return None


def _cw_latest(cw, queries, minutes):
    """Latest (value, ts) per returned query Id from GetMetricData."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    out = {}
    token = None
    while True:
        kw = dict(MetricDataQueries=queries, StartTime=start, EndTime=end,
                  ScanBy="TimestampDescending")
        if token:
            kw["NextToken"] = token
        resp = cw.get_metric_data(**kw)
        for r in resp.get("MetricDataResults", []):
            vals, tss = r.get("Values", []), r.get("Timestamps", [])
            if vals and r["Id"] not in out:
                out[r["Id"]] = (float(vals[0]), _parse_ts(tss[0]))
        token = resp.get("NextToken")
        if not token:
            return out


def _db_lookup(cur, account_id, instance_id):
    cur.execute("SELECT id FROM resources WHERE resource_type='ec2' "
                "AND resource_id=%s AND aws_account_id=%s LIMIT 1",
                (instance_id, account_id))
    row = cur.fetchone()
    return row["id"] if row else None


def _db_last(cur, table, rid, metric):
    cur.execute(f"SELECT metric_value, metric_timestamp FROM {table} "
                "WHERE resource_id=%s AND metric_name=%s "
                "ORDER BY metric_timestamp DESC LIMIT 1", (rid, metric))
    row = cur.fetchone()
    if not row or row["metric_value"] is None:
        return None
    return float(row["metric_value"]), _parse_ts(row["metric_timestamp"])


def check_instance(account, instance_id, region):
    from app.aws.sts import get_boto3_session
    from app.aws.collector_direct import (get_ec2_metric_series,
                                          _ec2_cwagent_dimensions, STANDARD_RETRY)
    from app.collector.disk_mounts import all_cwagent_disk_dims
    from app.db import get_connection

    session = get_boto3_session(account)
    cw = session.client("cloudwatch", region_name=region, config=STANDARD_RETRY)
    ec2_dim = [{"Name": "InstanceId", "Value": instance_id}]

    # --- CloudWatch, live -------------------------------------------------
    q5, q1, labels = [], [], {}          # labels: qid -> (db_metric, api_key, kind)
    for i, (cw_name, db_name, api_key) in enumerate([
            ("CPUUtilization", "cpuutilization", "cpu"),
            ("NetworkIn", "networkin", "network_in"),
            ("NetworkOut", "networkout", "network_out")]):
        qid = f"ec2m{i}"
        q5.append({"Id": qid, "MetricStat": {
            "Metric": {"Namespace": "AWS/EC2", "MetricName": cw_name, "Dimensions": ec2_dim},
            "Period": 300, "Stat": "Average"}, "ReturnData": True})
        labels[qid] = (db_name, api_key, "percent" if api_key == "cpu" else "bytes")

    mem_name = "mem_used_percent"
    mem_dims = _ec2_cwagent_dimensions(cw, mem_name, instance_id)
    if mem_dims is None:
        mem_name = "Memory % Committed Bytes In Use"
        mem_dims = _ec2_cwagent_dimensions(cw, mem_name, instance_id)
    if mem_dims:
        q1.append({"Id": "agmem", "MetricStat": {
            "Metric": {"Namespace": "CWAgent", "MetricName": mem_name, "Dimensions": mem_dims},
            "Period": 60, "Stat": "Average"}, "ReturnData": True})
        labels["agmem"] = ("mem_used_percent", "mem_utilization", "percent")

    mounts = all_cwagent_disk_dims(cw, instance_id)
    for n, (dims, path, db_name, cw_metric, invert) in enumerate(mounts):
        qid = f"agdisk{n}"
        stat = {"Metric": {"Namespace": "CWAgent", "MetricName": cw_metric, "Dimensions": dims},
                "Period": 60, "Stat": "Average"}
        if invert:
            q1.append({"Id": qid + "raw", "MetricStat": stat, "ReturnData": False})
            q1.append({"Id": qid, "Expression": f"100 - {qid}raw", "ReturnData": True})
        else:
            q1.append({"Id": qid, "MetricStat": stat, "ReturnData": True})
        labels[qid] = (db_name, ("disk", path), "percent")

    cw_vals = _cw_latest(cw, q5, 30)
    if q1:
        cw_vals.update(_cw_latest(cw, q1, 20))

    # --- DB + API ---------------------------------------------------------
    api = get_ec2_metric_series(instance_id, region=region, hours=1, account=account)
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        rid = _db_lookup(cur, account["id"], instance_id)
        rows = []
        for qid, (db_name, api_key, kind) in labels.items():
            if isinstance(api_key, tuple):          # ("disk", path)
                series = (api.get("disk_used_percent_by_mount") or {}).get(api_key[1], [])
                api_label = f"api.disk[{api_key[1]}]"
            else:
                series, api_label = api.get(api_key, []), f"api.{api_key}"
            cw_v = cw_vals.get(qid)
            dbm = _db_last(cur, "metrics", rid, db_name) if rid else None
            dbh = _db_last(cur, "metric_history", rid, db_name) if rid else None
            apv = _last_point(series)
            rows.append((db_name, kind, cw_v, dbm, dbh, api_label, apv))
    finally:
        cur.close()
        conn.close()
    return rows, api.get("cwagent_installed")


def _fmt(p):
    if p is None:
        return "-".rjust(22)
    v, t = p
    return f"{v:>10.2f} @{t.strftime('%H:%M')}".rjust(22)


def report(instance_id, rows, cwagent):
    print(f"\n=== {instance_id}  (cwagent_installed per API: {cwagent}) ===")
    print(f"{'metric':<26}{'CloudWatch':>22}{'DB metrics':>22}{'DB history':>22}"
          f"{'API/frontend':>22}  verdict (CW vs DB / hist / API)")
    bad = 0
    for db_name, kind, cw_v, dbm, dbh, api_label, apv in rows:
        verdicts = [classify(cw_v, x, kind) for x in (dbm, dbh, apv)]
        if any(v in ("DIFF", "MISSING") for v in verdicts):
            # history/metrics are legitimately absent for metrics they do not
            # store (e.g. cpu has no mem history); only flag when CW has data.
            if cw_v is not None:
                bad += 1
        print(f"{db_name:<26}{_fmt(cw_v)}{_fmt(dbm)}{_fmt(dbh)}{_fmt(apv)}  "
              f"{' / '.join(verdicts)}")
        if db_name in ("networkin", "networkout") and apv:
            print(f"{'':<26}  UI shows {apv[0] / 1024:.1f} KB per 5-min period "
                  f"(= {apv[0] / 1024 / 300:.3f} KB/s)")
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--account-id", type=int, required=True, help="aws_accounts.id")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--instance-id")
    g.add_argument("--all-running", action="store_true")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args()

    from app.db import get_connection
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT id, account_name, account_id, role_arn, auth_mode, external_id, "
                    "default_region, provider FROM aws_accounts WHERE id=%s", (args.account_id,))
        account = cur.fetchone()
        if not account:
            sys.exit(f"aws_accounts.id={args.account_id} not found")
        if args.all_running:
            cur.execute("SELECT resource_id, region FROM resources WHERE resource_type='ec2' "
                        "AND aws_account_id=%s AND instance_state='running' "
                        "ORDER BY resource_id LIMIT %s", (args.account_id, args.limit))
            targets = [(r["resource_id"], r["region"] or account["default_region"])
                       for r in cur.fetchall()]
        else:
            cur.execute("SELECT region FROM resources WHERE resource_type='ec2' AND "
                        "resource_id=%s AND aws_account_id=%s", (args.instance_id, args.account_id))
            row = cur.fetchone()
            targets = [(args.instance_id, (row or {}).get("region") or account["default_region"])]
    finally:
        cur.close()
        conn.close()

    total_bad = 0
    for iid, region in targets:
        try:
            rows, cwagent = check_instance(account, iid, region)
        except Exception as e:                      # keep going across instances
            print(f"\n=== {iid} ===\n  ERROR: {e}")
            total_bad += 1
            continue
        total_bad += report(iid, rows, cwagent)
    print(f"\n{'ALL OK' if not total_bad else str(total_bad) + ' metric(s) need attention'} "
          f"(STALE = datapoints from different windows, not a failure)")
    sys.exit(1 if total_bad else 0)


if __name__ == "__main__":
    main()
