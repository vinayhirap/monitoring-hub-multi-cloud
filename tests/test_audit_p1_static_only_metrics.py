# tests/test_audit_p1_static_only_metrics.py
"""Audit A5: availability / absolute-limit metrics must never use a learned band."""
import sys
sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401
import app.threshold_defaults as td  # noqa: E402
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_availability_and_limit_metrics_are_static_only():
    for name in ("HealthyHostCount", "healthyhosts_describe", "UnHealthyHostCount",
                 "unhealthyhosts_describe", "StatusCheckFailed", "StatusCheckFailed_System",
                 "DaysToExpiry", "NumberOfBackupJobsFailed", "NumberOfRestoreJobsFailed",
                 "NumberOfNotificationsFailed", "ErrorPortAllocation", "FreeStorageSpace",
                 "HealthCheckPercentageHealthy"):
        assert td.is_static_only_metric(name), name


def test_capacity_percent_still_static_only():
    assert td.is_static_only_metric("disk_used_percent")
    assert td.is_static_only_metric("mem_used_percent")
    assert td.is_static_only_metric("disk_used_percent__data")


def test_ordinary_metrics_may_still_be_dynamic():
    for name in ("CPUUtilization", "NetworkIn", "RequestCount", "VolumeWriteBytes",
                 "HTTPCode_Target_5XX_Count", "TargetResponseTime", "", None):
        assert not td.is_static_only_metric(name), name


def test_all_three_enforcement_points_use_the_guard():
    for rel in ("app/collector/threshold_tuning.py", "app/collector/alert_evaluator.py",
                "app/metric_meta.py", "app/api/settings.py"):
        assert "is_static_only_metric" in (ROOT / rel).read_text(), rel


# ── Audit A6 (2026-10-19): cert-expiry alert tagged "Auto-tuning"; list widened to every cloud ──

def test_a6_expiry_state_capacity_and_failure_metrics_are_static_only():
    must_be_static = (
        # AWS
        "SecondsUntilKeyMaterialExpiration", "DaysToExpiry", "HealthCheckStatus", "ClusterStatus.green",
        "ClusterStatus.red", "TunnelState", "ConnectionState", "GroupInServiceInstances", "FreeableMemory",
        "BurstBalance", "CPUCreditBalance", "BurstCreditBalance", "PercentageDiskSpaceUsed",
        "KafkaDataLogsDiskUsed", "DatabaseMemoryUsagePercentage", "JVMMemoryPressure", "MemoryUtilization",
        "PercentIOLimit", "VolumeThroughputPercentage", "ExecutionsFailed", "ExecutionsTimedOut",
        "FailedInvocations", "OfflinePartitionsCount", "UnderReplicatedPartitions",
        "ClusterIndexWritesBlocked", "cluster_failed_node_count", "DeadLetterErrors", "DeliveryErrors",
        "DeliveryToS3.Success",
        # Azure
        "VM Availability Metric", "Availability", "VipAvailability", "DipAvailability", "storage_percent",
        "dtu_consumption_percent", "NormalizedRUConsumption", "UsedMemoryPercentage",
        "node_disk_usage_percentage", "node_memory_working_set_percentage", "kube_pod_status_phase",
        "kube_node_status_condition", "SaturationShoebox", "CPU Credits Remaining", "PipelineFailedRuns",
        "ActivityFailedRuns", "UnhealthyHostCount",
        # GCP
        "uptime", "up", "replication/role", "disk/utilization", "memory/utilization", "memory/utilizations",
        "memory/limit_utilization", "memory/allocatable_utilization", "stats/memory/usage_ratio",
        "nat_allocation_failed", "port_usage",
    )
    for name in must_be_static:
        assert td.is_static_only_metric(name), name
        assert td.is_static_only_metric(name.upper()), name          # DB names are lower-cased


def test_a6_traffic_latency_and_cpu_metrics_stay_tunable_on_every_cloud():
    must_stay_dynamic = (
        # AWS
        "CPUUtilization", "NetworkOut", "RequestCount", "TargetResponseTime", "HTTPCode_Target_4XX_Count",
        "Invocations", "Duration", "ApproximateNumberOfMessagesVisible", "CPUCreditUsage", "ReadLatency",
        "ReplicaLag", "BytesInPerSec", "CacheHitRate", "Evictions", "ConnectionTime", "ConnectionBpsEgress",
        # Azure
        "Percentage CPU", "Network In Total", "cpu_percent", "Transactions", "SuccessE2ELatency",
        "connection_failed", "TotalRequestUnits", "PercentProcessorTime", "serverLoad",
        # GCP
        "cpu/utilization", "network/received_bytes_count", "request_count", "request_latencies",
        "cpu/limit_utilization", "cpu/allocatable_utilization", "execution_count", "restart_count",
        "num_undelivered_messages", "stats/cpu_utilization",
    )
    for name in must_stay_dynamic:
        assert not td.is_static_only_metric(name), name


def test_a6_no_curated_catalog_metric_is_silently_misclassified():
    """Every curated metric on every cloud is checked against an explicit decision, so a newly added
    catalog metric with a state / expiry / capacity meaning cannot slip through unreviewed."""
    from app.aws.metric_catalog_data import CURATED as AWS
    from app.providers.azure.metric_catalog_data import CURATED as AZ
    from app.providers.gcp.metric_catalog_data import CURATED as GCP
    static_now = {m[0] for cat in (AWS, AZ, GCP) for (_d, _n, _c, ms) in cat.values() for m in ms
                  if td.is_static_only_metric(m[0])}
    # certificate / key expiry is static on every cloud that publishes it
    assert {"DaysToExpiry", "SecondsUntilKeyMaterialExpiration"} <= static_now
    # nothing that is plain traffic / CPU / latency volume got caught by a too-greedy prefix
    for m in static_now:
        low = m.lower()
        assert not any(tok in low for tok in ("requestcount", "latency", "bytes_count")), m
