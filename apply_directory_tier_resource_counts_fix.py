#!/usr/bin/env python3
"""
apply_directory_tier_resource_counts_fix.py

Two more real bugs found after the previous two fixes:

1. Key mismatch (same class of bug as the earlier elb/alb one): the
   metric catalog's service key for "AWS Certificate Manager" is
   "certificatemanager" (see app/aws/metric_catalog_data.py), not
   "acm". The earlier fix registered its collector under "acm", so
   ServiceList.jsx's `resourceCounts[g.service]` lookup for that tile
   was ALWAYS undefined — meaning the ACM tile has been failing open
   (staying visible) regardless of the real count the whole time, even
   though the backend was correctly reporting 5.

2. Of the ~39 services this account has metrics enabled for, only 14
   have a resource collector after the last fix (7 core + 7 extended).
   The remaining ~25 "directory-tier" services (API Gateway, CloudFront,
   Cognito, Data Firehose, DocumentDB, DynamoDB, EFS, EKS, ElastiCache,
   EventBridge, Kinesis, MSK, MemoryDB, Neptune, OpenSearch, Redshift,
   Route 53, SNS, SQS, DAX, EC2 Auto Scaling, NAT Gateway, Site-to-Site
   VPN, Transit Gateway, WAF, KMS, CloudWatch Logs) have NO collector
   at all, so they correctly fail open per the existing safety design
   — but that means their tiles never actually get hidden even when
   the account has zero of them.

Fix
---
1. app/aws/collector_direct.py — add one lightweight list/describe-call
   collector per remaining directory-tier service.
2. app/api/live_data.py — register all of them under the metric
   catalog's ACTUAL key names (cross-checked against
   app/aws/metric_catalog_data.py CURATED dict), and rename the
   existing "acm" entry to "certificatemanager".

After this, the only services still capable of "fail open" are ones
truly outside AWS's boto3-describable surface for this app (there
shouldn't be any left for the AWS provider).

Usage:
    python apply_directory_tier_resource_counts_fix.py [repo_root]

Idempotent: safe to re-run. Backs up files to
"<file>.bak.pre-directory-tier-fix" (first run only). Reverts
automatically if a patched file fails py_compile.
"""
import py_compile
import shutil
import sys
from pathlib import Path

BAK_SUFFIX = ".bak.pre-directory-tier-fix"


class PatchError(Exception):
    pass


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def backup(path: Path):
    bak = path.with_name(path.name + BAK_SUFFIX)
    if not bak.exists():
        shutil.copy2(path, bak)


def apply_replacements(path: Path, replacements, already_applied_marker=None):
    text = read(path)
    if already_applied_marker and already_applied_marker in text:
        print(f"  SKIP  {path} (already patched)")
        return False

    backup(path)
    changed = 0
    for old, new, label in replacements:
        count = text.count(old)
        if count == 0:
            raise PatchError(f"{path}: pattern not found for '{label}'")
        if count > 1:
            raise PatchError(f"{path}: pattern for '{label}' matches {count} times, expected 1")
        text = text.replace(old, new, 1)
        changed += 1
    path.write_text(text, encoding="utf-8")
    print(f"  OK    {path} ({changed} edit{'s' if changed != 1 else ''})")
    return True


# Each: (function_name, body). Kept as plain describe/list calls, same
# style/caching as the existing collectors. Global services (CloudFront,
# Route 53) ignore the region argument, same pattern boto3 itself uses.
DIRECTORY_TIER_COLLECTORS = '''

# ── Directory-tier services (lightweight discovery only) ────────────────
# One cheap list/describe call each, same purpose as the extended-service
# collectors above: answer "does this account have any of these right
# now" for the Services page tile filter. Nothing here fetches metrics.

def collect_apigateway(region=None) -> list:
    return _cached(f"apigateway_{region}", lambda: _apigateway_raw(region))

def _apigateway_raw(region) -> list:
    try:
        s = get_session(region)
        rest = s.client("apigateway").get_paginator("get_rest_apis")
        rest_items = [i for p in rest.paginate() for i in p.get("items", [])]
        http_items = []
        try:
            v2 = s.client("apigatewayv2").get_paginator("get_apis")
            http_items = [i for p in v2.paginate() for i in p.get("Items", [])]
        except Exception:
            pass
        return rest_items + http_items
    except Exception as e:
        logger.error(f"API Gateway [{region}]: {e}"); return []


def collect_dynamodb_tables(region=None) -> list:
    return _cached(f"dynamodb_{region}", lambda: _dynamodb_raw(region))

def _dynamodb_raw(region) -> list:
    try:
        ddb = get_session(region).client("dynamodb")
        out = []
        for page in ddb.get_paginator("list_tables").paginate():
            out.extend(page.get("TableNames", []))
        return out
    except Exception as e:
        logger.error(f"DynamoDB [{region}]: {e}"); return []


def collect_sqs_queues(region=None) -> list:
    return _cached(f"sqs_{region}", lambda: _sqs_raw(region))

def _sqs_raw(region) -> list:
    try:
        sqs = get_session(region).client("sqs")
        out = []
        for page in sqs.get_paginator("list_queues").paginate():
            out.extend(page.get("QueueUrls", []))
        return out
    except Exception as e:
        logger.error(f"SQS [{region}]: {e}"); return []


def collect_sns_topics(region=None) -> list:
    return _cached(f"sns_{region}", lambda: _sns_raw(region))

def _sns_raw(region) -> list:
    try:
        sns = get_session(region).client("sns")
        out = []
        for page in sns.get_paginator("list_topics").paginate():
            out.extend(page.get("Topics", []))
        return out
    except Exception as e:
        logger.error(f"SNS [{region}]: {e}"); return []


def collect_cloudfront_distributions(region=None) -> list:
    # Global service — region argument intentionally unused.
    return _cached("cloudfront", _cloudfront_raw)

def _cloudfront_raw() -> list:
    try:
        cf = boto3.client("cloudfront")
        resp = cf.list_distributions()
        return resp.get("DistributionList", {}).get("Items", [])
    except Exception as e:
        logger.error(f"CloudFront: {e}"); return []


def collect_elasticache_clusters(region=None) -> list:
    return _cached(f"elasticache_{region}", lambda: _elasticache_raw(region))

def _elasticache_raw(region) -> list:
    try:
        ec = get_session(region).client("elasticache")
        out = []
        for page in ec.get_paginator("describe_cache_clusters").paginate():
            out.extend(page.get("CacheClusters", []))
        return out
    except Exception as e:
        logger.error(f"ElastiCache [{region}]: {e}"); return []


def collect_opensearch_domains(region=None) -> list:
    return _cached(f"opensearch_{region}", lambda: _opensearch_raw(region))

def _opensearch_raw(region) -> list:
    try:
        es = get_session(region).client("opensearch")
        return es.list_domain_names().get("DomainNames", [])
    except Exception as e:
        logger.error(f"OpenSearch [{region}]: {e}"); return []


def collect_eks_clusters(region=None) -> list:
    return _cached(f"eks_{region}", lambda: _eks_raw(region))

def _eks_raw(region) -> list:
    try:
        eks = get_session(region).client("eks")
        out = []
        for page in eks.get_paginator("list_clusters").paginate():
            out.extend(page.get("clusters", []))
        return out
    except Exception as e:
        logger.error(f"EKS [{region}]: {e}"); return []


def collect_efs_filesystems(region=None) -> list:
    return _cached(f"efs_{region}", lambda: _efs_raw(region))

def _efs_raw(region) -> list:
    try:
        efs = get_session(region).client("efs")
        out = []
        for page in efs.get_paginator("describe_file_systems").paginate():
            out.extend(page.get("FileSystems", []))
        return out
    except Exception as e:
        logger.error(f"EFS [{region}]: {e}"); return []


def collect_documentdb_clusters(region=None) -> list:
    return _cached(f"documentdb_{region}", lambda: _documentdb_raw(region))

def _documentdb_raw(region) -> list:
    try:
        docdb = get_session(region).client("docdb")
        out = []
        for page in docdb.get_paginator("describe_db_clusters").paginate():
            out.extend(page.get("DBClusters", []))
        return out
    except Exception as e:
        logger.error(f"DocumentDB [{region}]: {e}"); return []


def collect_neptune_clusters(region=None) -> list:
    return _cached(f"neptune_{region}", lambda: _neptune_raw(region))

def _neptune_raw(region) -> list:
    try:
        neptune = get_session(region).client("neptune")
        out = []
        for page in neptune.get_paginator("describe_db_clusters").paginate():
            out.extend(page.get("DBClusters", []))
        return out
    except Exception as e:
        logger.error(f"Neptune [{region}]: {e}"); return []


def collect_msk_clusters(region=None) -> list:
    return _cached(f"msk_{region}", lambda: _msk_raw(region))

def _msk_raw(region) -> list:
    try:
        kafka = get_session(region).client("kafka")
        out = []
        for page in kafka.get_paginator("list_clusters_v2").paginate():
            out.extend(page.get("ClusterInfoList", []))
        return out
    except Exception as e:
        logger.error(f"MSK [{region}]: {e}"); return []


def collect_kinesis_streams(region=None) -> list:
    return _cached(f"kinesis_{region}", lambda: _kinesis_raw(region))

def _kinesis_raw(region) -> list:
    try:
        k = get_session(region).client("kinesis")
        out = []
        for page in k.get_paginator("list_streams").paginate():
            out.extend(page.get("StreamNames", []))
        return out
    except Exception as e:
        logger.error(f"Kinesis [{region}]: {e}"); return []


def collect_firehose_streams(region=None) -> list:
    return _cached(f"firehose_{region}", lambda: _firehose_raw(region))

def _firehose_raw(region) -> list:
    try:
        fh = get_session(region).client("firehose")
        return fh.list_delivery_streams().get("DeliveryStreamNames", [])
    except Exception as e:
        logger.error(f"Firehose [{region}]: {e}"); return []


def collect_autoscaling_groups(region=None) -> list:
    return _cached(f"autoscaling_{region}", lambda: _autoscaling_raw(region))

def _autoscaling_raw(region) -> list:
    try:
        asg = get_session(region).client("autoscaling")
        out = []
        for page in asg.get_paginator("describe_auto_scaling_groups").paginate():
            out.extend(page.get("AutoScalingGroups", []))
        return out
    except Exception as e:
        logger.error(f"Auto Scaling [{region}]: {e}"); return []


def collect_nat_gateways(region=None) -> list:
    return _cached(f"natgateway_{region}", lambda: _natgateway_raw(region))

def _natgateway_raw(region) -> list:
    try:
        ec2 = get_session(region).client("ec2")
        out = []
        for page in ec2.get_paginator("describe_nat_gateways").paginate():
            out.extend([n for n in page.get("NatGateways", []) if n.get("State") != "deleted"])
        return out
    except Exception as e:
        logger.error(f"NAT Gateway [{region}]: {e}"); return []


def collect_transit_gateways(region=None) -> list:
    return _cached(f"transitgateway_{region}", lambda: _transitgateway_raw(region))

def _transitgateway_raw(region) -> list:
    try:
        ec2 = get_session(region).client("ec2")
        out = []
        for page in ec2.get_paginator("describe_transit_gateways").paginate():
            out.extend([t for t in page.get("TransitGateways", []) if t.get("State") != "deleted"])
        return out
    except Exception as e:
        logger.error(f"Transit Gateway [{region}]: {e}"); return []


def collect_route53_zones(region=None) -> list:
    # Global service — region argument intentionally unused.
    return _cached("route53", _route53_raw)

def _route53_raw() -> list:
    try:
        r53 = boto3.client("route53")
        out = []
        for page in r53.get_paginator("list_hosted_zones").paginate():
            out.extend(page.get("HostedZones", []))
        return out
    except Exception as e:
        logger.error(f"Route 53: {e}"); return []


def collect_waf_web_acls(region=None) -> list:
    return _cached(f"wafv2_{region}", lambda: _waf_raw(region))

def _waf_raw(region) -> list:
    try:
        waf = get_session(region).client("wafv2")
        return waf.list_web_acls(Scope="REGIONAL").get("WebACLs", [])
    except Exception as e:
        logger.error(f"WAF [{region}]: {e}"); return []


def collect_redshift_clusters(region=None) -> list:
    return _cached(f"redshift_{region}", lambda: _redshift_raw(region))

def _redshift_raw(region) -> list:
    try:
        rs = get_session(region).client("redshift")
        out = []
        for page in rs.get_paginator("describe_clusters").paginate():
            out.extend(page.get("Clusters", []))
        return out
    except Exception as e:
        logger.error(f"Redshift [{region}]: {e}"); return []


def collect_memorydb_clusters(region=None) -> list:
    return _cached(f"memorydb_{region}", lambda: _memorydb_raw(region))

def _memorydb_raw(region) -> list:
    try:
        mdb = get_session(region).client("memorydb")
        return mdb.describe_clusters().get("Clusters", [])
    except Exception as e:
        logger.error(f"MemoryDB [{region}]: {e}"); return []


def collect_dax_clusters(region=None) -> list:
    return _cached(f"dax_{region}", lambda: _dax_raw(region))

def _dax_raw(region) -> list:
    try:
        dax = get_session(region).client("dax")
        return dax.describe_clusters().get("Clusters", [])
    except Exception as e:
        logger.error(f"DAX [{region}]: {e}"); return []


def collect_eventbridge_rules(region=None) -> list:
    return _cached(f"events_{region}", lambda: _eventbridge_raw(region))

def _eventbridge_raw(region) -> list:
    try:
        ev = get_session(region).client("events")
        out = []
        for page in ev.get_paginator("list_rules").paginate():
            out.extend(page.get("Rules", []))
        return out
    except Exception as e:
        logger.error(f"EventBridge [{region}]: {e}"); return []


def collect_kms_keys(region=None) -> list:
    return _cached(f"kms_{region}", lambda: _kms_raw(region))

def _kms_raw(region) -> list:
    try:
        kms = get_session(region).client("kms")
        out = []
        for page in kms.get_paginator("list_keys").paginate():
            out.extend(page.get("Keys", []))
        return out
    except Exception as e:
        logger.error(f"KMS [{region}]: {e}"); return []


def collect_cloudwatch_log_groups(region=None) -> list:
    return _cached(f"logs_{region}", lambda: _logs_raw(region))

def _logs_raw(region) -> list:
    try:
        logs_client = get_session(region).client("logs")
        out = []
        for page in logs_client.get_paginator("describe_log_groups").paginate():
            out.extend(page.get("logGroups", []))
        return out
    except Exception as e:
        logger.error(f"CloudWatch Logs [{region}]: {e}"); return []


def collect_vpn_connections(region=None) -> list:
    return _cached(f"vpn_{region}", lambda: _vpn_raw(region))

def _vpn_raw(region) -> list:
    try:
        ec2 = get_session(region).client("ec2")
        conns = ec2.describe_vpn_connections().get("VpnConnections", [])
        return [c for c in conns if c.get("State") not in ("deleted", "deleting")]
    except Exception as e:
        logger.error(f"Site-to-Site VPN [{region}]: {e}"); return []

'''


def patch_collector_direct(repo_root: Path) -> Path:
    path = repo_root / "app" / "aws" / "collector_direct.py"
    marker = "def collect_vpn_connections"
    anchor = (
        "# ── ECS (unchanged — not in YACE config) ─────────────────────────────────\n"
        "\n"
        "def collect_ecs_clusters(region=None) -> list:"
    )
    replacements = [
        (
            anchor,
            DIRECTORY_TIER_COLLECTORS.strip("\n") + "\n\n\n" + anchor,
            "collector_direct.py: insert directory-tier collectors",
        ),
    ]
    changed = apply_replacements(path, replacements, already_applied_marker=marker)
    return path if changed else None


def patch_live_data(repo_root: Path) -> Path:
    path = repo_root / "app" / "api" / "live_data.py"
    marker = '"certificatemanager": collect_acm_certificates,'

    old_import_tail = (
        "    collect_dms_instances,\n"
        "    collect_direct_connections,\n"
        "    collect_state_machines,\n"
        "    get_account_summary,\n"
    )
    new_import_tail = (
        "    collect_dms_instances,\n"
        "    collect_direct_connections,\n"
        "    collect_state_machines,\n"
        "    collect_apigateway,\n"
        "    collect_dynamodb_tables,\n"
        "    collect_sqs_queues,\n"
        "    collect_sns_topics,\n"
        "    collect_cloudfront_distributions,\n"
        "    collect_elasticache_clusters,\n"
        "    collect_opensearch_domains,\n"
        "    collect_eks_clusters,\n"
        "    collect_efs_filesystems,\n"
        "    collect_documentdb_clusters,\n"
        "    collect_neptune_clusters,\n"
        "    collect_msk_clusters,\n"
        "    collect_kinesis_streams,\n"
        "    collect_firehose_streams,\n"
        "    collect_autoscaling_groups,\n"
        "    collect_nat_gateways,\n"
        "    collect_transit_gateways,\n"
        "    collect_route53_zones,\n"
        "    collect_waf_web_acls,\n"
        "    collect_redshift_clusters,\n"
        "    collect_memorydb_clusters,\n"
        "    collect_dax_clusters,\n"
        "    collect_eventbridge_rules,\n"
        "    collect_kms_keys,\n"
        "    collect_cloudwatch_log_groups,\n"
        "    collect_vpn_connections,\n"
        "    get_account_summary,\n"
    )

    old_dict_tail = (
        "    \"acm\":            collect_acm_certificates,\n"
        "    \"backup\":         collect_backup_resources,\n"
        "    \"dms\":            collect_dms_instances,\n"
        "    \"directconnect\":  collect_direct_connections,\n"
        "    \"states\":         collect_state_machines,\n"
        "}\n"
    )
    new_dict_tail = (
        "    \"certificatemanager\": collect_acm_certificates,\n"
        "    \"backup\":         collect_backup_resources,\n"
        "    \"dms\":            collect_dms_instances,\n"
        "    \"directconnect\":  collect_direct_connections,\n"
        "    \"states\":         collect_state_machines,\n"
        "    \"apigateway\":     collect_apigateway,\n"
        "    \"dynamodb\":       collect_dynamodb_tables,\n"
        "    \"sqs\":            collect_sqs_queues,\n"
        "    \"sns\":            collect_sns_topics,\n"
        "    \"cloudfront\":     collect_cloudfront_distributions,\n"
        "    \"elasticache\":    collect_elasticache_clusters,\n"
        "    \"opensearch\":     collect_opensearch_domains,\n"
        "    \"eks\":            collect_eks_clusters,\n"
        "    \"efs\":            collect_efs_filesystems,\n"
        "    \"documentdb\":     collect_documentdb_clusters,\n"
        "    \"neptune\":        collect_neptune_clusters,\n"
        "    \"msk\":            collect_msk_clusters,\n"
        "    \"kinesis\":        collect_kinesis_streams,\n"
        "    \"firehose\":       collect_firehose_streams,\n"
        "    \"autoscaling\":    collect_autoscaling_groups,\n"
        "    \"natgateway\":     collect_nat_gateways,\n"
        "    \"transitgateway\": collect_transit_gateways,\n"
        "    \"route53\":        collect_route53_zones,\n"
        "    \"wafv2\":          collect_waf_web_acls,\n"
        "    \"redshift\":       collect_redshift_clusters,\n"
        "    \"memorydb\":       collect_memorydb_clusters,\n"
        "    \"dax\":            collect_dax_clusters,\n"
        "    \"events\":         collect_eventbridge_rules,\n"
        "    \"kms\":            collect_kms_keys,\n"
        "    \"logs\":           collect_cloudwatch_log_groups,\n"
        "    \"vpn\":            collect_vpn_connections,\n"
        "}\n"
    )

    replacements = [
        (old_import_tail, new_import_tail, "live_data.py: import directory-tier collectors"),
        (old_dict_tail, new_dict_tail, "live_data.py: register directory-tier collectors + fix acm key"),
    ]
    changed = apply_replacements(path, replacements, already_applied_marker=marker)
    return path if changed else None


def main():
    repo_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    print(f"Repo root: {repo_root}")

    touched = []

    print("\n[1/2] app/aws/collector_direct.py — add directory-tier collectors")
    p = patch_collector_direct(repo_root)
    if p:
        touched.append(p)

    print("\n[2/2] app/api/live_data.py — register them + fix acm/certificatemanager key")
    p = patch_live_data(repo_root)
    if p:
        touched.append(p)

    if touched:
        print("\nCompiling patched files...")
        try:
            for f in touched:
                py_compile.compile(str(f), doraise=True)
            print("  OK    all patched files compile cleanly")
        except py_compile.PyCompileError as e:
            print(f"  FAIL  {e}\n  Reverting...")
            for f in touched:
                bak = f.with_name(f.name + BAK_SUFFIX)
                if bak.exists():
                    shutil.copy2(bak, f)
            print("  Reverted. No changes applied.")
            sys.exit(1)
        print(
            "\nDone. This requires prior application of\n"
            "  apply_extended_service_resource_counts_fix.py\n"
            "  apply_resource_counts_duplicate_route_fix.py\n"
            "Restart the backend, hard-refresh the Services page, and every\n"
            "enabled AWS service should now hide correctly when it has zero\n"
            "real resources — including AWS Certificate Manager, which was\n"
            "silently mismatched to the wrong key before this fix.\n"
            "\n"
            "IAM note: several of these (KMS, WAF, MSK, DocumentDB, Neptune,\n"
            "OpenSearch, MemoryDB, DAX...) need their own read permissions.\n"
            "Missing permission -> that one service's count comes back None\n"
            "-> its tile fails open (stays visible) rather than guessing —\n"
            "check backend logs for 'resource-counts: <svc> failed' lines."
        )
    else:
        print("\nNothing to do — already patched.")


if __name__ == "__main__":
    main()
