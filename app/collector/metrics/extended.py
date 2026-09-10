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
}

# Services needing a second, static dimension beyond resource_id -- read
# back from tags.cw_extra_dims (set by discovery/extended.py). Primary
# dimension name for each is still looked up the normal way below.
_SIMPLE_DIM_NAME.update({
    "cloudfront": "DistributionId",
    "opensearch": "DomainName",
    "wafv2":      "WebACL",
})


def _build_dimensions(resource):
    """resource: dict with resource_type, resource_id, tags (already
    json.loads'd dict, or None)."""
    rt = resource["resource_type"]
    dim_name = _SIMPLE_DIM_NAME.get(rt)
    if not dim_name:
        return None
    dims = [{"Name": dim_name, "Value": resource["resource_id"]}]
    tags = resource.get("tags") or {}
    extra = tags.get("cw_extra_dims") if isinstance(tags, dict) else None
    if extra:
        for k, v in extra.items():
            dims.append({"Name": k, "Value": v})
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


def _collect_extended_service(cw, resources, service_key):
    """resources: list of dicts (id, resource_id, resource_type, name,
    region, tags) all belonging to the same (service_key, region) group.
    Mirrors app/collector/metrics/runner.py's _build_queries/_execute_gmd
    shape but supports multi-dimension metrics via _build_dimensions."""
    metric_defs = EXTENDED_METRICS.get(service_key)
    if not metric_defs:
        return 0

    queries = []
    id_map = {}
    for r in resources:
        dims = _build_dimensions(r)
        if not dims:
            continue
        for cw_name, db_name, stat, namespace in metric_defs:
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


def collect_extended_for_account(session, account):
    """
    Queries this account's already-discovered extended-tier resources
    and runs GetMetricData for each (resource_type, region) group.
    Meant to be called once per account at the "low" tier, same
    cadence as EC2 CWAgent mem/disk -- these aren't latency-sensitive
    signals worth polling every 60-300s.
    """
    grouped_resources = _get_extended_resources_for_account(account["id"])
    for (resource_type, region), resources in grouped_resources.items():
        if resource_type not in EXTENDED_METRICS:
            continue
        cw_region = _region_for_service(resource_type, region)
        try:
            cw = session.client("cloudwatch", region_name=cw_region)
            _collect_extended_service(cw, resources, resource_type)
        except Exception as e:
            logger.error(f"  Extended collection failed [{resource_type}/{cw_region}] "
                         f"[{account['account_name']}]: {e}")
