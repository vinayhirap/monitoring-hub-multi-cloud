# app/collector/metrics_vm_sync.py
"""
Populates the `metrics` table from VictoriaMetrics (VM) for every resource
that has an ENABLED threshold, so alert_evaluator.py's evaluate_alerts()
has fresh data to read. This replaces the old boto3 GMD collector
(app/collector/metrics/runner.py, disabled during the VM/YACE migration)
as the writer for this table -- but reads from VM instead of CloudWatch,
so it's zero-cost (no GetMetricData, no GetMetricStatistics calls).

Only queries VM for (service, metric) pairs that actually have a threshold
configured -- same minimal-footprint principle as
collector_direct.py's check_and_write_alerts(). Resource lists come from
the `resources` DB table (populated by the discovery cycle), not live AWS
calls -- this job makes zero AWS API calls of any kind.

NOTE on _VM_METRIC_STUB below: this is a deliberate standalone copy of
the same mapping check_and_write_alerts() (app/aws/collector_direct.py)
keeps locally, current as of Aug 2026 (includes ALB + the 4 EBS ops/bytes
metrics). It is NOT imported from there, on purpose -- that mapping has
changed shape twice recently and is still actively evolving; duplicating
it here confines drift risk to this one dict instead of a fragile
cross-file refactor. If you extend VM coverage in check_and_write_alerts()
later, mirror the addition into _VM_METRIC_STUB by hand.

Thresholds with no VM series available yet (e.g. Lambda, RDS extended
stats -- see the Mumbai GMD cost audit, Aug 2026) are skipped and logged
as a summary count. Deliberately NOT falling back to boto3 here -- this
job runs on a schedule (every 5 min via scheduler.py's standard tier), and
a scheduled boto3 fallback would silently reopen the exact recurring GMD
cost this whole migration was meant to eliminate. If a skipped metric
turns out to matter, the fix is extending the YACE config to scrape it
(free), not adding a boto3 fallback here.

metric_catalog.metric_name is stored CamelCase (e.g. "CPUUtilization") --
this writes to metrics.metric_name using that same casing, matching what
evaluate_alerts()'s join against metric_catalog expects. (The old GMD
collector wrote lowercase names here, e.g. "cpuutilization" -- a likely-
unrelated pre-existing bug that would have made evaluate_alerts()'s join
fail even before the VM migration. Not fixing that old code path, just
not repeating the mistake here.)
"""
import logging

from app.db import get_connection
from app.clients.vm_client import vm_query_all
from app.collector.metrics_writer import write_metrics_batch

logger = logging.getLogger(__name__)

# svc -> dimension label YACE uses for this resource type
_VM_DIM_LABEL = {
    "ec2": "dimension_InstanceId",
    "ebs": "dimension_VolumeId",
    "rds": "dimension_DBInstanceIdentifier",
    "alb": "dimension_LoadBalancer",
}
# Explicit map, NOT a generic snake_case conversion -- YACE special-cases
# acronyms (CPUUtilization -> cpuutilization, not c_p_u_utilization).
# Keep this mirrored with check_and_write_alerts()'s local copy in
# app/aws/collector_direct.py -- see module docstring above.
_VM_METRIC_STUB = {
    ("ec2", "CPUUtilization"):      "aws_ec2_cpuutilization",
    ("ec2", "NetworkIn"):           "aws_ec2_network_in",       # confirmed live in VM -- Aug 2026
    ("ec2", "NetworkOut"):          "aws_ec2_network_out",      # confirmed live in VM -- Aug 2026
    # Free Describe-API path (fix #4, app/aws/describe_polling.py) --
    # NOT CloudWatch/YACE. Sub-second-fresh, zero GetMetricData cost.
    ("ec2", "StatusCheckFailed"):   "aws_ec2_status_check_failed_describe",

    ("ebs", "VolumeQueueLength"):   "aws_ebs_volume_queue_length",
    ("ebs", "BurstBalance"):        "aws_ebs_burst_balance",
    ("ebs", "VolumeReadOps"):       "aws_ebs_volume_read_ops",
    ("ebs", "VolumeWriteOps"):      "aws_ebs_volume_write_ops",
    ("ebs", "VolumeReadBytes"):     "aws_ebs_volume_read_bytes",
    ("ebs", "VolumeWriteBytes"):    "aws_ebs_volume_write_bytes",

    ("rds", "CPUUtilization"):      "aws_rds_cpuutilization",
    ("rds", "FreeStorageSpace"):    "aws_rds_free_storage_space",

    ("alb", "RequestCount"):              "aws_applicationelb_request_count",
    ("alb", "HTTPCode_Target_5XX_Count"): "aws_applicationelb_httpcode_target_5_xx_count",
    ("alb", "HTTPCode_Target_4XX_Count"): "aws_applicationelb_httpcode_target_4_xx_count",
    ("alb", "TargetResponseTime"):        "aws_applicationelb_target_response_time",
    ("alb", "HealthyHostCount"):          "aws_applicationelb_healthy_host_count",
    ("alb", "UnHealthyHostCount"):        "aws_applicationelb_un_healthy_host_count",
}
# Metrics pushed directly by describe_polling.py are raw gauges (no
# Average/Sum/Maximum suffix) -- skip the generic stat-suffix step for them.
_VM_NO_SUFFIX = {"aws_ec2_status_check_failed_describe"}
_STAT_SUFFIX  = {"Average": "average", "Sum": "sum", "Maximum": "maximum"}


def _fetch_enabled_threshold_targets():
    """
    One row per (resource, metric) that has an enabled threshold.
    Resources come from the `resources` table -- populated by the
    discovery cycle, no AWS calls made here.
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT DISTINCT
                r.id             AS resource_db_id,
                r.resource_id    AS aws_resource_id,
                r.resource_type,
                mc.metric_name,
                mc.service,
                mc.statistic
            FROM thresholds t
            JOIN metric_catalog mc
                ON mc.id = t.metric_id
            JOIN resources r
                ON r.resource_type  = t.resource_type
               AND r.aws_account_id = t.aws_account_id
            WHERE t.enabled = 1
        """)
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


def sync_metrics_from_vm() -> int:
    """
    Populates `metrics` from VM for every enabled threshold's resources.
    Returns the number of datapoints written. Zero AWS API calls.
    """
    rows = _fetch_enabled_threshold_targets()
    if not rows:
        logger.info("VM metrics sync: no enabled thresholds -- nothing to do")
        return 0

    # Group by (service, metric_name) so each distinct metric gets exactly
    # ONE VM call (vm_query_all fetches every resource's value at once)
    # instead of one VM call per resource.
    by_metric = {}
    for row in rows:
        key = (row["service"], row["metric_name"])
        by_metric.setdefault(key, []).append(row)

    datapoints      = []   # (resource_db_id, metric_name, value)
    skipped_no_stub = {}   # (service, metric_name) -> resource count
    matched         = 0

    for (service, metric_name), resource_rows in by_metric.items():
        stub      = _VM_METRIC_STUB.get((service, metric_name))
        dim_label = _VM_DIM_LABEL.get(service)

        if not stub or not dim_label:
            skipped_no_stub[(service, metric_name)] = len(resource_rows)
            continue

        stat        = resource_rows[0]["statistic"] or "Average"
        yace_metric = stub if stub in _VM_NO_SUFFIX else f"{stub}_{_STAT_SUFFIX.get(stat, 'average')}"

        values = vm_query_all(yace_metric, dim_label)

        for row in resource_rows:
            val = values.get(row["aws_resource_id"])
            if val is not None:
                # metric_name here is metric_catalog's CamelCase form
                # (e.g. "CPUUtilization"), matching what evaluate_alerts()
                # joins against.
                datapoints.append((row["resource_db_id"], metric_name, val))
                matched += 1

    write_metrics_batch(datapoints)

    if skipped_no_stub:
        total_skipped = sum(skipped_no_stub.values())
        detail = ", ".join(
            f"{svc}/{metric} x{n}"
            for (svc, metric), n in sorted(skipped_no_stub.items())
        )
        logger.info(
            f"VM metrics sync: {matched} written, {total_skipped} skipped "
            f"(no VM series yet) -- {detail}"
        )
    else:
        logger.info(f"VM metrics sync: {matched} written, 0 skipped")

    return matched
