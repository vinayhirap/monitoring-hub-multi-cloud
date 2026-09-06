#!/usr/bin/env python3
"""
scripts/sync_cloudwatch_alarm_thresholds.py

Checks whether ANY user has already created a CloudWatch Alarm
directly in AWS (console, CLI, Terraform, another tool — this app has
no way to know who) for a metric on a resource it tracks, and if so,
makes sure this app's own alert threshold for that same
account/resource-type/metric follows the same value — so an operator
who already tuned "alert me at 90% CPU" in the AWS console doesn't
also have to re-enter 90 here, and the two never silently disagree.

WHY THIS ISN'T A SIMPLE 1:1 SYNC
This app's `thresholds` table is keyed by
(aws_account_id, resource_type, metric_id) — one threshold per METRIC
TYPE per ACCOUNT, not per individual resource (see app/api/settings.py
and app/collector/alert_evaluator.py). A CloudWatch Alarm, by
contrast, is set on ONE SPECIFIC resource. So if two different EC2
instances in the same account have CloudWatch CPU alarms at 80% and
90%, there is no single app-level threshold that is "the same as
both" — the app can only have one CPUUtilization threshold for that
account's EC2 fleet.

This script resolves that honestly rather than silently picking one:
  - It finds every CloudWatch alarm on a resource this app knows about
    (matched via app.aws.sts.assume_role + the same resource discovery
    this app already relies on) and groups them by
    (account, resource_type, metric, comparison direction).
  - Within a group, it takes the MOST CONSERVATIVE value on each side —
    for ">"/">=" metrics (alert when high, e.g. CPU) the group's
    LOWEST alarm threshold becomes the app's "warning" and the
    HIGHEST becomes "critical"; for "<"/"<=" metrics ("lower is
    worse", e.g. CPUCreditBalance) it's reversed. If only one distinct
    value exists across every alarm in the group, warning and critical
    are both set to that value rather than inventing a second number.
  - Every individual alarm that fed into a group is printed in the
    report, so nothing is silently averaged away — an admin can always
    see exactly which AWS alarms justified the number the app ended up
    with.

WHAT THIS DOES
  1. For every active AWS account in `aws_accounts`, assumes its role
     (or uses the server's own credentials for a same-account setup —
     identical logic to app/collector/discovery/runner.py) and scans
     every AWS region that account actually has discovered resources
     in (from the `resources` table) for CloudWatch alarms via
     cloudwatch:DescribeAlarms (free, read-only).
  2. Matches each alarm's dimensions to a specific row in `resources`
     for EC2 (InstanceId), RDS (DBInstanceIdentifier), Lambda
     (FunctionName), and EBS (VolumeId) — the four resource types
     where this app's metric_catalog.service and resources/thresholds
     .resource_type strings already agree (see CAVEAT below for why
     ELB/ALB/NLB are report-only, not auto-applied).
  3. Compares the resolved (warning, critical, comparison) against
     whatever's currently in `thresholds` for that
     account/resource_type/metric.
  4. In --apply mode, writes any divergent threshold to match AWS
     (INSERT ... ON DUPLICATE KEY UPDATE, same statement shape as
     POST /api/settings/thresholds) and writes an audit_logs entry.
     Default mode is report-only — nothing in the DB is touched unless
     --apply is passed.

CAVEAT — ELB / ALB / NLB are REPORT-ONLY, never auto-applied:
metric_catalog seeds Application/Network Load Balancer metrics under
service="alb"/"nlb", but resources discovered via elbv2 (both ALB and
NLB) are stored as resource_type="elb" (see
app/collector/discovery/runner.py::_discover_elb), and
alert_evaluator.py joins thresholds.resource_type = resources
.resource_type directly. Auto-writing a threshold under "alb"/"nlb"
would never actually match those resources, and writing it under
"elb" would use a resource_type metric_catalog didn't seed those
metrics under — either way it's building on an existing mismatch in
the schema, not something this script should paper over silently.
Divergent ELB/ALB/NLB alarms are still detected, matched, and printed
in the report (so nothing is missed) — just never written to the DB.
Fixing that resource_type mismatch itself is a separate, deliberate
change, not a side effect of a threshold-sync script.

WHAT THIS DELIBERATELY DOES NOT COVER (yet): ECS (ClusterName+
ServiceName dimension pair not implemented), S3 (this app doesn't
discover S3 buckets into the `resources` table), and any CloudWatch
alarm built on metric math or anomaly detection (no flat
Namespace/MetricName/Dimensions to match against — these are counted
and reported as "skipped" so you know they exist, not silently
dropped).

Usage:
    python scripts/sync_cloudwatch_alarm_thresholds.py                 # report only, all accounts
    python scripts/sync_cloudwatch_alarm_thresholds.py --apply         # report AND write divergent thresholds
    python scripts/sync_cloudwatch_alarm_thresholds.py --account 5     # limit to one aws_accounts.id
    python scripts/sync_cloudwatch_alarm_thresholds.py --json-out out.json --apply
"""
import argparse
import json
import logging
import sys
from datetime import datetime, timezone

import boto3
from dotenv import load_dotenv
load_dotenv()

from app.db import get_connection
from app.aws.sts import assume_role

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("cwalarm_sync")

# Namespace -> resource_type, restricted to the four types where
# metric_catalog.service and resources/thresholds.resource_type are
# confirmed to use the SAME string (see module docstring CAVEAT for
# why ELB/ALB/NLB live in the report-only map below instead).
NAMESPACE_TO_TYPE = {
    "AWS/EC2":    "ec2",
    "AWS/RDS":    "rds",
    "AWS/Lambda": "lambda",
    "AWS/EBS":    "ebs",
}

# Namespaces we can still MATCH to a discovered resource and REPORT a
# divergence for, but never auto-write via --apply (see CAVEAT above).
NAMESPACE_TO_TYPE_REPORT_ONLY = {
    "AWS/ApplicationELB": "elb",
    "AWS/NetworkELB":     "elb",
    "AWS/ELB":            "elb",
}

DIMENSION_KEY = {
    "ec2":    "InstanceId",
    "rds":    "DBInstanceIdentifier",
    "lambda": "FunctionName",
    "ebs":    "VolumeId",
}

COMPARISON_MAP = {
    "GreaterThanThreshold":        ">",
    "GreaterThanOrEqualToThreshold": ">=",
    "LessThanThreshold":           "<",
    "LessThanOrEqualToThreshold":  "<=",
}


# ── Account / session plumbing (mirrors app/collector/discovery/runner.py) ──

def _get_active_aws_accounts(conn, only_account_id=None):
    cursor = conn.cursor(dictionary=True)
    if only_account_id:
        cursor.execute(
            "SELECT id, account_name, account_id, role_arn, external_id, default_region "
            "FROM aws_accounts WHERE status='active' AND provider='aws' AND id=%s",
            (only_account_id,),
        )
    else:
        cursor.execute(
            "SELECT id, account_name, account_id, role_arn, external_id, default_region "
            "FROM aws_accounts WHERE status='active' AND provider='aws'"
        )
    rows = cursor.fetchall()
    cursor.close()
    return rows


def _get_session(account):
    """Same-account uses default credentials, cross-account uses STS —
    identical logic to discovery/runner.py's _get_session()."""
    try:
        if account.get("role_arn"):
            return assume_role(account["role_arn"], account.get("external_id"),
                                session_name="mh-cwalarm-sync")
        return boto3.Session()
    except Exception as e:
        logger.error(f"Session failed for {account['account_name']}: {e}")
        return None


def _account_regions(conn, account_id, default_region):
    """Every region this account actually has DISCOVERED resources in
    (not just its one configured default_region) — an account can have
    EC2 in ap-south-1 and RDS in us-east-1 at once, and we want alarms
    from both."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT region FROM resources "
        "WHERE aws_account_id=%s AND region IS NOT NULL AND region != ''",
        (account_id,),
    )
    regions = [r[0] for r in cursor.fetchall()]
    cursor.close()
    return regions or ([default_region] if default_region else [])


def _load_resources_index(conn, account_id):
    """{resource_type: (by_resource_id_dict, by_name_dict)} for this
    account — loaded once and reused across every alarm match instead
    of re-querying per alarm."""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT resource_type, resource_id, name FROM resources WHERE aws_account_id=%s",
        (account_id,),
    )
    rows = cursor.fetchall()
    cursor.close()

    index = {}
    for r in rows:
        by_id, by_name = index.setdefault(r["resource_type"], ({}, {}))
        by_id[r["resource_id"]] = r
        if r["name"]:
            by_name[r["name"]] = r
    return index


# ── Alarm scanning ───────────────────────────────────────────────────

def _scan_region_alarms(cw):
    """Every simple (flat Namespace/MetricName/Dimensions) CloudWatch
    metric alarm in this region. Composite alarms are excluded via
    AlarmTypes. Metric-math / anomaly-detection alarms (no MetricName
    at the top level — they use a `Metrics` expression list instead)
    are counted separately and skipped, since there's no single
    Namespace/Dimensions pair to match against a resource."""
    alarms, skipped_math = [], 0
    paginator = cw.get_paginator("describe_alarms")
    for page in paginator.paginate(AlarmTypes=["MetricAlarm"]):
        for alarm in page.get("MetricAlarms", []):
            if not alarm.get("MetricName"):
                skipped_math += 1
                continue
            alarms.append(alarm)
    return alarms, skipped_math


def _extract_dims(alarm):
    return {d["Name"]: d["Value"] for d in alarm.get("Dimensions", [])}


def _match_resource(resources_index, resource_type, dims):
    by_id, by_name = resources_index.get(resource_type, ({}, {}))

    if resource_type == "lambda":
        fname = dims.get("FunctionName")
        return by_name.get(fname) if fname else None

    if resource_type == "elb":
        # ALB/NLB CloudWatch dimension value looks like
        # "app/my-alb/50dc6c495c0c9188" — the tail of the full ARN
        # stored as resources.resource_id (see _discover_elb, which
        # stores LoadBalancerArn, not the short name). Classic ELB
        # uses LoadBalancerName instead, which matches resources.name.
        lb = dims.get("LoadBalancer") or dims.get("LoadBalancerName")
        if not lb:
            return None
        if lb in by_name:
            return by_name[lb]
        for rid, row in by_id.items():
            if rid.endswith(f"loadbalancer/{lb}"):
                return row
        return None

    dim_key = DIMENSION_KEY.get(resource_type)
    val = dims.get(dim_key) if dim_key else None
    return by_id.get(val) if val else None


# ── Threshold reconciliation ─────────────────────────────────────────

def _split_warning_critical(threshold_values, comparison):
    """Same convention as app/threshold_defaults.DEFAULT_THRESHOLDS:
    for '>'/'>=' metrics critical is the LARGER (more extreme) value
    and warning the SMALLER; for '<'/'<=' ("lower is worse") metrics
    it's reversed. A single distinct value maps to warning==critical
    rather than inventing a second number."""
    values = sorted(set(threshold_values))
    if comparison in (">", ">="):
        return values[0], values[-1]
    return values[-1], values[0]


def _lookup_metric_id(conn, resource_type, metric_name):
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id FROM metric_catalog WHERE service=%s AND metric_name=%s LIMIT 1",
        (resource_type, metric_name),
    )
    row = cursor.fetchone()
    cursor.close()
    return row["id"] if row else None


def _lookup_existing_threshold(conn, account_id, resource_type, metric_id):
    if metric_id is None:
        return None
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, warning_value, critical_value, comparison, enabled FROM thresholds "
        "WHERE aws_account_id=%s AND resource_type=%s AND metric_id=%s",
        (account_id, resource_type, metric_id),
    )
    row = cursor.fetchone()
    cursor.close()
    return row


def _upsert_threshold(conn, account_id, resource_type, metric_id, warning, critical, comparison):
    """Same statement shape as POST /api/settings/thresholds, except
    evaluation_period/enabled are only set on a brand-new row (default
    5 min / enabled) — an existing row's evaluation_period is an
    operator's own tuning and is deliberately left untouched here,
    only warning/critical/comparison are ever synced from AWS."""
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO thresholds
          (aws_account_id, resource_type, metric_id, warning_value,
           critical_value, comparison, evaluation_period, enabled)
        VALUES (%s,%s,%s,%s,%s,%s,5,1)
        ON DUPLICATE KEY UPDATE
          warning_value  = VALUES(warning_value),
          critical_value = VALUES(critical_value),
          comparison     = VALUES(comparison)
        """,
        (account_id, resource_type, metric_id, warning, critical, comparison),
    )
    conn.commit()
    new_id = cursor.lastrowid
    cursor.close()
    return new_id


def _write_audit(conn, actor, action, detail):
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audit_logs (actor, action, payload) VALUES (%s,%s,%s)",
            (actor, action, json.dumps({"detail": detail, "role": "ADMIN"})),
        )
        conn.commit()
        cursor.close()
    except Exception as e:
        logger.warning(f"Audit log write failed: {e}")


# ── Per-account scan ──────────────────────────────────────────────────

def scan_account(conn, account, apply_changes: bool):
    account_id = account["id"]
    session = _get_session(account)
    if session is None:
        return {"account": account["account_name"], "error": "could not obtain AWS session", "groups": []}

    regions = _account_regions(conn, account_id, account.get("default_region"))
    resources_index = _load_resources_index(conn, account_id)

    # key = (resource_type, metric_name, comparison) -> list of alarm matches
    groups = {}
    unmatched = []
    skipped_math_total = 0
    regions_scanned = []

    for region in regions:
        try:
            cw = session.client("cloudwatch", region_name=region)
            alarms, skipped_math = _scan_region_alarms(cw)
            skipped_math_total += skipped_math
            regions_scanned.append(region)
        except Exception as e:
            logger.warning(f"[{account['account_name']}/{region}] DescribeAlarms failed: {e}")
            continue

        for alarm in alarms:
            namespace   = alarm.get("Namespace")
            metric_name = alarm.get("MetricName")
            comparison  = COMPARISON_MAP.get(alarm.get("ComparisonOperator"))
            threshold   = alarm.get("Threshold")
            if comparison is None or threshold is None or not namespace:
                continue

            report_only = False
            resource_type = NAMESPACE_TO_TYPE.get(namespace)
            if resource_type is None:
                resource_type = NAMESPACE_TO_TYPE_REPORT_ONLY.get(namespace)
                report_only = True
            if resource_type is None:
                continue  # a namespace this app doesn't track resources for at all

            dims = _extract_dims(alarm)
            resource = _match_resource(resources_index, resource_type, dims)
            if not resource:
                unmatched.append({
                    "region": region, "namespace": namespace, "metric_name": metric_name,
                    "alarm_name": alarm.get("AlarmName"), "dimensions": dims,
                })
                continue

            key = (resource_type, metric_name, comparison)
            groups.setdefault(key, []).append({
                "threshold":     threshold,
                "alarm_name":    alarm.get("AlarmName"),
                "resource_name": resource.get("name") or resource["resource_id"],
                "resource_id":   resource["resource_id"],
                "region":        region,
                "report_only":   report_only,
            })

    # ── Reconcile each group against the app's current threshold ──────
    results = []
    for (resource_type, metric_name, comparison), entries in groups.items():
        values = [e["threshold"] for e in entries]
        warning, critical = _split_warning_critical(values, comparison)
        report_only = any(e["report_only"] for e in entries)

        metric_id = _lookup_metric_id(conn, resource_type, metric_name)
        existing = _lookup_existing_threshold(conn, account_id, resource_type, metric_id)

        in_sync = bool(
            existing
            and float(existing["warning_value"])  == float(warning)
            and float(existing["critical_value"]) == float(critical)
            and existing["comparison"]            == comparison
        )

        action_taken = "already in sync" if in_sync else "would sync" if not apply_changes else "not applied"
        if not in_sync and metric_id is None:
            action_taken = "no matching metric_catalog entry — cannot sync"
        elif not in_sync and report_only:
            action_taken = "diverges — REPORT ONLY (see ELB/ALB/NLB caveat), not auto-applied"
        elif not in_sync and apply_changes and metric_id is not None:
            _upsert_threshold(conn, account_id, resource_type, metric_id, warning, critical, comparison)
            _write_audit(
                conn, "cwalarm-sync",
                "Threshold synced from CloudWatch alarm",
                f"account={account['account_name']} resource_type={resource_type} "
                f"metric={metric_name} warning={warning} critical={critical} comparison={comparison} "
                f"(from {len(entries)} AWS alarm(s): {', '.join(sorted({e['alarm_name'] for e in entries}))})",
            )
            action_taken = "SYNCED"

        results.append({
            "resource_type": resource_type,
            "metric_name":   metric_name,
            "comparison":    comparison,
            "aws_warning":   warning,
            "aws_critical":  critical,
            "app_warning":   existing["warning_value"]  if existing else None,
            "app_critical":  existing["critical_value"] if existing else None,
            "in_sync":       in_sync,
            "action":        action_taken,
            "report_only":   report_only,
            "source_alarms": [
                {"alarm_name": e["alarm_name"], "resource": e["resource_name"],
                 "region": e["region"], "threshold": e["threshold"]}
                for e in entries
            ],
        })

    return {
        "account":            account["account_name"],
        "account_db_id":      account_id,
        "regions_scanned":    regions_scanned,
        "skipped_math_alarms": skipped_math_total,
        "unmatched_alarms":   unmatched,
        "groups":             results,
    }


# ── Reporting ─────────────────────────────────────────────────────────

def print_report(all_results, apply_changes: bool):
    print("=" * 78)
    print(f"CloudWatch Alarm -> App Threshold sync  ({'APPLY' if apply_changes else 'REPORT ONLY'})")
    print(f"Run at {datetime.now(timezone.utc).isoformat()}")
    print("=" * 78)

    total_groups, total_divergent, total_synced = 0, 0, 0

    for acc in all_results:
        print(f"\n--- {acc['account']} (id={acc['account_db_id']}) ---")
        if acc.get("error"):
            print(f"  ERROR: {acc['error']}")
            continue
        print(f"  Regions scanned: {', '.join(acc['regions_scanned']) or '(none)'}")
        if acc["skipped_math_alarms"]:
            print(f"  Skipped {acc['skipped_math_alarms']} metric-math/anomaly-detection alarm(s) — no flat metric to match.")
        if acc["unmatched_alarms"]:
            print(f"  {len(acc['unmatched_alarms'])} alarm(s) on a resource this app doesn't currently track (not in `resources`):")
            for u in acc["unmatched_alarms"][:10]:
                print(f"    - [{u['region']}] {u['alarm_name']}  ({u['namespace']}/{u['metric_name']})")
            if len(acc["unmatched_alarms"]) > 10:
                print(f"    ... and {len(acc['unmatched_alarms']) - 10} more")

        if not acc["groups"]:
            print("  No matching CloudWatch alarms found on tracked resources.")
            continue

        for g in acc["groups"]:
            total_groups += 1
            if not g["in_sync"]:
                total_divergent += 1
            if g["action"] == "SYNCED":
                total_synced += 1

            app_desc = (
                f"warning={g['app_warning']} critical={g['app_critical']}"
                if g["app_warning"] is not None else "(no threshold set in app yet)"
            )
            print(f"\n  [{g['resource_type']}] {g['metric_name']}  (comparison {g['comparison']})")
            print(f"    AWS alarms say:  warning={g['aws_warning']}  critical={g['aws_critical']}")
            print(f"    App currently:   {app_desc}")
            print(f"    -> {g['action']}")
            for s in g["source_alarms"]:
                print(f"       from: \"{s['alarm_name']}\" on {s['resource']} [{s['region']}] threshold={s['threshold']}")

    print("\n" + "=" * 78)
    print(f"Totals: {total_groups} metric group(s) checked, {total_divergent} divergent from AWS, "
          f"{total_synced} synced this run.")
    if not apply_changes and total_divergent:
        print("Re-run with --apply to write these to the app's thresholds table.")
    print("=" * 78)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                         help="Write divergent thresholds to the DB (default: report only, no writes)")
    parser.add_argument("--account", type=int, default=None,
                         help="Limit to one aws_accounts.id (default: every active AWS account)")
    parser.add_argument("--json-out", type=str, default=None,
                         help="Also write the full structured report to this JSON file")
    args = parser.parse_args()

    conn = get_connection()
    try:
        accounts = _get_active_aws_accounts(conn, args.account)
        if not accounts:
            print("No active AWS accounts found" + (f" for id={args.account}" if args.account else "") + ".")
            sys.exit(1)

        all_results = []
        for account in accounts:
            logger.info(f"Scanning {account['account_name']} (id={account['id']})...")
            all_results.append(scan_account(conn, account, args.apply))

        print_report(all_results, args.apply)

        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as f:
                json.dump(all_results, f, indent=2, default=str)
            print(f"\nFull report written to {args.json_out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
