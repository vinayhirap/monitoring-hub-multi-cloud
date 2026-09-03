# app/aws/resource_discovery.py
"""
Auto-detects which AWS services are actually in use in an account/region,
so metric selection can be driven by what's really there instead of a
manual checkbox pick.

Two complementary signals, because neither one alone is trustworthy:

  1. Resource Groups Tagging API (`resourcegroupstaggingapi:GetResources`)
     — one cheap paginated call covers ~35 of our 40 curated services at
     once. CAVEAT (confirmed via scripts/check_resource_tags.py against a
     real account): this API — and therefore YACE's own discovery job,
     which uses the same API — only returns resources that have at least
     one tag. An untagged S3 bucket or Lambda function is invisible here,
     not because of a bug in this app, but because AWS's tagging index
     simply never indexed it. This is a real, external limitation, not
     something this module can fully paper over.

  2. The existing Describe/List-API discovery already run every 15 min in
     app/collector/discovery/runner.py for EC2/RDS/ELB/ECS/Lambda. That
     path does NOT depend on tags at all (describe_instances etc. return
     every resource regardless of tagging), so it's the authoritative
     signal for those 5 "core" service keys — the tagging sweep is only
     the primary signal for the ~35 "extended"/"directory" service keys
     that have no dedicated describe-based collector.

Combine both (see app/collector/discovery/runner.py) for the best
available picture; callers should not rely on this module alone for
core services.
"""
import logging
import re

logger = logging.getLogger(__name__)

# ARN service segment ("arn:aws:<service>:...") -> our metric_catalog
# service key, for every unambiguous 1:1 case. Case is exactly what AWS
# uses in the ARN.
#
# Deliberately excludes:
#   - "elasticloadbalancing" (ARN resource path alone tells app/net/gwy —
#     handled in _classify_elb below, not a fixed 1:1 map entry)
#   - "rds" (ARN alone can't distinguish plain RDS from DocumentDB or
#     Neptune, which also live under the rds:// service ARN — handled by
#     disambiguate_rds_family() via a describe call)
ARN_SERVICE_MAP = {
    "lambda":            "lambda",
    "ecs":               "ecs",
    "apigateway":        "apigateway",
    "dynamodb":          "dynamodb",
    "sqs":               "sqs",
    "sns":               "sns",
    "cloudfront":        "cloudfront",
    "elasticache":       "elasticache",
    "es":                "opensearch",
    "eks":               "eks",
    "elasticfilesystem": "efs",
    "kafka":             "msk",
    "kinesis":           "kinesis",
    "firehose":          "firehose",
    "autoscaling":       "autoscaling",
    "route53":           "route53",
    "wafv2":             "wafv2",
    "redshift":          "redshift",
    "memorydb":          "memorydb",
    "dax":               "dax",
    "states":            "states",
    "events":            "events",
    "kms":               "kms",
    "acm":               "certificatemanager",
    "backup":            "backup",
    "cognito-idp":       "cognito",
    "logs":              "logs",
    "globalaccelerator": "globalaccelerator",
    "dms":               "dms",
    "directconnect":     "directconnect",
    "s3":                "s3",
    "ec2":               None,  # handled separately below (natgw/tgw/vpn/instance/volume all share this prefix)
}

# Services whose Tagging API index — and therefore YACE discovery — is
# only ever populated in us-east-1, regardless of which region the
# resources themselves live in or which region the account defaults to.
# (Confirmed this session for CloudFront; Route53 and WAFv2-global follow
# the same global-resource rule.)
GLOBAL_NAMESPACE_SERVICES = {"cloudfront", "route53", "s3"}

_EC2_ARN_RESOURCE_MAP = {
    "natgateway":      "natgateway",
    "transit-gateway": "transitgateway",
    "vpn-connection":  "vpn",
}


def _classify_arn(arn: str) -> str | None:
    """arn:aws:<service>:<region>:<account>:<resource> -> service_key or None."""
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return None
    service, resource = parts[2], parts[5]

    if service == "elasticloadbalancing":
        if resource.startswith("loadbalancer/app/"):
            return "alb"
        if resource.startswith("loadbalancer/net/"):
            return "nlb"
        return None  # gateway-LB etc. — not in our curated catalog

    if service == "ec2":
        resource_type = resource.split("/", 1)[0]
        return _EC2_ARN_RESOURCE_MAP.get(resource_type)

    if service == "rds":
        return None  # ambiguous — see disambiguate_rds_family

    mapped = ARN_SERVICE_MAP.get(service)
    return mapped


def _sweep_region(tagging_client) -> set[str]:
    found = set()
    paginator = tagging_client.get_paginator("get_resources")
    for page in paginator.paginate():
        for item in page.get("ResourceTagMappingList", []):
            key = _classify_arn(item.get("ResourceARN", ""))
            if key:
                found.add(key)
    return found


def discover_tagged_service_keys(session, region: str) -> set[str]:
    """
    One paginated sweep of every TAGGED resource in `region`, plus a
    second sweep in us-east-1 for the handful of services whose tagging
    index is global-only. Returns the set of metric_catalog service keys
    found. Resources with zero tags are invisible to this call — that's
    an AWS Tagging API limitation, not a bug here (see module docstring).
    """
    found: set[str] = set()
    try:
        regional = session.client("resourcegroupstaggingapi", region_name=region)
        found |= _sweep_region(regional)
    except Exception as e:
        logger.error(f"Tagging API sweep failed [{region}]: {e}")

    if region != "us-east-1":
        try:
            global_client = session.client("resourcegroupstaggingapi", region_name="us-east-1")
            global_found = _sweep_region(global_client)
            # Only trust the us-east-1 sweep for the services that are
            # genuinely global-indexed; a regional service's ARNs showing
            # up here would just mean the account also has resources in
            # us-east-1 itself, which isn't what we're asking.
            found |= (global_found & GLOBAL_NAMESPACE_SERVICES)
        except Exception as e:
            logger.error(f"Tagging API global sweep failed: {e}")

    return found


def disambiguate_rds_family(session, region: str, service_keys: set[str]) -> set[str]:
    """
    The Tagging API can't tell plain RDS apart from DocumentDB/Neptune —
    all three share the `arn:aws:rds:...` prefix. If any 'rds'-service
    ARNs were seen, make one real describe call to resolve engine names
    into the correct catalog key(s). Mutates and returns a new set;
    doesn't touch service_keys that came from elsewhere.
    """
    result = set(service_keys)
    try:
        rds = session.client("rds", region_name=region)
        engines = set()

        for page in rds.get_paginator("describe_db_instances").paginate():
            for db in page.get("DBInstances", []):
                engines.add(db.get("Engine", ""))

        try:
            for page in rds.get_paginator("describe_db_clusters").paginate():
                for c in page.get("DBClusters", []):
                    engines.add(c.get("Engine", ""))
        except Exception:
            pass  # some accounts/regions have no cluster API access; instance pass already covers plain RDS

        if any(e == "docdb" for e in engines):
            result.add("documentdb")
        if any(e == "neptune" for e in engines):
            result.add("neptune")
        if any(e not in ("docdb", "neptune") for e in engines):
            result.add("rds")
    except Exception as e:
        logger.error(f"RDS engine disambiguation failed [{region}]: {e}")
        # Fall back to the conservative guess: assume plain RDS rather than
        # silently dropping the signal that *something* under rds:// exists.
        result.add("rds")

    return result


def discover_all_service_keys(session, region: str) -> set[str]:
    """
    Full sweep: tagging API for everything else, plus one always-run RDS
    describe call (since _classify_arn always returns None for rds://
    ARNs, 'rds'/'documentdb'/'neptune' can only ever be added here).
    """
    found = discover_tagged_service_keys(session, region)
    found = disambiguate_rds_family(session, region, found)
    return found
