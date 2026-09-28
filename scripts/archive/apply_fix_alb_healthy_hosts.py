#!/usr/bin/env python3
"""
apply_fix_alb_healthy_hosts.py
========================================
Fixes a real bug found while investigating the "Healthy/Unhealthy Hosts
show 'No data in last 6H' forever" report: these two metrics have
NEVER worked via ANY code path in this app -- not Phase 1's local
collection, not its boto3 fallback -- for as long as this deployment
has existed.

CONFIRMED AGAINST AWS'S OWN DOCS, NOT GUESSED
--------------------------------------------------
AWS's CloudWatch documentation for AWS/ApplicationELB is explicit:
HealthyHostCount and UnHealthyHostCount are ONLY published with
dimension combinations that include BOTH LoadBalancer AND TargetGroup
-- never LoadBalancer alone. (https://docs.aws.amazon.com/elasticload
balancing/latest/application/load-balancer-cloudwatch-metrics.html --
also independently confirmed via multiple real-world bug reports from
other tools hitting this exact same mistake, e.g. aws/aws-cdk#5046.)

Traced both places this app queries these two metrics and confirmed
both only ever supply LoadBalancer:
  - app/collector/metrics/runner.py's ELB_METRICS includes
    HealthyHostCount, built via _DIM_NAME["elb"] = "LoadBalancer" only
    -- this GetMetricData call has been returning empty for this metric
    on every single cycle, for every account, forever. Real, wasted
    CloudWatch cost for zero data, not just a missing feature.
  - app/aws/collector_direct.py's _get_elb_metric_series() boto3
    fallback for BOTH healthy_hosts and unhealthy_hosts also builds
    `dims = [{"Name": "LoadBalancer", "Value": lb_dim}]` with no
    TargetGroup -- so even the "fall back to a live call" safety net
    was equally broken for these two specific fields. There was no
    path to ever getting this data.

THE FIX -- USING CODE THAT ALREADY WORKS, NOT WRITING NEW API CALLS
------------------------------------------------------------------------
app/aws/describe_polling.py's poll_alb_target_health() ALREADY computes
correct healthy/unhealthy counts, per target group, via
DescribeTargetHealth (free, no CloudWatch dimension problem at all --
it's not a CloudWatch call). It just only ever pushed the result to VM.
This script:

1. Extends _get_target_groups_by_region() to also capture each target
   group's LoadBalancerArns (already returned by describe_target_groups,
   was just being discarded).
2. Extends poll_alb_target_health() to aggregate healthy/unhealthy
   counts PER LOAD BALANCER (summing across all target groups attached
   to that LB -- a load balancer can have multiple target groups; a sum
   is the reasonable "total healthy targets behind this LB" figure) and
   write that into the local `metrics` table too, keyed by the LB's
   resource_db_id (resources.resource_type='elb', matched by ARN) --
   dual-write, VM push kept exactly as before for any external Grafana
   consumer.
3. app/aws/collector_direct.py's _get_elb_metric_series(): healthy_hosts
   and unhealthy_hosts now read from this new, correct local source
   ("healthyhosts_describe" / "unhealthyhosts_describe" -- deliberately
   different names from the old broken "healthyhosts", so there's no
   ambiguity about which source populated a given row) instead of the
   old always-empty CloudWatch-based path. Removed from the "missing ->
   boto3 fallback" list entirely -- falling back to an equally-broken
   CloudWatch call would just waste an API call for the same empty
   result.
4. app/collector/metrics/runner.py: removed HealthyHostCount from
   ELB_METRICS -- it never worked, so this stops paying for a
   GetMetricData call that has never once returned data.
5. check_and_write_alerts()'s LOCAL_METRIC_STUB: HealthyHostCount now
   maps to the new working "healthyhosts_describe" metric name, and
   UnHealthyHostCount is ADDED (it never had ANY local source before --
   now it does).

TESTED: the target-group-to-LB aggregation logic was exercised with a
mocked DB and mocked DescribeTargetHealth/DescribeTargetGroups
responses (two target groups belonging to the same LB, one healthy
target in each -- confirmed the write is a SUM: 2 healthy total, not
1), confirmed the VM push payload is unchanged, and confirmed
_get_elb_metric_series's new field names route to the new metric_history
query correctly. NOT tested: an actual live poll cycle against a real
AWS account -- no credentials/network access available here; verify
per the checklist below.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_alb_healthy_hosts.py --dry-run
    python3 apply_fix_alb_healthy_hosts.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

DESCRIBE_TG_OLD = '''def _get_target_groups_by_region():
    """{(account_db_id, role_arn, external_id, region): [tg_arn, ...]}"""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id AS account_db_id, role_arn, external_id, default_region
            FROM aws_accounts WHERE status = 'active'
        """)
        accounts = cur.fetchall()
    finally:
        cur.close(); conn.close()

    grouped = {}
    for a in accounts:
        region = a["default_region"]
        if not region:
            continue
        try:
            session = _session_for(a["role_arn"], a["external_id"], region)
            elbv2 = session.client("elbv2", region_name=region)
            tgs = elbv2.describe_target_groups().get("TargetGroups", [])
            arns = [tg["TargetGroupArn"] for tg in tgs]
            if arns:
                grouped[(a["account_db_id"], a["role_arn"], a["external_id"], region)] = arns
        except Exception as e:
            logger.warning(f"describe_polling: list target groups [{region}]: {e}")
    return grouped'''

DESCRIBE_TG_NEW = '''def _get_target_groups_by_region():
    """
    {(account_db_id, role_arn, external_id, region): [(tg_arn, [lb_arn, ...]), ...]}
    LoadBalancerArns is captured now (describe_target_groups already
    returns it -- it was just being discarded before) so
    poll_alb_target_health() can aggregate healthy/unhealthy counts up
    to the load-balancer level, not just per target group. See
    apply_fix_alb_healthy_hosts.py.
    """
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id AS account_db_id, role_arn, external_id, default_region
            FROM aws_accounts WHERE status = 'active'
        """)
        accounts = cur.fetchall()
    finally:
        cur.close(); conn.close()

    grouped = {}
    for a in accounts:
        region = a["default_region"]
        if not region:
            continue
        try:
            session = _session_for(a["role_arn"], a["external_id"], region)
            elbv2 = session.client("elbv2", region_name=region)
            tgs = elbv2.describe_target_groups().get("TargetGroups", [])
            pairs = [(tg["TargetGroupArn"], tg.get("LoadBalancerArns") or []) for tg in tgs]
            if pairs:
                grouped[(a["account_db_id"], a["role_arn"], a["external_id"], region)] = pairs
        except Exception as e:
            logger.warning(f"describe_polling: list target groups [{region}]: {e}")
    return grouped'''

POLL_ALB_OLD = '''def poll_alb_target_health() -> int:
    """
    DescribeTargetHealth for every target group across all active accounts —
    free, not CloudWatch-billed, sub-second-fresh. Returns count of target
    groups polled.
    """
    total = 0
    for (account_db_id, role_arn, external_id, region), tg_arns in _get_target_groups_by_region().items():
        try:
            session = _session_for(role_arn, external_id, region)
            elbv2 = session.client("elbv2", region_name=region)
            ts = int(time.time() * 1000)
            lines = []
            for tg_arn in tg_arns:
                try:
                    health = elbv2.describe_target_health(TargetGroupArn=tg_arn)
                except Exception:
                    continue
                descs = health.get("TargetHealthDescriptions", [])
                healthy = sum(1 for t in descs if t.get("TargetHealth", {}).get("State") == "healthy")
                unhealthy = len(descs) - healthy
                tg_id = tg_arn.split("targetgroup/")[-1]
                lines.append(
                    f'aws_alb_healthy_host_count_describe{{dimension_TargetGroup="{tg_id}",dimension_AccountId="{account_db_id}"}} {healthy} {ts}'
                )
                lines.append(
                    f'aws_alb_unhealthy_host_count_describe{{dimension_TargetGroup="{tg_id}",dimension_AccountId="{account_db_id}"}} {unhealthy} {ts}'
                )
                total += 1
            _push_to_vm(lines)
        except Exception as e:
            logger.warning(f"describe_polling: ALB health [{region}, account {account_db_id}]: {e}")
    return total'''

POLL_ALB_NEW = '''def _elb_resource_db_ids_by_arn(account_db_id):
    """{lb_arn: resource_db_id} for this account's discovered load balancers."""
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, resource_id FROM resources
            WHERE aws_account_id = %s AND resource_type = 'elb'
        """, (account_db_id,))
        return {row["resource_id"]: row["id"] for row in cur.fetchall()}
    finally:
        cur.close(); conn.close()


def poll_alb_target_health() -> int:
    """
    DescribeTargetHealth for every target group across all active accounts —
    free, not CloudWatch-billed, sub-second-fresh. Returns count of target
    groups polled.

    ALSO writes healthy/unhealthy counts into the local `metrics` table,
    aggregated PER LOAD BALANCER (summed across every target group
    attached to that LB) -- this is the only working source for these
    two metrics in this entire app. CloudWatch's HealthyHostCount/
    UnHealthyHostCount require BOTH LoadBalancer and TargetGroup
    dimensions together; neither Phase 1's collector nor its boto3
    fallback ever supplied TargetGroup, so those paths have never once
    returned data. See apply_fix_alb_healthy_hosts.py. VM push is
    unchanged (still per-target-group, for any external Grafana
    consumer -- this is a dual-write, not a replacement).
    """
    total = 0
    for (account_db_id, role_arn, external_id, region), tg_pairs in _get_target_groups_by_region().items():
        try:
            session = _session_for(role_arn, external_id, region)
            elbv2 = session.client("elbv2", region_name=region)
            ts = int(time.time() * 1000)
            lines = []
            lb_totals = {}  # lb_arn -> [healthy, unhealthy]
            for tg_arn, lb_arns in tg_pairs:
                try:
                    health = elbv2.describe_target_health(TargetGroupArn=tg_arn)
                except Exception:
                    continue
                descs = health.get("TargetHealthDescriptions", [])
                healthy = sum(1 for t in descs if t.get("TargetHealth", {}).get("State") == "healthy")
                unhealthy = len(descs) - healthy
                tg_id = tg_arn.split("targetgroup/")[-1]
                lines.append(
                    f'aws_alb_healthy_host_count_describe{{dimension_TargetGroup="{tg_id}",dimension_AccountId="{account_db_id}"}} {healthy} {ts}'
                )
                lines.append(
                    f'aws_alb_unhealthy_host_count_describe{{dimension_TargetGroup="{tg_id}",dimension_AccountId="{account_db_id}"}} {unhealthy} {ts}'
                )
                total += 1
                for lb_arn in lb_arns:
                    acc = lb_totals.setdefault(lb_arn, [0, 0])
                    acc[0] += healthy
                    acc[1] += unhealthy
            _push_to_vm(lines)

            if lb_totals:
                resource_ids_by_arn = _elb_resource_db_ids_by_arn(account_db_id)
                local_rows = []
                for lb_arn, (healthy_sum, unhealthy_sum) in lb_totals.items():
                    resource_db_id = resource_ids_by_arn.get(lb_arn)
                    if resource_db_id is None:
                        continue
                    local_rows.append((resource_db_id, "healthyhosts_describe", float(healthy_sum)))
                    local_rows.append((resource_db_id, "unhealthyhosts_describe", float(unhealthy_sum)))
                if local_rows:
                    write_metrics_batch(local_rows)
        except Exception as e:
            logger.warning(f"describe_polling: ALB health [{region}, account {account_db_id}]: {e}")
    return total'''

COLLECTOR_ELB_FIELDS_OLD = '''            "healthy_hosts":      vm_series("healthyhosts"),
            "unhealthy_hosts":    vm_series("unhealthyhosts"),'''

COLLECTOR_ELB_FIELDS_NEW = '''            # healthyhosts/unhealthyhosts (the plain names) NEVER had data via
            # ANY path -- confirmed against AWS's own docs: CloudWatch's
            # HealthyHostCount/UnHealthyHostCount require BOTH LoadBalancer
            # AND TargetGroup dimensions, which this app's CloudWatch-based
            # collection and its boto3 fallback never supplied. Now reads
            # from describe_polling.py's DescribeTargetHealth-based
            # aggregation instead (no CloudWatch dimension problem at all,
            # since it's not a CloudWatch call). See
            # apply_fix_alb_healthy_hosts.py.
            "healthy_hosts":      _metric_history_query_range("elb", lb_name, "healthyhosts_describe", start, end, match_field="name"),
            "unhealthy_hosts":    _metric_history_query_range("elb", lb_name, "unhealthyhosts_describe", start, end, match_field="name"),'''

COLLECTOR_FALLBACK_OLD = '''            fallback_map = {
                "requests":           ("RequestCount", "Sum"),
                "errors_5xx":         ("HTTPCode_Target_5XX_Count", "Sum"),
                "errors_4xx":         ("HTTPCode_Target_4XX_Count", "Sum"),
                "errors_elb_5xx":     ("HTTPCode_ELB_5XX_Count", "Sum"),
                "latency":            ("TargetResponseTime", "Average"),
                "healthy_hosts":      ("HealthyHostCount", "Average"),
                "unhealthy_hosts":    ("UnHealthyHostCount", "Average"),
                "active_connections": ("ActiveConnectionCount", "Average"),
                "new_connections":    ("NewConnectionCount", "Sum"),
            }
            queries = [_make_query(k, ns, fallback_map[k][0], dims, fallback_map[k][1])
                       for k in missing]'''

COLLECTOR_FALLBACK_NEW = '''            fallback_map = {
                "requests":           ("RequestCount", "Sum"),
                "errors_5xx":         ("HTTPCode_Target_5XX_Count", "Sum"),
                "errors_4xx":         ("HTTPCode_Target_4XX_Count", "Sum"),
                "errors_elb_5xx":     ("HTTPCode_ELB_5XX_Count", "Sum"),
                "latency":            ("TargetResponseTime", "Average"),
                # healthy_hosts/unhealthy_hosts deliberately NOT here --
                # this fallback only ever supplies a LoadBalancer
                # dimension, and CloudWatch requires TargetGroup too for
                # these two metrics (confirmed against AWS's docs). This
                # fallback would waste a real API call for a guaranteed
                # empty result. See apply_fix_alb_healthy_hosts.py --
                # these two are populated by describe_polling.py instead,
                # never by this fallback.
                "active_connections": ("ActiveConnectionCount", "Average"),
                "new_connections":    ("NewConnectionCount", "Sum"),
            }
            queries = [_make_query(k, ns, fallback_map[k][0], dims, fallback_map[k][1])
                       for k in missing if k in fallback_map]'''

LOCAL_STUB_OLD = '''        ("alb", "RequestCount"):              "requestcount",
        ("alb", "HTTPCode_Target_5XX_Count"): "errors5xx",
        ("alb", "TargetResponseTime"):        "responselatency",
        ("alb", "HealthyHostCount"):          "healthyhosts",
    }'''

LOCAL_STUB_NEW = '''        ("alb", "RequestCount"):              "requestcount",
        ("alb", "HTTPCode_Target_5XX_Count"): "errors5xx",
        ("alb", "TargetResponseTime"):        "responselatency",
        # Both HealthyHostCount and UnHealthyHostCount now map to
        # describe_polling.py's DescribeTargetHealth-based aggregation --
        # neither ever had a working CloudWatch-based source (confirmed:
        # both require a TargetGroup dimension this app never supplied).
        # UnHealthyHostCount is a NEW entry here -- it never had ANY
        # local source before this fix. See apply_fix_alb_healthy_hosts.py.
        ("alb", "HealthyHostCount"):          "healthyhosts_describe",
        ("alb", "UnHealthyHostCount"):        "unhealthyhosts_describe",
    }'''

RUNNER_DOCSTRING_OLD = '''          - ELB:    RequestCount, 5XX, TargetResponseTime, HealthyHostCount
                    (4XX DROPPED — client noise; UnHealthyHostCount DROPPED — redundant)'''

RUNNER_DOCSTRING_NEW = '''          - ELB:    RequestCount, 5XX, TargetResponseTime
                    (4XX DROPPED — client noise. HealthyHostCount /
                    UnHealthyHostCount REMOVED entirely, not just
                    trimmed — confirmed against AWS's own docs that both
                    require a TargetGroup dimension this collector never
                    supplied, so they never returned data via this path;
                    both are now sourced from app/aws/describe_polling.py's
                    free DescribeTargetHealth-based aggregation instead.
                    See apply_fix_alb_healthy_hosts.py.)'''

RUNNER_ELB_OLD = '''ELB_METRICS = [
    # 4XX DROPPED — mostly client noise
    # UnHealthyHostCount DROPPED — redundant with HealthyHostCount
    ("RequestCount",              "requestcount",    "Sum",     "AWS/ApplicationELB"),
    ("HTTPCode_Target_5XX_Count", "errors5xx",       "Sum",     "AWS/ApplicationELB"),
    ("TargetResponseTime",        "responselatency", "Average", "AWS/ApplicationELB"),
    ("HealthyHostCount",          "healthyhosts",    "Average", "AWS/ApplicationELB"),
]'''

RUNNER_ELB_NEW = '''ELB_METRICS = [
    # 4XX DROPPED — mostly client noise
    # HealthyHostCount / UnHealthyHostCount REMOVED (apply_fix_alb_healthy_hosts.py) --
    # confirmed against AWS's own docs that these require BOTH
    # LoadBalancer AND TargetGroup dimensions; this collector only ever
    # supplied LoadBalancer, so this GetMetricData call has NEVER once
    # returned data for either metric -- pure wasted CloudWatch cost.
    # Both are now correctly sourced from describe_polling.py's free
    # DescribeTargetHealth-based aggregation instead (see
    # app/aws/describe_polling.py's poll_alb_target_health()).
    ("RequestCount",              "requestcount",    "Sum",     "AWS/ApplicationELB"),
    ("HTTPCode_Target_5XX_Count", "errors5xx",       "Sum",     "AWS/ApplicationELB"),
    ("TargetResponseTime",        "responselatency", "Average", "AWS/ApplicationELB"),
]'''


def die(msg):
    print(f"\n[ABORT] {msg}", file=sys.stderr)
    sys.exit(1)


def find_repo_root():
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(cur, "app", "auth", "security.py")) and \
           os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            die("Could not locate the monitoring-hub-multi-cloud repo root.")
        cur = parent


def backup(path):
    bpath = path + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(path, bpath)
    return bpath


def prepare_patch(path, label, replacements, done_marker):
    if not os.path.exists(path):
        die(f"{label} not found at {path}.")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    if done_marker in content:
        return None, f"{label} already patched -- skipping."
    new_content = content
    for old, new in replacements:
        n = new_content.count(old)
        if n != 1:
            die(f"{label}: expected exactly 1 match for one of the expected blocks, found {n}. "
                f"File may differ from what this script expects.")
        new_content = new_content.replace(old, new, 1)
    return new_content, f"{label}: OK ({len(new_content) - len(content):+d} bytes)"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="(default; kept for backward compatibility)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    repo_root = find_repo_root()
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    describe_path = os.path.join(repo_root, "app", "aws", "describe_polling.py")
    collector_path = os.path.join(repo_root, "app", "aws", "collector_direct.py")
    runner_path = os.path.join(repo_root, "app", "collector", "metrics", "runner.py")

    results = []

    describe_content, describe_note = prepare_patch(
        describe_path, "app/aws/describe_polling.py",
        [(DESCRIBE_TG_OLD, DESCRIBE_TG_NEW), (POLL_ALB_OLD, POLL_ALB_NEW)],
        "_elb_resource_db_ids_by_arn",
    )
    results.append((describe_path, "app/aws/describe_polling.py", describe_content, describe_note))

    collector_content, collector_note = prepare_patch(
        collector_path, "app/aws/collector_direct.py",
        [
            (COLLECTOR_ELB_FIELDS_OLD, COLLECTOR_ELB_FIELDS_NEW),
            (COLLECTOR_FALLBACK_OLD, COLLECTOR_FALLBACK_NEW),
            (LOCAL_STUB_OLD, LOCAL_STUB_NEW),
        ],
        "healthyhosts_describe",
    )
    results.append((collector_path, "app/aws/collector_direct.py", collector_content, collector_note))

    runner_content, runner_note = prepare_patch(
        runner_path, "app/collector/metrics/runner.py",
        [(RUNNER_DOCSTRING_OLD, RUNNER_DOCSTRING_NEW), (RUNNER_ELB_OLD, RUNNER_ELB_NEW)],
        "pure wasted CloudWatch cost",
    )
    results.append((runner_path, "app/collector/metrics/runner.py", runner_content, runner_note))

    print("\nFile patch plan:")
    for _, _, _, note in results:
        print(f"  {note}")

    if all(content is None for _, _, content, _ in results):
        print("\nNothing to do -- everything this script would change is already applied.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    for path, label, content, note in results:
        if content is None:
            continue
        backup(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"Patched {label}")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 40 --no-pager

  B) Wait for a describe_polling cycle (runs on its own fast loop,
     should be within a minute or two), then check the LB detail page
     -- Healthy Hosts / Unhealthy Hosts should now show real numbers
     instead of "No data in last 6H".

  C) Confirm in the DB:
       mysql -u monitor -p monitoring_hub -e \\
         "SELECT r.resource_id, m.metric_name, m.metric_value, m.metric_timestamp
          FROM metrics m JOIN resources r ON r.id = m.resource_id
          WHERE r.resource_type='elb' AND m.metric_name IN ('healthyhosts_describe','unhealthyhosts_describe');"

  D) Confirm no more wasted CloudWatch calls for the old broken metric:
     HealthyHostCount should no longer appear in the critical-tier GMD
     query logs for ELB.

  E) Review, commit, push:
       git status
       git diff app/aws/describe_polling.py app/aws/collector_direct.py app/collector/metrics/runner.py
       git add app/aws/describe_polling.py app/aws/collector_direct.py app/collector/metrics/runner.py apply_fix_alb_healthy_hosts.py
       git commit -m "fix(alb): HealthyHostCount/UnHealthyHostCount never worked via any code path -- CloudWatch requires both LoadBalancer and TargetGroup dimensions, which neither the collector nor its boto3 fallback ever supplied. Wired up describe_polling.py's already-correct DescribeTargetHealth computation to write locally instead of only to VM, aggregated per load balancer."
       git push origin main
""")


if __name__ == "__main__":
    main()
