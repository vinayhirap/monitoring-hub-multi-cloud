# app/collector/polling_model.py
"""
THE polling model -- one place that says, for every metric this app
collects on a schedule, which tier it is polled on, how far back each poll
looks, and (derived from that) how the alert evaluator and the Alerts page
should treat its freshness. Pure data + pure functions, no imports beyond
the equally-pure catalog/tier modules, so app/alert_rules.py (which must
stay dependency-free) can import it.

Tiers (metric-polling audit, 2026-09-23):

  critical       2 min   availability / error / saturation, 1-min source.
                         Alerts for these are ALSO evaluated on every
                         2-min tick (scheduler.py -> evaluate_alerts(p1_only)),
                         otherwise a 2-min poll would buy nothing over 5 min.
  standard       5 min   alertable signals, or the source itself is 5-min
                         (EC2 basic monitoring)
  low           15 min   trend / capacity
  extended      60 min   informational extended-service metrics
  slow_extended 24 h     daily publishers (S3 storage, cert/key expiry, ...)

Look-back windows are sized to poll interval + provider publication delay,
not to the poll interval alone -- CloudWatch 5-min metrics become visible
5-10 min after their period starts, 1-min metrics ~2 min; Azure platform
metrics ~4-5 min; Cloud Monitoring up to ~4 min. A window shorter than
that silently skips points that were not yet visible on the previous poll.
"""

from collections import namedtuple

TIER_SECONDS = {
    "critical":      120,
    "standard":      300,
    "low":           900,
    "extended":      3600,
    "slow_extended": 86400,
}

# ── AWS core (runner.py) ────────────────────────────────────────────────
# gate: None | "alb" | "nlb" | "tclass" | "replica" -- resource filter
# applied by runner.py before a definition is queried for a resource.
CoreMetric = namedtuple(
    "CoreMetric",
    "resource_type cw_name db_name stat namespace tier lookback_min gate",
)

AWS_CORE_METRICS = [
    # EC2 -- basic monitoring publishes 5-min points, visible 5-10 min late.
    CoreMetric("ec2", "CPUUtilization",   "cpuutilization",   "Average", "AWS/EC2", "standard", 15, None),
    CoreMetric("ec2", "NetworkIn",        "networkin",        "Average", "AWS/EC2", "standard", 15, None),
    CoreMetric("ec2", "NetworkOut",       "networkout",       "Average", "AWS/EC2", "standard", 15, None),
    CoreMetric("ec2", "CPUCreditBalance", "cpucreditbalance", "Average", "AWS/EC2", "low",      25, "tclass"),
    # EBS -- ops/bytes as Sum (AWS-documented statistic; IOPS = Sum / 60).
    CoreMetric("ebs", "VolumeQueueLength", "volumequeuelength", "Average", "AWS/EBS", "standard", 15, None),
    CoreMetric("ebs", "VolumeReadOps",     "volumereadops",     "Sum",     "AWS/EBS", "low",      25, None),
    CoreMetric("ebs", "VolumeWriteOps",    "volumewriteops",    "Sum",     "AWS/EBS", "low",      25, None),
    CoreMetric("ebs", "VolumeReadBytes",   "volumereadbytes",   "Sum",     "AWS/EBS", "low",      25, None),
    CoreMetric("ebs", "VolumeWriteBytes",  "volumewritebytes",  "Sum",     "AWS/EBS", "low",      25, None),
    # RDS -- 1-min source.
    CoreMetric("rds", "CPUUtilization",      "cpuutilization", "Average", "AWS/RDS", "critical", 6,  None),
    CoreMetric("rds", "DatabaseConnections", "dbconnections",  "Average", "AWS/RDS", "critical", 6,  None),
    CoreMetric("rds", "FreeableMemory",      "freeablememory", "Average", "AWS/RDS", "critical", 6,  None),
    CoreMetric("rds", "ReadLatency",         "readlatency",    "Average", "AWS/RDS", "standard", 8,  None),
    CoreMetric("rds", "WriteLatency",        "writelatency",   "Average", "AWS/RDS", "standard", 8,  None),
    CoreMetric("rds", "DiskQueueDepth",      "diskqueuedepth", "Average", "AWS/RDS", "standard", 8,  None),
    CoreMetric("rds", "ReplicaLag",          "replicalag",     "Average", "AWS/RDS", "standard", 8,  "replica"),
    CoreMetric("rds", "ReadIOPS",            "readiops",       "Average", "AWS/RDS", "low",      18, None),
    CoreMetric("rds", "WriteIOPS",           "writeiops",      "Average", "AWS/RDS", "low",      18, None),
    CoreMetric("rds", "FreeStorageSpace",    "freestorage",    "Average", "AWS/RDS", "low",      18, None),
    CoreMetric("rds", "SwapUsage",           "swapusage",      "Average", "AWS/RDS", "low",      18, None),
    # ALB -- 1-min source, published only while traffic flows.
    CoreMetric("elb", "RequestCount",               "requestcount",               "Sum",     "AWS/ApplicationELB", "critical", 6,  "alb"),
    CoreMetric("elb", "HTTPCode_Target_5XX_Count",  "errors5xx",                  "Sum",     "AWS/ApplicationELB", "critical", 6,  "alb"),
    CoreMetric("elb", "HTTPCode_ELB_5XX_Count",     "httpcode_elb_5xx_count",     "Sum",     "AWS/ApplicationELB", "critical", 6,  "alb"),
    CoreMetric("elb", "TargetResponseTime",         "responselatency",            "Average", "AWS/ApplicationELB", "standard", 8,  "alb"),
    CoreMetric("elb", "TargetConnectionErrorCount", "targetconnectionerrorcount", "Sum",     "AWS/ApplicationELB", "standard", 8,  "alb"),
    CoreMetric("elb", "RejectedConnectionCount",    "rejectedconnectioncount",    "Sum",     "AWS/ApplicationELB", "low",      18, "alb"),
    CoreMetric("elb", "HTTPCode_Target_4XX_Count",  "httpcode_target_4xx_count",  "Sum",     "AWS/ApplicationELB", "low",      18, "alb"),
    CoreMetric("elb", "ActiveConnectionCount",      "activeconnectioncount",      "Sum",     "AWS/ApplicationELB", "low",      18, "alb"),
    CoreMetric("elb", "NewConnectionCount",         "newconnectioncount",         "Sum",     "AWS/ApplicationELB", "low",      18, "alb"),
    # NLB -- its own namespace (previously queried as an ALB: always empty).
    CoreMetric("elb", "ActiveFlowCount",        "activeflowcount",        "Average", "AWS/NetworkELB", "standard", 8, "nlb"),
    CoreMetric("elb", "NewFlowCount",           "newflowcount",           "Sum",     "AWS/NetworkELB", "standard", 8, "nlb"),
    CoreMetric("elb", "ProcessedBytes",         "processedbytes",         "Sum",     "AWS/NetworkELB", "standard", 8, "nlb"),
    CoreMetric("elb", "TCP_Client_Reset_Count", "tcp_client_reset_count", "Sum",     "AWS/NetworkELB", "standard", 8, "nlb"),
    # Lambda -- 1-min source.
    CoreMetric("lambda", "Errors",               "errors",               "Sum",     "AWS/Lambda", "standard", 8,  None),
    CoreMetric("lambda", "Throttles",            "throttles",            "Sum",     "AWS/Lambda", "standard", 8,  None),
    CoreMetric("lambda", "Duration",             "duration",             "Average", "AWS/Lambda", "standard", 8,  None),
    CoreMetric("lambda", "ConcurrentExecutions", "concurrentexecutions", "Maximum", "AWS/Lambda", "standard", 8,  None),
    CoreMetric("lambda", "Invocations",          "invocations",          "Sum",     "AWS/Lambda", "low",      18, None),
    CoreMetric("lambda", "IteratorAge",          "iteratorage",          "Maximum", "AWS/Lambda", "low",      18, None),
]

# CWAgent (EC2 guest metrics, agent default publish = 60 s).
CWAGENT_MEM_TIER, CWAGENT_MEM_LOOKBACK = "standard", 10
CWAGENT_DISK_TIER, CWAGENT_DISK_LOOKBACK = "low", 20
# Describe-API derived (free) -- polled by describe_polling.py every 60 s.
DESCRIBE_POLL_SECONDS = 60
AWS_DESCRIBE_METRICS = {
    ("ec2", "statuscheckfailed"),
    ("elb", "healthyhosts_describe"),
    ("elb", "unhealthyhosts_describe"),
}

# ── AWS extended (extended.py) ──────────────────────────────────────────
# Per-(service, CloudWatch metric) tier. Anything not listed polls on the
# service default: "slow_extended" for the daily services in
# extended.SLOW_EXTENDED_SERVICES, otherwise "extended" (60 min).
AWS_EXTENDED_TIER_OVERRIDES = {
    # 5 min -- incident signals
    ("sqs", "ApproximateAgeOfOldestMessage"):      "standard",
    ("sqs", "ApproximateNumberOfMessagesVisible"): "standard",
    ("opensearch", "ClusterStatus.red"):           "standard",
    ("opensearch", "ClusterStatus.yellow"):        "standard",
    ("opensearch", "ClusterIndexWritesBlocked"):   "standard",
    ("opensearch", "JVMMemoryPressure"):           "standard",
    ("elasticache", "CPUUtilization"):             "standard",
    ("elasticache", "DatabaseMemoryUsagePercentage"): "standard",
    ("elasticache", "Evictions"):                  "standard",
    ("memorydb", "DatabaseMemoryUsagePercentage"): "standard",
    ("memorydb", "Evictions"):                     "standard",
    ("route53", "HealthCheckStatus"):              "standard",
    ("ecs", "CPUUtilization"):                     "standard",
    ("ecs", "MemoryUtilization"):                  "standard",
    ("ecs", "RunningTaskCount"):                   "standard",
    ("ecs", "PendingTaskCount"):                   "standard",
    ("eks", "cluster_failed_node_count"):          "standard",
    ("dynamodb", "ThrottledRequests"):             "standard",
    ("dynamodb", "ReadThrottleEvents"):            "standard",
    ("dynamodb", "WriteThrottleEvents"):           "standard",
    ("dynamodb", "SystemErrors"):                  "standard",
    ("msk", "OfflinePartitionsCount"):             "standard",
    ("msk", "UnderReplicatedPartitions"):          "standard",
    ("apigateway", "5XXError"):                    "standard",
    # 15 min -- failures / state / saturation trend
    ("natgateway", "ErrorPortAllocation"):         "low",
    ("natgateway", "PacketsDropCount"):            "low",
    ("vpn", "TunnelState"):                        "low",
    ("directconnect", "ConnectionState"):          "low",
    ("directconnect", "ConnectionErrorCount"):     "low",
    ("states", "ExecutionsFailed"):                "low",
    ("states", "ExecutionsTimedOut"):              "low",
    ("events", "FailedInvocations"):               "low",
    ("kinesis", "ReadProvisionedThroughputExceeded"):  "low",
    ("kinesis", "WriteProvisionedThroughputExceeded"): "low",
    ("kinesis", "GetRecords.IteratorAgeMilliseconds"): "low",
    ("firehose", "DeliveryToS3.DataFreshness"):    "low",
    ("efs", "PercentIOLimit"):                     "low",
    ("efs", "BurstCreditBalance"):                 "low",
    ("msk", "CpuUser"):                            "low",
    ("msk", "KafkaDataLogsDiskUsed"):              "low",
    ("redshift", "PercentageDiskSpaceUsed"):       "low",
    ("s3", "AllRequests"):                         "low",
    ("s3", "4xxErrors"):                           "low",
    ("s3", "5xxErrors"):                           "low",
    ("s3", "FirstByteLatency"):                    "low",
    ("s3", "TotalRequestLatency"):                 "low",
    # daily publishers that were on the hourly tier
    ("certificatemanager", "DaysToExpiry"):        "slow_extended",
    ("kms", "SecondsUntilKeyMaterialExpiration"):  "slow_extended",
}

# Extended-tier GetMetricData look-back per tier (Period=300 buckets).
AWS_EXTENDED_LOOKBACK_MIN = {
    "standard":      15,
    "low":           25,
    "extended":      70,
    "slow_extended": 2900,   # 48 h S3 delivery delay + buffer
}

AWS_EXTENDED_NAMESPACE_OVERRIDES = {
    ("ecs", "RunningTaskCount"):          "ECS/ContainerInsights",
    ("ecs", "PendingTaskCount"):          "ECS/ContainerInsights",
    ("eks", "cluster_failed_node_count"): "ContainerInsights",
    ("eks", "cluster_node_count"):        "ContainerInsights",
}

AWS_EXTENDED_REGION_OVERRIDES = {
    "cloudfront":        "us-east-1",
    "route53":           "us-east-1",
    "globalaccelerator": "us-west-2",
}

# Metrics AWS only publishes with an extra dimension the resource row
# can't supply (per DynamoDB Operation, per MSK broker). Collected as one
# series per resource with a SEARCH expression reduced by metric math.
# value: (reducer, schema dimension list, key dimension, SEARCH statistic)
AWS_SEARCH_METRICS = {
    ("dynamodb", "ThrottledRequests"):        ("SUM", ["Operation", "TableName"], "TableName", "Sum"),
    ("dynamodb", "SystemErrors"):             ("SUM", ["Operation", "TableName"], "TableName", "Sum"),
    ("dynamodb", "SuccessfulRequestLatency"): ("MAX", ["Operation", "TableName"], "TableName", "Average"),
    ("msk", "CpuUser"):                       ("MAX", ["Broker ID", "Cluster Name"], "Cluster Name", "Average"),
    ("msk", "KafkaDataLogsDiskUsed"):         ("MAX", ["Broker ID", "Cluster Name"], "Cluster Name", "Average"),
    ("msk", "UnderReplicatedPartitions"):     ("SUM", ["Broker ID", "Cluster Name"], "Cluster Name", "Sum"),
}

# Published only at account/region level -- a per-resource query can never
# return data, so it is never sent (billed for nothing otherwise).
AWS_EXTENDED_UNSUPPORTED = {("dynamodb", "UserErrors")}

AWS_SLOW_EXTENDED_SERVICES = {"s3", "logs", "backup", "cloudfront", "wafv2"}


def aws_extended_tier(service, cw_metric_name):
    tier = AWS_EXTENDED_TIER_OVERRIDES.get((service, cw_metric_name))
    if tier:
        return tier
    return "slow_extended" if service in AWS_SLOW_EXTENDED_SERVICES else "extended"


# ── Azure / GCP window sizing ───────────────────────────────────────────
AZURE_PUBLISH_DELAY_SECONDS = 300
GCP_PUBLISH_DELAY_SECONDS = 300


def provider_window_seconds(interval_seconds, publish_delay_seconds):
    return int(interval_seconds) + int(publish_delay_seconds)


# ── Alert freshness derived from polling interval ───────────────────────
EVAL_WINDOW_MIN_BY_INTERVAL = {120: 10, 300: 10, 900: 25, 3600: 75, 86400: 1560}
STALE_MIN_BY_INTERVAL = {120: 20, 300: 20, 900: 45, 3600: 180, 86400: 3000}


def cadence_for_interval(seconds):
    if seconds <= 300:
        return "core"
    if seconds >= 86400:
        return "slow"
    return "extended"


_CLASS_DEFAULT_INTERVAL = {"core": 300, "extended": 3600, "slow": 86400}
_AWS_CORE_TYPES = ("ec2", "ebs", "rds", "lambda", "elb", "ecs")


def _aws_class(resource_type):
    if resource_type in AWS_SLOW_EXTENDED_SERVICES:
        return "slow"
    if resource_type in _AWS_CORE_TYPES:
        return "core"
    return "extended"


def metric_interval_overrides():
    """{(provider, resource_type, lower(db_metric_name)): poll interval s}
    for every scheduled metric whose polling interval differs from what its
    resource-type class (alert_rules.cadence_class_sql) assumes -- so alert
    evaluation windows, breach counting and staleness follow the metric's
    REAL cadence instead of its resource type's."""
    from app.threshold_defaults import resolve_db_metric_name
    from app.aws.metric_catalog_data import CURATED as AWS_CURATED
    from app.providers.azure.metric_catalog_data import CURATED as AZ_CURATED
    from app.providers.gcp.metric_catalog_data import CURATED as GCP_CURATED
    from app.providers.azure import severity_tiers as az
    from app.providers.gcp import severity_tiers as gcp

    out = {}

    def put(provider, rtype, name, seconds, default):
        if seconds != default:
            out[(provider, rtype, name.lower())] = seconds

    for m in AWS_CORE_METRICS:
        put("aws", m.resource_type, m.db_name, TIER_SECONDS[m.tier], 300)
    put("aws", "ec2", "mem_used_percent", TIER_SECONDS[CWAGENT_MEM_TIER], 300)
    put("aws", "ec2", "disk_used_percent", TIER_SECONDS[CWAGENT_DISK_TIER], 300)

    for service, (_d, _ns, category, metrics) in AWS_CURATED.items():
        if category != "extended" or service == "nlb":
            continue
        default = _CLASS_DEFAULT_INTERVAL[_aws_class(service)]
        for (m_name, _u, _s, _dflt, _desc) in metrics:
            put("aws", service, resolve_db_metric_name(service, m_name),
                TIER_SECONDS[aws_extended_tier(service, m_name)], default)

    az_tiers = az.metric_tiers(AZ_CURATED)
    for (service, metric), tier in az_tiers.items():
        put("azure", service, metric, az.TIER_INTERVAL_SECONDS[tier], 300)
    gcp_tiers = gcp.metric_tiers(GCP_CURATED)
    for (service, metric), tier in gcp_tiers.items():
        put("gcp", service, metric, gcp.TIER_INTERVAL_SECONDS[tier], 300)
    return out


# Prefix rules the tuple list can't express (per-mount CWAgent disk metrics
# are named disk_used_percent__<slug>).
PREFIX_INTERVAL_OVERRIDES = [("aws", "ec2", "disk_used_percent__", TIER_SECONDS[CWAGENT_DISK_TIER])]


# ── P1 set: evaluated on every critical tick ────────────────────────────
def p1_metric_keys():
    """{(resource_type, lower(db metric name))} evaluated every 2 min."""
    from app.providers.azure import severity_tiers as az
    from app.providers.gcp import severity_tiers as gcp
    keys = {(m.resource_type, m.db_name) for m in AWS_CORE_METRICS if m.tier == "critical"}
    keys |= set(AWS_DESCRIBE_METRICS)
    for service, names in az.CRITICAL_METRICS.items():
        keys |= {(service, n.lower()) for n in names}
    for service, names in gcp.CRITICAL_METRICS.items():
        keys |= {(service, n.lower()) for n in names}
    return keys
