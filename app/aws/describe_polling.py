# app/aws/describe_polling.py
"""
Free, zero-CloudWatch-billing status signals via AWS Describe APIs.

DescribeInstanceStatus and DescribeTargetHealth are plain EC2/ELB API calls,
NOT CloudWatch — they cost nothing and return sub-second-fresh state,
compared to CloudWatch's own ~1-5 min publish delay for the equivalent
StatusCheckFailed / HealthyHostCount metrics. This module polls them on its
own fast, free cadence and pushes results straight into VictoriaMetrics via
its Prometheus text-format import endpoint, so the rest of the app (Grafana,
FastAPI reads) can query them exactly like any YACE-scraped series.

This REPLACES the need to CloudWatch-poll EC2 StatusCheckFailed at all —
once this is running, you can disable/unselect that metric in the Metric
Catalog for accounts using it, cutting one more CloudWatch job entirely.
ALB HealthyHostCount/UnHealthyHostCount can stay dual-sourced (YACE keeps
them for historical trend data at 60s per the "critical" tier override in
seed_metric_catalog.py; this module gives the live/current-second view for
list pages) or you can drop them from YACE too once you trust this path.

Metric names pushed:
  aws_ec2_status_check_failed_describe{dimension_InstanceId="..."}   0|1
  aws_alb_healthy_host_count_describe{dimension_TargetGroup="..."}   int
  aws_alb_unhealthy_host_count_describe{dimension_TargetGroup="..."} int

EC2 StatusCheckFailed is ALSO written into the local `metrics` table
(see poll_ec2_status() below) -- so check_and_write_alerts() (Settings'
"Check Thresholds Now") can read it locally instead of falling through
to a real, billed CloudWatch call. Fixed a real bug found while
investigating this file for Phase 5 -- see apply_final_cleanup.py.
ALB target-group health stays VM-only: no "target_group" resource type
exists in `resources` to write against, and this module's external-
Grafana-compatible push (see above) is the only known consumer for it.
"""
import time
import logging
import requests

from app.db import get_connection
from app.collector.metrics_writer import write_metrics_batch
from app.aws.collector_direct import get_session
from app.clients.vm_client import VM_URL

logger = logging.getLogger(__name__)


def _push_to_vm(lines: list) -> None:
    if not lines:
        return
    try:
        r = requests.post(
            f"{VM_URL}/api/v1/import/prometheus",
            data="\n".join(lines).encode(),
            timeout=5,
        )
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"describe_polling: VM push failed: {e}")


def _get_ec2_instances_by_region():
    """{(account_row): [instance_id, ...]} grouped by account+region, active accounts only."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT a.id AS account_db_id, a.role_arn, a.external_id, a.default_region,
                   r.id AS resource_db_id, r.resource_id
            FROM resources r
            JOIN aws_accounts a ON a.id = r.aws_account_id
            WHERE r.resource_type = 'ec2'
              AND r.instance_state = 'running'
              AND a.status = 'active'
        """)
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()

    grouped = {}
    for row in rows:
        key = (row["account_db_id"], row["role_arn"], row["external_id"], row["default_region"])
        grouped.setdefault(key, []).append((row["resource_id"], row["resource_db_id"]))
    return grouped


def _session_for(role_arn, external_id, region):
    if role_arn:
        from app.aws.sts import assume_role
        return assume_role(role_arn, external_id)
    return get_session(region)


def poll_ec2_status() -> int:
    """
    DescribeInstanceStatus for every running EC2 instance across all active
    accounts — free, not CloudWatch-billed. Call on a fast loop (30-60s);
    it costs nothing extra to run often. Returns count of instances polled.
    """
    total = 0
    for (account_db_id, role_arn, external_id, region), instance_pairs in _get_ec2_instances_by_region().items():
        if not region or not instance_pairs:
            continue
        instance_ids = [iid for iid, _rdid in instance_pairs]
        resource_db_id_by_iid = dict(instance_pairs)
        try:
            session = _session_for(role_arn, external_id, region)
            ec2 = session.client("ec2", region_name=region)
            ts = int(time.time() * 1000)
            lines = []
            local_rows = []  # (resource_db_id, "statuscheckfailed", value) for the `metrics` table
            # DescribeInstanceStatus accepts up to 100 IDs per call — chunk defensively.
            for i in range(0, len(instance_ids), 100):
                chunk = instance_ids[i:i + 100]
                resp = ec2.describe_instance_status(InstanceIds=chunk, IncludeAllInstances=True)
                for s in resp.get("InstanceStatuses", []):
                    iid = s["InstanceId"]
                    sys_ok = s.get("SystemStatus", {}).get("Status") == "ok"
                    inst_ok = s.get("InstanceStatus", {}).get("Status") == "ok"
                    failed = 0 if (sys_ok and inst_ok) else 1
                    lines.append(
                        f'aws_ec2_status_check_failed_describe{{dimension_InstanceId="{iid}",dimension_AccountId="{account_db_id}"}} {failed} {ts}'
                    )
                    resource_db_id = resource_db_id_by_iid.get(iid)
                    if resource_db_id is not None:
                        local_rows.append((resource_db_id, "statuscheckfailed", float(failed)))
            _push_to_vm(lines)
            if local_rows:
                write_metrics_batch(local_rows)
            total += len(instance_ids)
        except Exception as e:
            logger.warning(f"describe_polling: EC2 status [{region}, account {account_db_id}]: {e}")
    return total


def _get_target_groups_by_region():
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
    return grouped


def _elb_resource_db_ids_by_arn(account_db_id):
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
    return total


def poll_all() -> dict:
    """Run both free pollers once. Safe to call on any cadence — zero AWS cost either way."""
    ec2_count = poll_ec2_status()
    alb_count = poll_alb_target_health()
    logger.info(f"describe_polling: {ec2_count} EC2 instances, {alb_count} target groups (free, 0 GetMetricData calls)")
    return {"ec2_instances": ec2_count, "target_groups": alb_count}
