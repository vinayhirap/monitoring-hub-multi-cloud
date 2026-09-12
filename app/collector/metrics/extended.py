# app/collector/metrics/extended.py
"""
GetMetricData collection for AWS's extended-tier services -- the
collection-side half of app/collector/discovery/extended.py (read that
module's docstring first for the confidence-level breakdown; the same
caveats apply here since dimension building is the same research).

Metric definitions are NOT re-typed here -- they're generated straight
from the existing, already-curated app/aws/metric_catalog_data.py
CURATED table, reusing app/threshold_defaults.py's
resolve_db_metric_name() for the CloudWatch-name -> db-name mapping so
this collector's has_data checks agree with every other has_data check
in the app (Settings -> Metric Thresholds, Metrics to Monitor, etc.) --
a single source of truth for that mapping rather than a second,
possibly-drifting copy.
"""
import json
import logging

from app.db import get_connection
from app.aws.metric_catalog_data import CURATED
from app.threshold_defaults import resolve_db_metric_name
from app.collector.metrics.runner import _execute_gmd

logger = logging.getLogger(__name__)

# service_key -> [(CW_MetricName, db_metric_name, Statistic, Namespace), ...]
# Built once at import time from CURATED -- editing metric_catalog_data.py
# and re-running scripts/seed_metric_catalog.py is enough to pick up
# catalog changes; no separate list to keep in sync here.
EXTENDED_METRICS = {}
for _service_key, (_display, _namespace, _category, _metrics) in CURATED.items():
    if _category != "extended" or _service_key == "nlb":
        continue  # nlb already covered by the core ELB collector
    EXTENDED_METRICS[_service_key] = [
        (m_name, resolve_db_metric_name(_service_key, m_name), stat, _namespace)
        for (m_name, unit, stat, is_default, desc) in _metrics
    ]

# Services confirmed, via a live 24h metric_history audit on 2026-09-12
# (AuroGov Mumbai), to publish zero datapoints regardless of how often
# they're polled: S3 storage metrics publish once/day (AWS-confirmed),
# CloudWatch Logs DeliveryErrors/Backup job-failure counters/CloudFront
# request-rate metrics/WAFv2 BlockedRequests only emit on rare events,
# not on a fixed short interval. Polling any of these hourly (or every
# 15 min, before this patch) can only ever return empty -- AWS bills
# per request regardless of whether data comes back. Moved to a 24h
# cadence: still catches the once-a-day S3 storage snapshot and any of
# the rare-event counters within a day, at 1/24th the request volume.
#
# SQS/Kinesis were ALSO all-zero in that same audit, but deliberately
# NOT included here: their metrics only publish when there's queue/
# stream activity at all (not a slow-vs-fast publish-rate issue), so a
# slower poll doesn't fix anything -- it just means finding out about
# real activity up to 24h late. Left on the hourly "extended" tier;
# worth checking with the resource owners whether those specific
# queues/streams are actually still in use before touching further.
SLOW_EXTENDED_SERVICES = {"s3", "logs", "backup", "cloudfront", "wafv2"}


# ── Per-service dimension builders ──────────────────────────────
#
# Each returns the FULL list of CloudWatch Dimensions for a given
# resource row (a dict with at least resource_id / name / tags keys).
# Most services need just one dimension (resource_id IS the dimension
# value, by construction of how discovery/extended.py wrote it) --
# those fall through to _default_dims. The handful needing a second,
# static dimension (CloudFront, OpenSearch, WAFv2) read it back out of
# the `cw_extra_dims` key discovery/extended.py stashed in `tags`.

_SIMPLE_DIM_NAME = {
    "dynamodb":           "TableName",
    "sqs":                "QueueName",
    "sns":                "TopicName",
    "kinesis":            "StreamName",
    "firehose":            "DeliveryStreamName",
    "autoscaling":        "AutoScalingGroupName",
    "natgateway":         "NatGatewayId",
    "efs":                "FileSystemId",
    "elasticache":        "CacheClusterId",
    "redshift":           "ClusterIdentifier",
    "memorydb":           "ClusterName",
    "dax":                "ClusterId",
    "states":             "StateMachineArn",
    "events":             "RuleName",
    "kms":                "KeyId",
    "certificatemanager": "CertificateArn",
    "backup":             "BackupVaultName",
    "cognito":            "UserPool",
    "logs":               "LogGroupName",
    "dms":                "ReplicationInstanceIdentifier",
    "directconnect":      "ConnectionId",
    "eks":                "ClusterName",
    "documentdb":         "DBClusterIdentifier",
    "neptune":            "DBClusterIdentifier",
    "apigateway":         "ApiName",
    "route53":            "HealthCheckId",
    "msk":                "Cluster Name",  # literal space -- see extended.py docstring
    "transitgateway":     "TransitGateway",
    "vpn":                "VpnId",
    "ecs":                "ClusterName",
    "s3":                 "BucketName",
}

# Services needing a second, static dimension beyond resource_id -- read
# back from tags.cw_extra_dims (set by discovery/extended.py). Primary
# dimension name for each is still looked up the normal way below.
_SIMPLE_DIM_NAME.update({
    "cloudfront": "DistributionId",
    "opensearch": "DomainName",
    "wafv2":      "WebACL",
})


_S3_STORAGE_METRICS = {"BucketSizeBytes", "NumberOfObjects"}
_S3_REQUEST_METRICS = {"AllRequests", "4xxErrors", "5xxErrors", "FirstByteLatency", "TotalRequestLatency"}


def _build_dimensions(resource, cw_metric_name=None):
    """resource: dict with resource_type, resource_id, tags (already
    json.loads'd dict, or None). cw_metric_name is only consulted for
    s3, where different metrics in the SAME service need different
    second dimensions -- every other service's dimensions depend only
    on the resource, so passing/omitting it changes nothing for them."""
    rt = resource["resource_type"]
    dim_name = _SIMPLE_DIM_NAME.get(rt)
    if not dim_name:
        return None
    # ECS is the one service here whose resources.resource_id is a full
    # ARN (discovery/runner.py's _discover_ecs stores c["clusterArn"]),
    # not a bare name -- every other _SIMPLE_DIM_NAME service's
    # resource_id already IS the correct dimension value directly. The
    # bare cluster name CloudWatch's ClusterName dimension actually needs
    # is in resources.name instead (same row, stored separately).
    dim_value = resource["name"] if rt == "ecs" else resource["resource_id"]
    dims = [{"Name": dim_name, "Value": dim_value}]
    tags = resource.get("tags") or {}
    extra = tags.get("cw_extra_dims") if isinstance(tags, dict) else None
    if extra:
        for k, v in extra.items():
            dims.append({"Name": k, "Value": v})
    if rt == "s3":
        # S3's CloudWatch dimensions differ by WHICH metric is being
        # requested, not just by resource: storage metrics (free,
        # always published) need StorageType; request metrics need
        # FilterId, which additionally requires a per-bucket "request
        # metrics" filter to be manually enabled in the S3 console
        # first -- an AWS-side prerequisite this code cannot create.
        # "EntireBucket" is the name AWS's own console suggests by
        # default when enabling it for the whole bucket; a bucket using
        # a different filter name, or with request metrics never
        # enabled at all, will show no data for these specific metrics
        # regardless of correct dimensions.
        if cw_metric_name in _S3_STORAGE_METRICS:
            dims.append({"Name": "StorageType", "Value": "StandardStorage"})
        elif cw_metric_name in _S3_REQUEST_METRICS:
            dims.append({"Name": "FilterId", "Value": "EntireBucket"})
    return dims


def _region_for_service(service_key, resource_region):
    """A handful of extended services' CloudWatch metrics only exist in
    a fixed region regardless of where the resource itself is discovered
    from -- see discovery/extended.py's docstring for why."""
    if service_key == "cloudfront":
        return "us-east-1"
    if service_key == "globalaccelerator":
        return "us-west-2"
    return resource_region


def _enabled_extended_metrics(cur, account_id):
    """
    {(service, metric_name), ...} for this account's actually-enabled
    extended-tier selection -- the same account_metric_selections table
    Azure's _enabled_azure_metrics() and GCP's _enabled_gcp_metrics()
    already filter against, and that Settings -> Metrics writes to.

    Before this, extended.py was the one collector in this codebase that
    ignored account_metric_selections entirely and just polled every
    metric_catalog row for a service regardless of enabled state --
    billing for metrics the UI itself showed as switched off. Discovery's
    enable_metrics_for_services() only ever auto-enables is_default=1
    metrics (additive-only, never disables), so this filter doesn't drop
    anything currently considered "on" -- it stops paying for the ~55%
    of extended-tier metrics (169 -> 76 across the curated extended
    services) that were never enabled to begin with, and makes
    unchecking a metric in Settings -> Metrics actually reduce spend.
    """
    cur.execute("""
        SELECT mc.service, mc.metric_name
        FROM metric_catalog mc
        JOIN account_metric_selections ams ON ams.metric_id = mc.id
        WHERE ams.aws_account_id = %s AND ams.enabled = 1
              AND mc.provider = 'aws' AND mc.category = 'extended'
    """, (account_id,))
    return {(row["service"], row["metric_name"]) for row in cur.fetchall()}


def _collect_extended_service(cw, resources, service_key, enabled_keys):
    """resources: list of dicts (id, resource_id, resource_type, name,
    region, tags) all belonging to the same (service_key, region) group.
    Mirrors app/collector/metrics/runner.py's _build_queries/_execute_gmd
    shape but supports multi-dimension metrics via _build_dimensions."""
    metric_defs = EXTENDED_METRICS.get(service_key)
    if not metric_defs:
        return 0

    # Only the metrics this account has actually enabled -- see
    # _enabled_extended_metrics()'s docstring for why this filter exists.
    metric_defs = [d for d in metric_defs if (service_key, d[0]) in enabled_keys]
    if not metric_defs:
        return 0

    queries = []
    id_map = {}
    for r in resources:
        for cw_name, db_name, stat, namespace in metric_defs:
            dims = _build_dimensions(r, cw_name)
            if not dims:
                continue
            qid = f"ext{len(queries)}"
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace":  namespace,
                        "MetricName": cw_name,
                        "Dimensions": dims,
                    },
                    "Period": 300,
                    "Stat":   stat,
                },
                "ReturnData": True,
            })
            id_map[qid] = (r["id"], db_name)

    if not queries:
        return 0

    n = _execute_gmd(cw, queries, id_map, minutes=16)
    logger.info(f"    Extended[{service_key}]: {n} datapoints / {len(resources)} resources")
    return n


def _get_extended_resources_for_account(account_id):
    """
    Same shape as app/collector/metrics/runner.py's
    _get_resources_for_account, but also selects `tags` (needed for the
    cw_extra_dims lookup multi-dimension services stash there) and
    filters to only the extended-tier resource_types this module knows
    how to collect -- kept as its own query rather than modifying the
    existing core query, so core collection's row shape/behavior is
    untouched by this addition.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    placeholders = ",".join(["%s"] * len(EXTENDED_METRICS))
    cursor.execute(f"""
        SELECT id, resource_id, resource_type, name, region, tags
        FROM resources
        WHERE aws_account_id = %s
          AND resource_type IN ({placeholders})
    """, (account_id, *EXTENDED_METRICS.keys()))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    grouped = {}
    for r in rows:
        if isinstance(r.get("tags"), str):
            try:
                r["tags"] = json.loads(r["tags"])
            except (TypeError, ValueError):
                r["tags"] = {}
        key = (r["resource_type"], r["region"])
        grouped.setdefault(key, []).append(r)
    return grouped


def collect_extended_for_account(session, account, tier="extended"):
    """
    Queries this account's already-discovered extended-tier resources
    and runs GetMetricData for each (resource_type, region) group.

    tier = 'extended'      -- everything except SLOW_EXTENDED_SERVICES,
                               called hourly (scheduler.py's "extended" tier)
    tier = 'slow_extended' -- only SLOW_EXTENDED_SERVICES, called once
                               a day (scheduler.py's "slow_extended" tier)
                               -- see SLOW_EXTENDED_SERVICES' docstring
                               above for why these are split out.
    """
    grouped_resources = _get_extended_resources_for_account(account["id"])
    if not grouped_resources:
        return

    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        enabled_keys = _enabled_extended_metrics(cur, account["id"])
    finally:
        cur.close()
        conn.close()

    if not enabled_keys:
        logger.info(f"    Extended: no enabled extended-tier metrics for [{account['account_name']}] -- skipping")
        return

    for (resource_type, region), resources in grouped_resources.items():
        if resource_type not in EXTENDED_METRICS:
            continue
        is_slow = resource_type in SLOW_EXTENDED_SERVICES
        if (tier == "slow_extended") != is_slow:
            continue
        cw_region = _region_for_service(resource_type, region)
        try:
            cw = session.client("cloudwatch", region_name=cw_region)
            _collect_extended_service(cw, resources, resource_type, enabled_keys)
        except Exception as e:
            logger.error(f"  Extended collection failed [{resource_type}/{cw_region}] "
                         f"[{account['account_name']}]: {e}")
