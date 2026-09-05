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
"""
import time
import logging
import requests

from app.db import get_connection
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
                   r.resource_id
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
        grouped.setdefault(key, []).append(row["resource_id"])
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
    for (account_db_id, role_arn, external_id, region), instance_ids in _get_ec2_instances_by_region().items():
        if not region or not instance_ids:
            continue
        try:
            session = _session_for(role_arn, external_id, region)
            ec2 = session.client("ec2", region_name=region)
            ts = int(time.time() * 1000)
            lines = []
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
            _push_to_vm(lines)
            total += len(instance_ids)
        except Exception as e:
            logger.warning(f"describe_polling: EC2 status [{region}, account {account_db_id}]: {e}")
    return total


def _get_target_groups_by_region():
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
    return grouped


def poll_alb_target_health() -> int:
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
    return total


def poll_all() -> dict:
    """Run both free pollers once. Safe to call on any cadence — zero AWS cost either way."""
    ec2_count = poll_ec2_status()
    alb_count = poll_alb_target_health()
    logger.info(f"describe_polling: {ec2_count} EC2 instances, {alb_count} target groups (free, 0 GetMetricData calls)")
    return {"ec2_instances": ec2_count, "target_groups": alb_count}
