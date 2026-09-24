# app/aws/cloudwatch.py

from app.clients.vm_client import vm_query

# Maps (namespace, metric_name) -> (yace_metric_stub, dimension_label)
# yace_metric_stub gets combined with the statistic to build the full
# metric name, e.g. aws_ec2_cpuutilization + _average
_YACE_METRIC_MAP = {
    ("AWS/EC2", "CPUUtilization"): ("aws_ec2_cpuutilization", "dimension_InstanceId"),
    ("AWS/EC2", "NetworkIn"):      ("aws_ec2_network_in",      "dimension_InstanceId"),
    ("AWS/EC2", "NetworkOut"):     ("aws_ec2_network_out",     "dimension_InstanceId"),
    ("AWS/EBS", "VolumeReadOps"):    ("aws_ebs_volume_read_ops",    "dimension_VolumeId"),
    ("AWS/EBS", "VolumeWriteOps"):   ("aws_ebs_volume_write_ops",   "dimension_VolumeId"),
    ("AWS/EBS", "VolumeReadBytes"):  ("aws_ebs_volume_read_bytes",  "dimension_VolumeId"),
    ("AWS/EBS", "VolumeWriteBytes"): ("aws_ebs_volume_write_bytes", "dimension_VolumeId"),
    ("AWS/EBS", "VolumeQueueLength"):("aws_ebs_volume_queue_length","dimension_VolumeId"),
    ("AWS/RDS", "CPUUtilization"):     ("aws_rds_cpuutilization",     "dimension_DBInstanceIdentifier"),
    ("AWS/RDS", "FreeStorageSpace"):   ("aws_rds_free_storage_space", "dimension_DBInstanceIdentifier"),
    ("AWS/RDS", "DatabaseConnections"):("aws_rds_database_connections","dimension_DBInstanceIdentifier"),
}

_STAT_SUFFIX = {"Average": "average", "Sum": "sum", "Maximum": "maximum"}


def fetch_metric(
    namespace: str,
    metric_name: str,
    dimensions: list,
    statistic: str = "Average",
    period: int = 300,
    minutes: int = 5,
    region: str = "ap-south-1",   # matches YACE config region — confirm this matches your app's actual region
    start_time=None,
    end_time=None,
):
    """
    Generic metric fetcher — now backed by VictoriaMetrics/YACE for EC2/EBS/RDS.
    Falls back to None (caller already handles None) for anything not yet
    covered by the YACE config (Lambda, ECS, S3, ALB, NAT).
    """
    key = (namespace, metric_name)
    if key not in _YACE_METRIC_MAP:
        # Not scraped by YACE yet — caller should be using boto3 directly for these
        return None

    stub, dim_label = _YACE_METRIC_MAP[key]
    suffix = _STAT_SUFFIX.get(statistic, "average")
    yace_metric = f"{stub}_{suffix}"

    if not dimensions:
        return None
    dim_value = dimensions[0]["Value"]

    # Bug fix: a bare instant query drops any series whose last sample is
    # older than VictoriaMetrics' default instant-query staleness window
    # (~5 min) -- see system primer bug class #5. That's shorter than
    # this app's own "extended" (60 min) and "slow_extended" (24 h)
    # collection tiers, so metrics on those tiers were looking
    # permanently "missing" through this call even though YACE had
    # genuinely scraped them. last_over_time(...[26h]) returns the most
    # recent sample within a 26h window (covers slow_extended plus
    # buffer) instead of requiring one inside the last ~5 min.
    promql = f'last_over_time({yace_metric}{{{dim_label}="{dim_value}"}}[26h])'
    return vm_query(promql)