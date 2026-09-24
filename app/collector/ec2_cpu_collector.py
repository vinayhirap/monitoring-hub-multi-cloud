from datetime import datetime
from app.db import get_connection
from app.aws.cloudwatch import fetch_metric
from app.collector.metrics_writer import write_metric
import logging

logger = logging.getLogger(__name__)


def collect_ec2_metrics():
    logger.info("EC2 collector started")

    # Bug fix: no try/finally around the connection -- a transient DB
    # error on execute()/fetchall() would leak a connection out of the
    # pool (primer bug class #2). This module has no live callers
    # anywhere in the codebase (superseded by collector_direct.py's
    # path) but is fixed here for defense-in-depth rather than left as
    # a live leak if it's ever wired back in.
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT r.id, r.resource_id, a.default_region
            FROM resources r
            JOIN aws_accounts a ON a.id = r.aws_account_id
            WHERE r.resource_type = 'ec2'
              AND r.instance_state != 'terminated'
        """)
        instances = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    logger.info(f"Collecting metrics for {len(instances)} EC2 instances")

    for inst in instances:
        if not inst["default_region"]:
            logger.warning(f"Skipping {inst['resource_id']} â€” no region set")
            continue

        value = fetch_metric(
            namespace   = "AWS/EC2",
            metric_name = "CPUUtilization",
            dimensions  = [{"Name": "InstanceId", "Value": inst["resource_id"]}],
            statistic   = "Average",
            period      = 300,
            minutes     = 10,
            region      = inst["default_region"],
        )

        logger.debug(f"CloudWatch => {inst['resource_id']} = {value}")

        if value is None:
            continue

        write_metric(
            resource_db_id = inst["id"],
            metric_name    = "cpuutilization",
            metric_value   = value
        )
