# app/threshold_defaults.py
"""
Shared default alert-threshold values, keyed by CloudWatch metric_name.

Single source of truth used by both:
  - app/api/settings.py        (POST /api/settings/thresholds/seed)
  - app/api/metric_catalog.py  (auto-sync when Metrics to Monitor selection changes)

so the two can never drift apart. Each value is a (warning, critical, comparison)
tuple. comparison is one of ">", "<", ">=" and is evaluated against the metric's
current value: ">" and ">=" mean "alert when value is at/above this number"
(the common case — utilization, error counts, latency); "<" means "alert when
value drops below this number" (balances, credits, free space, healthy-host
counts, and other "lower is worse" metrics).

Values here are sane, conservative starting points intended to be edited per
account/resource in Settings -> Metric Thresholds once real traffic levels are
known -- they intentionally err toward not over-firing on day one.
"""

# metric_name -> (warning_value, critical_value, comparison)
DEFAULT_THRESHOLDS = {
    '4XXError': (1, 5, '>'),
    '4xxErrorRate': (5, 15, '>'),
    '4xxErrors': (1, 5, '>'),
    '5XXError': (1, 5, '>'),
    '5xxErrorRate': (1, 5, '>'),
    '5xxErrors': (1, 5, '>'),
    'ActiveConnectionCount': (1000000, 5000000, '>'),
    'ActiveFlowCount': (1000000, 5000000, '>'),
    'AllRequests': (1000000, 5000000, '>'),
    'AllowedRequests': (1000000, 5000000, '>'),
    'ApproximateAgeOfOldestMessage': (300, 900, '>'),
    'ApproximateNumberOfMessagesNotVisible': (1000, 10000, '>'),
    'ApproximateNumberOfMessagesVisible': (1000, 10000, '>'),
    'BinLogDiskUsage': (1000000, 5000000, '>'),
    'BlockedRequests': (1, 5, '>'),
    'BucketSizeBytes': (1000000, 5000000, '>'),
    'BufferCacheHitRatio': (80, 50, '<'),
    'BurstBalance': (30, 10, '<'),
    'BurstCreditBalance': (0, 0, '<'),
    'BytesDownloaded': (1000000, 5000000, '>'),
    'BytesIn': (1000000, 5000000, '>'),
    'BytesInFromSource': (1000000, 5000000, '>'),
    'BytesInPerSec': (800000000, 950000000, '>'),
    'BytesOut': (1000000, 5000000, '>'),
    'BytesOutPerSec': (800000000, 950000000, '>'),
    'BytesOutToDestination': (1000000, 5000000, '>'),
    'BytesUploaded': (1000000, 5000000, '>'),
    'CDCLatencySource': (30, 60, '>'),
    'CDCLatencyTarget': (30, 60, '>'),
    'CPUCreditBalance': (50, 10, '<'),
    'CPUCreditUsage': (1000000, 5000000, '>'),
    'CPUUtilization': (70, 90, '>'),
    'CacheHitCount': (1000000, 5000000, '>'),
    'CacheHitRate': (80, 50, '<'),
    'CacheHits': (1000000, 5000000, '>'),
    'CacheMissCount': (1000000, 5000000, '>'),
    'CacheMisses': (1000000, 5000000, '>'),
    'ClientConnections': (1000000, 5000000, '>'),
    'ClusterIndexWritesBlocked': (1, 5, '>'),
    'ClusterStatus.green': (1, 1, '<'),
    'ClusterStatus.red': (1, 1, '>='),
    'ClusterStatus.yellow': (1, 1, '>='),
    'ColdStart': (1000000, 5000000, '>'),
    'ConcurrentExecutions': (1000000, 5000000, '>'),
    'ConditionalCheckFailedRequests': (1, 5, '>'),
    'ConnectionBpsEgress': (800000000, 950000000, '>'),
    'ConnectionBpsIngress': (800000000, 950000000, '>'),
    'ConnectionErrorCount': (1, 5, '>'),
    'ConnectionState': (1, 1, '<'),
    'ConnectionTime': (1000, 3000, '>'),
    'ConsumedReadCapacityUnits': (1000000, 5000000, '>'),
    'ConsumedWriteCapacityUnits': (1000000, 5000000, '>'),
    'Count': (1000000, 5000000, '>'),
    'CountedRequests': (1000000, 5000000, '>'),
    'CpuUser': (70, 90, '>'),
    'CurrConnections': (1000000, 5000000, '>'),
    'DataReadIOBytes': (1000000, 5000000, '>'),
    'DataWriteIOBytes': (1000000, 5000000, '>'),
    'DatabaseConnections': (1000000, 5000000, '>'),
    'DatabaseConnectionsBorrowLatency': (5, 15, '>'),
    'DatabaseMemoryUsagePercentage': (70, 90, '>'),
    'DaysToExpiry': (30, 7, '<'),
    'DeadLetterErrors': (1, 5, '>'),
    'DeliveryErrors': (1, 5, '>'),
    'DeliveryToS3.DataFreshness': (5, 15, '>'),
    'DeliveryToS3.Success': (99, 90, '<'),
    'DiskQueueDepth': (10, 30, '>'),
    'DiskReadBytes': (1000000, 5000000, '>'),
    'DiskWriteBytes': (1000000, 5000000, '>'),
    'Duration': (1000, 3000, '>'),
    'EBSReadBytes': (1000000, 5000000, '>'),
    'EBSReadOps': (1000000, 5000000, '>'),
    'EBSWriteBytes': (1000000, 5000000, '>'),
    'EBSWriteOps': (1000000, 5000000, '>'),
    'ErrorPortAllocation': (1, 5, '>'),
    'Errors': (1, 5, '>'),
    'Evictions': (1000000, 5000000, '>'),
    'ExecutionThrottled': (1, 5, '>'),
    'ExecutionTime': (1000, 3000, '>'),
    'ExecutionsFailed': (1, 5, '>'),
    'ExecutionsSucceeded': (1000000, 5000000, '>'),
    'ExecutionsTimedOut': (1000000, 5000000, '>'),
    'FailedInvocations': (1, 5, '>'),
    'FaultRequestCount': (1000000, 5000000, '>'),
    'FirstByteLatency': (1000, 3000, '>'),
    'FreeStorageSpace': (2147483648, 536870912, '<'),
    'FreeableMemory': (536870912, 209715200, '<'),
    'GetRecords.IteratorAgeMilliseconds': (1000, 3000, '>'),
    'GremlinRequestsPerSec': (800000000, 950000000, '>'),
    'GroupDesiredCapacity': (1000000, 5000000, '>'),
    'GroupInServiceInstances': (1, 0, '<'),
    'GroupMaxSize': (1000000, 5000000, '>'),
    'GroupMinSize': (1000000, 5000000, '>'),
    'GroupPendingInstances': (1000000, 5000000, '>'),
    'GroupTerminatingInstances': (1000000, 5000000, '>'),
    'HTTPCode_ELB_5XX_Count': (1, 5, '>'),
    'HTTPCode_Target_2XX_Count': (1000000, 5000000, '>'),
    'HTTPCode_Target_4XX_Count': (1, 5, '>'),
    'HTTPCode_Target_5XX_Count': (1, 5, '>'),
    'HealthCheckPercentageHealthy': (90, 50, '<'),
    'HealthCheckStatus': (1, 1, '<'),
    'HealthyHostCount': (1, 0, '<'),
    'IncomingBytes': (1000000, 5000000, '>'),
    'IncomingLogEvents': (1000000, 5000000, '>'),
    'IncomingRecords': (1000000, 5000000, '>'),
    'IndexingLatency': (1000, 3000, '>'),
    'IntegrationLatency': (1000, 3000, '>'),
    'Invocations': (1000000, 5000000, '>'),
    'ItemCacheHits': (1000000, 5000000, '>'),
    'ItemCacheMisses': (1000000, 5000000, '>'),
    'IteratorAge': (1000, 3000, '>'),
    'JVMMemoryPressure': (75, 90, '>'),
    'KafkaDataLogsDiskUsed': (75, 90, '>'),
    'Latency': (1000, 3000, '>'),
    'MatchedEvents': (1000000, 5000000, '>'),
    'MemoryUtilization': (70, 90, '>'),
    # EC2 CWAgent's own literal metric name (snake_case, distinct from the
    # PascalCase 'MemoryUtilization' key above used by ECS/other services)
    # -- see apply_add_cwagent_mem_threshold.py.
    'mem_used_percent': (80, 90, '>'),
    # Matches the EC2 chart's own existing threshold indicator for this
    # metric (35.5% shown as healthy in an earlier screenshot) --
    # disk filling up is generally a slower-moving, later-warning signal
    # than memory, so a slightly higher bar is reasonable. See
    # apply_add_cwagent_disk_threshold.py.
    'disk_used_percent': (80, 90, '>'),
    'MetadataNoToken': (1, 5, '>'),
    'NetworkBytesIn': (1000000, 5000000, '>'),
    'NetworkBytesOut': (1000000, 5000000, '>'),
    'NetworkIn': (1000000, 5000000, '>'),
    'NetworkOut': (1000000, 5000000, '>'),
    'NewConnectionCount': (1000000, 5000000, '>'),
    'NewFlowCount': (1000000, 5000000, '>'),
    'Nodes': (1000000, 5000000, '>'),
    'NumberOfBackupJobsCompleted': (1000000, 5000000, '>'),
    'NumberOfBackupJobsFailed': (1, 5, '>'),
    'NumberOfEmptyReceives': (1000000, 5000000, '>'),
    'NumberOfMessagesDeleted': (1000000, 5000000, '>'),
    'NumberOfMessagesPublished': (1000000, 5000000, '>'),
    'NumberOfMessagesReceived': (1000000, 5000000, '>'),
    'NumberOfMessagesSent': (1000000, 5000000, '>'),
    'NumberOfNotificationsDelivered': (1000000, 5000000, '>'),
    'NumberOfNotificationsFailed': (1, 5, '>'),
    'NumberOfObjects': (1000000, 5000000, '>'),
    'NumberOfRestoreJobsFailed': (1, 5, '>'),
    'OfflinePartitionsCount': (1, 5, '>'),
    'OriginLatency': (1000, 3000, '>'),
    'PacketDropCountBlackhole': (1, 5, '>'),
    'PacketDropCountNoRoute': (1, 5, '>'),
    'PacketsDropCount': (1, 5, '>'),
    'PassedRequests': (1000000, 5000000, '>'),
    'PendingTaskCount': (1000000, 5000000, '>'),
    'PercentIOLimit': (80, 95, '>'),
    'PercentageDiskSpaceUsed': (75, 90, '>'),
    'ProcessedBytes': (1000000, 5000000, '>'),
    'ProcessedBytesIn': (1000000, 5000000, '>'),
    'ProcessedBytesOut': (1000000, 5000000, '>'),
    'PublishSize': (1000000, 5000000, '>'),
    'QueryDuration': (100000, 500000, '>'),
    'ReadIOPS': (800000000, 950000000, '>'),
    'ReadLatency': (5, 15, '>'),
    'ReadProvisionedThroughputExceeded': (1000000, 5000000, '>'),
    'ReadThrottleEvents': (1, 5, '>'),
    'RejectedConnectionCount': (1, 5, '>'),
    'ReplicaLag': (30, 60, '>'),
    'ReplicationLag': (30, 60, '>'),
    'RequestCount': (1000000, 5000000, '>'),
    'Requests': (1000000, 5000000, '>'),
    'RunningTaskCount': (1000000, 5000000, '>'),
    'SearchLatency': (1000, 3000, '>'),
    'SecondsUntilKeyMaterialExpiration': (5, 15, '>'),
    'SignInSuccesses': (1000000, 5000000, '>'),
    'SignUpSuccesses': (1000000, 5000000, '>'),
    'SparqlRequestsPerSec': (800000000, 950000000, '>'),
    'StatusCheckFailed': (1, 1, '>='),
    'StatusCheckFailed_Instance': (1, 1, '>='),
    'StatusCheckFailed_System': (1, 1, '>='),
    'StorageBytes': (1000000, 5000000, '>'),
    'SuccessfulRequestLatency': (1000, 3000, '>'),
    'SwapUsage': (1000000, 5000000, '>'),
    'SystemErrors': (1, 5, '>'),
    'TCP_Client_Reset_Count': (1000000, 5000000, '>'),
    'TargetConnectionErrorCount': (1, 5, '>'),
    'TargetResponseTime': (5, 15, '>'),
    'ThrottledEvents': (1, 5, '>'),
    'ThrottledRequests': (1, 5, '>'),
    'ThrottledRules': (1, 5, '>'),
    'Throttles': (1, 5, '>'),
    'TotalErrorRate': (1, 5, '>'),
    'TotalRequestLatency': (1000, 3000, '>'),
    'TransactionLogsDiskUsage': (1000000, 5000000, '>'),
    'TunnelDataIn': (1000000, 5000000, '>'),
    'TunnelDataOut': (1000000, 5000000, '>'),
    'TunnelState': (1, 1, '<'),
    'UnHealthyHostCount': (1, 3, '>'),
    'UnderReplicatedPartitions': (1, 5, '>'),
    'UserErrors': (1, 5, '>'),
    'VolumeBytesUsed': (1000000, 5000000, '>'),
    'VolumeConsumedReadWriteOps': (1000000, 5000000, '>'),
    'VolumeIdleTime': (5, 15, '>'),
    'VolumeQueueLength': (10, 30, '>'),
    'VolumeReadBytes': (1000000, 5000000, '>'),
    'VolumeReadOps': (1000000, 5000000, '>'),
    'VolumeThroughputPercentage': (80, 95, '>'),
    'VolumeTotalReadTime': (5, 15, '>'),
    'VolumeTotalWriteTime': (5, 15, '>'),
    'VolumeWriteBytes': (1000000, 5000000, '>'),
    'VolumeWriteOps': (1000000, 5000000, '>'),
    'WLMQueueLength': (10, 30, '>'),
    'WriteIOPS': (800000000, 950000000, '>'),
    'WriteLatency': (5, 15, '>'),
    'WriteProvisionedThroughputExceeded': (1000000, 5000000, '>'),
    'WriteThrottleEvents': (1, 5, '>'),
    'cluster_failed_node_count': (1, 3, '>'),
    'cluster_node_count': (1000000, 5000000, '>'),
}

# Used when a metric_name has no explicit entry above (e.g. a "directory"
# metric discovered live via ListMetrics that isn't in the curated catalog).
FALLBACK_THRESHOLD = (1000000, 5000000, ">")

# metric_catalog.service ("alb", "nlb") is the correct catalog/display value
# for those two ALB/NLB metric_catalog entries -- this map is NOT about
# changing that. It exists because app/collector/discovery/runner.py stores
# ALL Elastic Load Balancing v2 resources (both ALB and NLB; this codebase
# doesn't distinguish them at discovery time) under resources.resource_type
# = "elb" uniformly, while thresholds.resource_type needs to match THAT
# value for app/collector/alert_evaluator.py's scheduled JOIN
# (`t.resource_type = r.resource_type`) to ever succeed -- it has no
# service-name fallback, unlike check_and_write_alerts()'s own separate
# LOCAL_RESOURCE_TYPE map for the same translation.
#
# Lives HERE, not duplicated in settings.py/metric_catalog.py separately,
# for the exact reason this module's own docstring already states above:
# so the various places that write thresholds.resource_type can never
# drift apart. Originally fixed only in app/api/settings.py
# (apply_fix_alb_nlb_threshold_resource_type.py) -- that missed
# app/api/metric_catalog.py's OWN separate INSERT INTO thresholds in
# _sync_thresholds_for_selection(), which onboarding auto-detect, "Apply
# Default Template", and every Settings -> Metrics to Monitor checkbox
# change all funnel through. Moved here and both call sites updated to
# use it, closing that gap for good. See
# apply_fix_threshold_resource_type_everywhere.py.
THRESHOLD_RESOURCE_TYPE_ALIASES = {"alb": "elb", "nlb": "elb"}


def normalize_threshold_resource_type(value):
    return THRESHOLD_RESOURCE_TYPE_ALIASES.get(value, value)


# Confirmed by reading app/providers/azure/metrics_collector.py and
# app/providers/gcp/metrics_collector.py directly: both write
# metric_catalog.metric_name into `metrics`/`metric_history` completely
# UNCHANGED (Azure: metric.name, the SDK's own echo of the exact
# requested catalog name; GCP: row["metric_name"], the catalog row
# itself) -- no transform, no abbreviation, for either cloud. A simple
# .lower() comparison on both sides always correctly matches for Azure
# and GCP, and for MOST AWS metrics too (app/collector/metrics/runner.py
# happens to use "cpuutilization" for "CPUUtilization", etc.).
#
# But AWS's db_metric_name convention is a genuinely separate,
# hand-picked abbreviation in several cases -- confirmed by comparing
# every entry in runner.py's EC2_METRICS_*/EBS_METRICS/RDS_METRICS/
# ELB_METRICS/LAMBDA_METRICS_* tuples against metric_catalog's official
# name, catalog_name.lower() != db_metric_name for these specific ones:
#   RDS DatabaseConnections -> dbconnections (not "databaseconnections")
#   RDS FreeStorageSpace    -> freestorage   (not "freestoragespace")
#   ELB HTTPCode_Target_5XX_Count -> errors5xx (not the CW name, lowered)
#   ELB TargetResponseTime  -> responselatency (not "targetresponsetime")
#   ELB HealthyHostCount    -> healthyhosts_describe (different SOURCE
#                              entirely -- see apply_fix_alb_healthy_hosts.py;
#                              CloudWatch-based collection for this metric
#                              never worked at all, describe_polling.py's
#                              free DescribeTargetHealth path is the only
#                              real source)
#   ELB UnHealthyHostCount  -> unhealthyhosts_describe (same as above)
# A blind catalog_name.lower() guess is WRONG for exactly these 6 --
# without this override, has_data-style checks would incorrectly treat
# metrics that genuinely have real, actively-collected data as if they
# never produced anything. This map is checked FIRST; anything not
# listed here (which covers the rest of AWS plus all of Azure/GCP)
# correctly falls back to the plain .lower() comparison.
AWS_METRIC_NAME_TO_DB_NAME = {
    ("rds", "DatabaseConnections"): "dbconnections",
    ("rds", "FreeStorageSpace"): "freestorage",
    ("elb", "HTTPCode_Target_5XX_Count"): "errors5xx",
    ("elb", "TargetResponseTime"): "responselatency",
    ("elb", "HealthyHostCount"): "healthyhosts_describe",
    ("elb", "UnHealthyHostCount"): "unhealthyhosts_describe",
}


def resolve_db_metric_name(resource_type, catalog_metric_name):
    """
    The single source of truth for "given a metric_catalog metric_name
    and its resource_type, what string actually appears in
    metrics.metric_name / metric_history.metric_name?" -- checks the
    explicit AWS override table first (for the handful of AWS metrics
    where the internal abbreviation genuinely diverges from a case-fold
    of the official name), falling back to a plain lowercase compare
    for everything else (correct for Azure, GCP, and most of AWS, which
    all write their metric_name consistent with a simple case-fold).
    """
    override = AWS_METRIC_NAME_TO_DB_NAME.get((resource_type, catalog_metric_name))
    if override is not None:
        return override
    return (catalog_metric_name or "").lower()
