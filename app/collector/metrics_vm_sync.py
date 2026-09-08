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
    One row per (resource, metric) that has an enabled threshold, for
    EVERY provider -- this query itself was never AWS-specific, it just
    had no working sync path for anything but AWS until this fix. Now
    also selects mc.provider so sync_metrics_from_vm() can route each
    row through the right VM query convention.

    Resources come from the `resources` table -- populated by the
    discovery cycle, no cloud API calls made here.
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
                mc.statistic,
                mc.provider
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


def _slug(name: str) -> str:
    """'Percentage CPU' -> 'percentage_cpu'. Matches EXACTLY the slug
    logic in app/providers/{azure,gcp}/metrics_collector.py's _slug() --
    must stay mirrored, since this has to reconstruct the same VM metric
    name those collectors already pushed under."""
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return s or "value"


def _sync_aws_metrics(rows) -> tuple[list, dict, int]:
    """
    Unchanged AWS sync logic, extracted as-is from the pre-fix
    sync_metrics_from_vm() so this fix makes zero behavior changes to the
    AWS path. Returns (datapoints, skipped_no_stub, matched).
    """
    by_metric = {}
    for row in rows:
        key = (row["service"], row["metric_name"])
        by_metric.setdefault(key, []).append(row)

    datapoints = []
    skipped_no_stub = {}
    matched = 0

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
                datapoints.append((row["resource_db_id"], metric_name, val))
                matched += 1

    return datapoints, skipped_no_stub, matched


def _sync_azure_gcp_metrics(rows) -> tuple[list, dict, int]:
    """
    Azure/GCP sync -- the actual fix. These collectors (see
    app/providers/{azure,gcp}/metrics_collector.py) push a plain
    `resource_id` label per datapoint, not AWS/YACE's per-service
    dimension_XxxId scheme, and the VM metric name is mechanically
    derivable (f"{provider}_{service}_{slug(metric_name)}") rather than
    needing a hand-curated stub table like AWS/YACE requires -- so this
    needs no equivalent of _VM_METRIC_STUB at all.

    Returns (datapoints, skipped_no_series, matched) -- same shape as
    _sync_aws_metrics so the caller can combine both uniformly.
    """
    by_metric = {}
    for row in rows:
        key = (row["provider"], row["service"], row["metric_name"])
        by_metric.setdefault(key, []).append(row)

    datapoints = []
    skipped_no_series = {}
    matched = 0

    for (provider, service, metric_name), resource_rows in by_metric.items():
        vm_metric = f"{provider}_{service}_{_slug(metric_name)}"
        values = vm_query_all(vm_metric, "resource_id")

        if not values:
            skipped_no_series[(provider, service, metric_name)] = len(resource_rows)
            continue

        for row in resource_rows:
            val = values.get(row["aws_resource_id"])
            if val is not None:
                # metric_name here is metric_catalog's exact stored name
                # (e.g. "Percentage CPU"), matching what evaluate_alerts()
                # joins against -- NOT the slugged VM series name above,
                # which is only used to know which series to query.
                datapoints.append((row["resource_db_id"], metric_name, val))
                matched += 1
            else:
                skipped_no_series.setdefault((provider, service, metric_name), 0)

    return datapoints, skipped_no_series, matched


def sync_metrics_from_vm() -> int:
    """
    Historically populated `metrics` from VM for whichever providers
    hadn't yet moved to direct-fetch. After Phase 1 (AWS), Phase 2
    (Azure), and Phase 3 (GCP, see apply_gcp_direct_metrics_fetch.py),
    ALL THREE providers write their own last-value cache directly --
    this function's entire job is now permanent dead weight until
    Phase 4 removes the call to it from scheduler.py entirely and
    retires VM. Short-circuiting here (log once, return immediately)
    instead of running a real DB query every standard-tier cycle for
    zero rows, forever, until then. _fetch_enabled_threshold_targets(),
    _sync_aws_metrics(), and _sync_azure_gcp_metrics() are left in place
    below, unreachable but harmless, for Phase 4 to clean up alongside
    the rest of the VM code.
    """
    logger.info(
        "VM metrics sync: no-op -- AWS (Phase 1), Azure (Phase 2), and GCP "
        "(Phase 3) are all handled directly now. Safe to remove this call "
        "from scheduler.py once Phase 4 confirms nothing else needs it."
    )
    return 0
