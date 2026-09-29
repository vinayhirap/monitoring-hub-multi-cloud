#!/usr/bin/env python3
"""
tools/verify_cloudwatch_parity.py -- read-only check that what CloudOps shows
for an EC2 instance matches what CloudWatch itself reports.

For each metric it compares:

  collector  CloudWatch's value for the SAME time bucket as the newest
             metric_history row (queried as [ts, ts+period), same account
             credentials, dimensions and Windows "100 - free%" inversion the
             collector uses) vs what the collector stored
  api        what get_ec2_metric_series() returns, i.e. the JSON the frontend
             chart receives, vs its source: metric_history for cpu/network,
             live CloudWatch (newest datapoint) for mem/disk

Comparing "latest CloudWatch" to a stored sample is misleading because
CloudWatch anchors buckets to the request start time, so the two land on
different 5-minute samples (bursty network can differ 20x with no bug).

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
import os
import sys
from datetime import datetime, timedelta, timezone

# Running `python3 tools/x.py` puts tools/ (not the repo root) on sys.path,
# so `import app` would fail. Add the repo root explicitly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def _spec(namespace, metric, dims, period, invert=False):
    return {"ns": namespace, "metric": metric, "dims": dims,
            "period": period, "invert": invert}


def _queries(spec, qid):
    stat = {"Metric": {"Namespace": spec["ns"], "MetricName": spec["metric"],
                       "Dimensions": spec["dims"]},
            "Period": spec["period"], "Stat": "Average"}
    if spec["invert"]:      # Windows free% -> used%, same as the collector
        return [{"Id": qid + "raw", "MetricStat": stat, "ReturnData": False},
                {"Id": qid, "Expression": f"100 - {qid}raw", "ReturnData": True}]
    return [{"Id": qid, "MetricStat": stat, "ReturnData": True}]


def bucket_window(ts, period):
    """(start, end) that makes GetMetricData return exactly ONE bucket that
    starts at `ts`. CloudWatch anchors buckets to the request StartTime, so a
    'latest 30 min' query and the collector's own query land on different
    5-minute buckets; comparing those proves nothing on bursty metrics
    (network). Asking for [ts, ts+period) returns the very bucket the
    collector stored."""
    return ts, ts + timedelta(seconds=period)


def _cw_at(cw, spec, ts):
    start, end = bucket_window(ts, spec["period"])
    resp = cw.get_metric_data(MetricDataQueries=_queries(spec, "q"),
                              StartTime=start, EndTime=end)
    for r in resp.get("MetricDataResults", []):
        if r["Id"] == "q" and r.get("Values"):
            return float(r["Values"][0]), _parse_ts(r["Timestamps"][0])
    return None


def _db_lookup(cur, account_id, instance_id):
    cur.execute("SELECT id FROM resources WHERE resource_type='ec2' "
                "AND resource_id=%s AND aws_account_id=%s LIMIT 1",
                (instance_id, account_id))
    row = cur.fetchone()
    return row["id"] if row else None


def _db_last(cur, table, rid, metric, back=0):
    """Newest row (back=0) or the back-th newest (back=2 -> two rows older)."""
    cur.execute(f"SELECT metric_value, metric_timestamp FROM {table} "
                "WHERE resource_id=%s AND metric_name=%s "
                "ORDER BY metric_timestamp DESC LIMIT 1 OFFSET %s",
                (rid, metric, int(back)))
    row = cur.fetchone()
    if not row or row["metric_value"] is None:
        return None
    return float(row["metric_value"]), _parse_ts(row["metric_timestamp"])


def check_instance(account, instance_id, region, back=0):
    from app.aws.sts import get_boto3_session
    from app.aws.collector_direct import (get_ec2_metric_series,
                                          _ec2_cwagent_dimensions, STANDARD_RETRY)
    from app.collector.disk_mounts import all_cwagent_disk_dims
    from app.db import get_connection

    session = get_boto3_session(account)
    cw = session.client("cloudwatch", region_name=region, config=STANDARD_RETRY)
    ec2_dim = [{"Name": "InstanceId", "Value": instance_id}]

    # label -> (db_metric, api_key, kind, spec, is_live_in_api)
    labels = []
    for cw_name, db_name, api_key in [("CPUUtilization", "cpuutilization", "cpu"),
                                      ("NetworkIn", "networkin", "network_in"),
                                      ("NetworkOut", "networkout", "network_out")]:
        labels.append((db_name, api_key, "percent" if api_key == "cpu" else "bytes",
                       _spec("AWS/EC2", cw_name, ec2_dim, 300), False))

    mem_name = "mem_used_percent"
    mem_dims = _ec2_cwagent_dimensions(cw, mem_name, instance_id)
    if mem_dims is None:
        mem_name = "Memory % Committed Bytes In Use"
        mem_dims = _ec2_cwagent_dimensions(cw, mem_name, instance_id)
    if mem_dims:
        labels.append(("mem_used_percent", "mem_utilization", "percent",
                       _spec("CWAgent", mem_name, mem_dims, 60), True))
    for dims, path, db_name, cw_metric, invert in all_cwagent_disk_dims(cw, instance_id):
        labels.append((db_name, ("disk", path), "percent",
                       _spec("CWAgent", cw_metric, dims, 60, invert), True))

    api = get_ec2_metric_series(instance_id, region=region, hours=1, account=account)
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        rid = _db_lookup(cur, account["id"], instance_id)
        rows = []
        for db_name, api_key, kind, spec, live_api in labels:
            if isinstance(api_key, tuple):
                series = (api.get("disk_used_percent_by_mount") or {}).get(api_key[1], [])
            else:
                series = api.get(api_key, [])
            dbh_new = _db_last(cur, "metric_history", rid, db_name) if rid else None
            dbh = _db_last(cur, "metric_history", rid, db_name, back) if rid else None
            apv = _last_point(series)
            # (1) collector parity: CloudWatch's value for the SAME bucket the DB stored
            cw_same = _cw_at(cw, spec, dbh[1]) if dbh else None
            # (2) API parity: live-CW metrics (mem/disk) vs newest CW now; the
            #     5-min metrics are served from metric_history, so API vs history.
            if live_api:
                q = _queries(spec, "n")
                cw_now = _cw_latest(cw, q, 20).get("n")
                api_ref = cw_now
            else:
                api_ref = dbh_new     # API serves the NEWEST history row
            rows.append({"name": db_name, "kind": kind, "cw_same": cw_same, "dbh": dbh,
                         "api": apv, "api_ref": api_ref, "live": live_api})
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
    print(f"{'metric':<26}{'CW same bucket':>22}{'DB history':>22}{'API/frontend':>22}"
          f"  collector / api")
    bad = 0
    for r in rows:
        collector = classify(r["cw_same"], r["dbh"], r["kind"])
        api = classify(r["api_ref"], r["api"], r["kind"])
        # Nothing stored yet is not a mismatch; CW having data the DB lacks is.
        if r["cw_same"] is None and r["dbh"] is None:
            collector = "NO-DATA"
        if collector in ("DIFF", "MISSING") or api in ("DIFF", "MISSING"):
            bad += 1
        print(f"{r['name']:<26}{_fmt(r['cw_same'])}{_fmt(r['dbh'])}{_fmt(r['api'])}"
              f"  {collector} / {api}")
        if r["name"] in ("networkin", "networkout") and r["api"]:
            # Average of a 5-min basic-monitoring point = mean bytes per MINUTE
            # (SampleCount 5, Sum = bytes in the 5 min), so KB/s = value/60/1024.
            print(f"{'':<26}  stored {r['api'][0] / 1024:.1f} KB/min (mean of the 5 "
                  f"one-minute samples) = {r['api'][0] / 1024 / 60:.3f} KB/s")
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--account-id", type=int, required=True, help="aws_accounts.id")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--instance-id")
    g.add_argument("--all-running", action="store_true")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--back", type=int, default=0,
                    help="compare the N-th newest stored bucket instead of the newest. "
                         "The newest bucket can still be filling in CloudWatch when the "
                         "collector reads it (and history is INSERT IGNORE, so it is never "
                         "corrected); --back 2 compares settled buckets.")
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
            rows, cwagent = check_instance(account, iid, region, args.back)
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
