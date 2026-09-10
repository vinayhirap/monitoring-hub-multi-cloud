# app/collector/discovery/extended.py
"""
Resource discovery for AWS's extended-tier services (see Section 3 of
the Sep 6-10 2026 handover: "AWS's ~33 extended-tier services have zero
discovery code at all"). This closes that gap.

IMPORTANT CONTEXT -- what already existed vs. what this file adds:
  app/aws/resource_discovery.py already detects WHICH of these 33
  service keys an account uses (via a Resource Groups Tagging API
  sweep) -- but only to auto-enable checkboxes in Settings -> Metrics
  to Monitor. It does NOT create per-resource rows, and a tagging-API
  sweep can't tell you a resource's actual CloudWatch dimension value
  (e.g. it sees a DynamoDB ARN, not the exact TableName string
  GetMetricData needs). This file is the missing layer: real
  describe/list API calls per service, writing one `resources` row per
  actual resource with the resource_id set to whatever value that
  service's CloudWatch dimension actually needs -- the same job
  _discover_ec2/_discover_rds/etc. already do for the 5 core services,
  extended to the remaining 33 (NLB excluded -- already covered via the
  existing ELB ARN-pattern split).

CONFIDENCE LEVELS -- read before trusting this in production:
  Every dimension name below was checked against AWS's published
  CloudWatch metrics/dimensions documentation, not guessed -- but NONE
  of it has been exercised against a live AWS account from this
  environment (no credentials available in this sandbox). This matches
  the exact caution the handover itself calls out for GCP's extended
  tier: "needs the same research rigor... confirming exact schemas
  against docs before writing anything" -- done here for AWS's version
  of the same gap, but real-account verification is still required
  before trusting any single one of these end to end. Two confidence
  bands:

  HIGH  -- single resource, single simple CloudWatch dimension, in the
          same shape as this app's existing core discoverers
          (DynamoDB, SQS, SNS, Kinesis, Firehose, Auto Scaling, EFS,
          Redshift, MemoryDB, DAX, Step Functions, EventBridge rules,
          KMS, ACM, Backup, Cognito, CloudWatch Logs, DMS, Direct
          Connect, NAT Gateway, ElastiCache, EKS).

  NEEDS EXTRA CARE -- multi-dimension or otherwise unusual CloudWatch
          requirements, flagged individually below:
            - cloudfront:    requires BOTH DistributionId AND a literal
                             Region="Global" dimension; must query the
                             CloudWatch API in us-east-1 regardless of
                             the account's default region.
            - opensearch:    requires BOTH ClientId (=AWS account ID)
                             AND DomainName dimensions together.
            - wafv2:         requires BOTH WebACL (name) AND Region
                             dimensions together; REGIONAL scope only
                             here -- CLOUDFRONT-scope WebACLs (which
                             live in us-east-1 regardless of account
                             region) are NOT covered by this first pass.
            - msk:           AWS's own dimension name has a literal
                             space in it: "Cluster Name" -- not a typo.
                             Cluster-level metrics only; per-broker
                             dimension ("Broker ID") not covered here.
            - transitgateway: dimension name is "TransitGateway"
                             (value = the tgw-xxxx ID), not
                             "TransitGatewayId".
            - vpn:           dimension name is "VpnId". Per-tunnel
                             metrics (dimension "TunnelIpAddress") need
                             a separate describe_vpn_connections() dig
                             into TunnelOptions and are NOT covered
                             here -- connection-level only.
            - globalaccelerator: control-plane API and CloudWatch
                             metrics both only exist in us-west-2,
                             regardless of the account's normal region.
            - route53:       only health checks are covered (the only
                             Route 53 resource type CloudWatch actually
                             integrates with directly); hosted-zone
                             query volume needs query logging, which is
                             a different, opt-in AWS feature entirely.

TESTED: importability, dimension-builder pure-function logic (given
synthetic resource rows), and metric-definition generation from the
existing CURATED catalog were all verified offline (no AWS credentials
needed for those checks) -- see apply_add_extended_service_discovery.py's
self-test. Actual discovery against a real account (correct API calls,
correct pagination, correct IAM permissions) has NOT been verified and
must be checked against a real account before relying on it.
"""
import json
import logging

logger = logging.getLogger(__name__)


def _upsert_resource(cursor, aws_account_id, resource_type, resource_id,
                      name, tags, region):
    """Same contract as discovery/runner.py's _upsert_resource -- kept
    as a local copy (not imported) so this module has no import-order
    dependency on runner.py, and can be safely imported from ADD
    inside runner.py without a circular-import risk."""
    cursor.execute("""
        INSERT INTO resources
            (aws_account_id, resource_type, resource_id, name, tags, region)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            name   = VALUES(name),
            tags   = VALUES(tags),
            region = VALUES(region)
    """, (
        aws_account_id, resource_type, resource_id,
        name, json.dumps(tags), region
    ))


def _safe(fn):
    """Wrap a per-service discoverer so one service's API/permission
    failure (e.g. account has no MSK access) never blocks the rest --
    matches the existing try/except-per-service pattern in
    discovery/runner.py's _discover_ec2 etc."""
    def wrapped(session, account, region, cursor):
        try:
            fn(session, account, region, cursor)
        except Exception as e:
            logger.warning(f"  [{fn.__name__}] discovery failed "
                            f"[{account['account_name']}/{region}]: {e}")
    wrapped.__name__ = fn.__name__
    return wrapped


# ── HIGH confidence: single resource, single simple dimension ───────

@_safe
def _discover_dynamodb(session, account, region, cursor):
    ddb = session.client("dynamodb", region_name=region)
    count = 0
    paginator = ddb.get_paginator("list_tables")
    for page in paginator.paginate():
        for table_name in page.get("TableNames", []):
            _upsert_resource(cursor, account["id"], "dynamodb", table_name,
                              table_name, {}, region)
            count += 1
    logger.info(f"  DynamoDB: {count} tables in {account['account_name']} / {region}")


@_safe
def _discover_sqs(session, account, region, cursor):
    sqs = session.client("sqs", region_name=region)
    count = 0
    paginator = sqs.get_paginator("list_queues")
    for page in paginator.paginate():
        for url in page.get("QueueUrls", []):
            queue_name = url.rstrip("/").rsplit("/", 1)[-1]
            _upsert_resource(cursor, account["id"], "sqs", queue_name,
                              queue_name, {"queue_url": url}, region)
            count += 1
    logger.info(f"  SQS: {count} queues in {account['account_name']} / {region}")


@_safe
def _discover_sns(session, account, region, cursor):
    sns = session.client("sns", region_name=region)
    count = 0
    paginator = sns.get_paginator("list_topics")
    for page in paginator.paginate():
        for t in page.get("Topics", []):
            arn = t["TopicArn"]
            topic_name = arn.rsplit(":", 1)[-1]
            _upsert_resource(cursor, account["id"], "sns", topic_name,
                              topic_name, {"arn": arn}, region)
            count += 1
    logger.info(f"  SNS: {count} topics in {account['account_name']} / {region}")


@_safe
def _discover_kinesis(session, account, region, cursor):
    kin = session.client("kinesis", region_name=region)
    count = 0
    paginator = kin.get_paginator("list_streams")
    for page in paginator.paginate():
        for stream_name in page.get("StreamNames", []):
            _upsert_resource(cursor, account["id"], "kinesis", stream_name,
                              stream_name, {}, region)
            count += 1
    logger.info(f"  Kinesis: {count} streams in {account['account_name']} / {region}")


@_safe
def _discover_firehose(session, account, region, cursor):
    fh = session.client("firehose", region_name=region)
    count = 0
    resp = fh.list_delivery_streams()
    names = resp.get("DeliveryStreamNames", [])
    while True:
        for name in names:
            _upsert_resource(cursor, account["id"], "firehose", name, name, {}, region)
            count += 1
        if not resp.get("HasMoreDeliveryStreams") or not names:
            break
        resp = fh.list_delivery_streams(ExclusiveStartDeliveryStreamName=names[-1])
        names = resp.get("DeliveryStreamNames", [])
    logger.info(f"  Firehose: {count} delivery streams in {account['account_name']} / {region}")


@_safe
def _discover_autoscaling(session, account, region, cursor):
    asg = session.client("autoscaling", region_name=region)
    count = 0
    paginator = asg.get_paginator("describe_auto_scaling_groups")
    for page in paginator.paginate():
        for g in page.get("AutoScalingGroups", []):
            gname = g["AutoScalingGroupName"]
            tags = {t["Key"]: t["Value"] for t in g.get("Tags", []) if t.get("Key")}
            _upsert_resource(cursor, account["id"], "autoscaling", gname,
                              gname, tags, region)
            count += 1
    logger.info(f"  Auto Scaling: {count} groups in {account['account_name']} / {region}")


@_safe
def _discover_natgateway(session, account, region, cursor):
    ec2 = session.client("ec2", region_name=region)
    count = 0
    paginator = ec2.get_paginator("describe_nat_gateways")
    for page in paginator.paginate(Filter=[{"Name": "state", "Values": ["available"]}]):
        for gw in page.get("NatGateways", []):
            gw_id = gw["NatGatewayId"]
            tags = {t["Key"]: t["Value"] for t in gw.get("Tags", []) if t.get("Key")}
            _upsert_resource(cursor, account["id"], "natgateway", gw_id,
                              tags.get("Name", gw_id), tags, region)
            count += 1
    logger.info(f"  NAT Gateway: {count} gateways in {account['account_name']} / {region}")


@_safe
def _discover_efs(session, account, region, cursor):
    efs = session.client("efs", region_name=region)
    count = 0
    paginator = efs.get_paginator("describe_file_systems")
    for page in paginator.paginate():
        for fs in page.get("FileSystems", []):
            fsid = fs["FileSystemId"]
            tags = {t["Key"]: t["Value"] for t in fs.get("Tags", []) if t.get("Key")}
            name = tags.get("Name", fsid)
            _upsert_resource(cursor, account["id"], "efs", fsid, name, tags, region)
            count += 1
    logger.info(f"  EFS: {count} file systems in {account['account_name']} / {region}")


@_safe
def _discover_elasticache(session, account, region, cursor):
    ec = session.client("elasticache", region_name=region)
    count = 0
    paginator = ec.get_paginator("describe_cache_clusters")
    for page in paginator.paginate():
        for c in page.get("CacheClusters", []):
            cid = c["CacheClusterId"]
            _upsert_resource(cursor, account["id"], "elasticache", cid, cid, {}, region)
            count += 1
    logger.info(f"  ElastiCache: {count} clusters in {account['account_name']} / {region}")


@_safe
def _discover_redshift(session, account, region, cursor):
    rs = session.client("redshift", region_name=region)
    count = 0
    paginator = rs.get_paginator("describe_clusters")
    for page in paginator.paginate():
        for c in page.get("Clusters", []):
            cid = c["ClusterIdentifier"]
            _upsert_resource(cursor, account["id"], "redshift", cid, cid, {}, region)
            count += 1
    logger.info(f"  Redshift: {count} clusters in {account['account_name']} / {region}")


@_safe
def _discover_memorydb(session, account, region, cursor):
    mdb = session.client("memorydb", region_name=region)
    count = 0
    paginator = mdb.get_paginator("describe_clusters")
    for page in paginator.paginate():
        for c in page.get("Clusters", []):
            name = c["Name"]
            _upsert_resource(cursor, account["id"], "memorydb", name, name, {}, region)
            count += 1
    logger.info(f"  MemoryDB: {count} clusters in {account['account_name']} / {region}")


@_safe
def _discover_dax(session, account, region, cursor):
    dax = session.client("dax", region_name=region)
    count = 0
    paginator = dax.get_paginator("describe_clusters")
    for page in paginator.paginate():
        for c in page.get("Clusters", []):
            name = c["ClusterName"]
            # AWS/DAX's own dimension is literally called "ClusterId"
            # but its VALUE is the cluster NAME, not a separate numeric
            # ID -- confirmed against AWS's DAX metrics/dimensions docs.
            _upsert_resource(cursor, account["id"], "dax", name, name, {}, region)
            count += 1
    logger.info(f"  DAX: {count} clusters in {account['account_name']} / {region}")


@_safe
def _discover_states(session, account, region, cursor):
    sfn = session.client("stepfunctions", region_name=region)
    count = 0
    paginator = sfn.get_paginator("list_state_machines")
    for page in paginator.paginate():
        for m in page.get("stateMachines", []):
            arn = m["stateMachineArn"]
            name = m["name"]
            _upsert_resource(cursor, account["id"], "states", arn, name, {}, region)
            count += 1
    logger.info(f"  Step Functions: {count} state machines in {account['account_name']} / {region}")


@_safe
def _discover_events(session, account, region, cursor):
    eb = session.client("events", region_name=region)
    count = 0
    paginator = eb.get_paginator("list_rules")
    for page in paginator.paginate():
        for r in page.get("Rules", []):
            name = r["Name"]
            _upsert_resource(cursor, account["id"], "events", name, name, {}, region)
            count += 1
    logger.info(f"  EventBridge: {count} rules in {account['account_name']} / {region}")


@_safe
def _discover_kms(session, account, region, cursor):
    kms = session.client("kms", region_name=region)
    count = 0
    paginator = kms.get_paginator("list_keys")
    for page in paginator.paginate():
        for k in page.get("Keys", []):
            key_id = k["KeyId"]
            _upsert_resource(cursor, account["id"], "kms", key_id, key_id, {}, region)
            count += 1
    logger.info(f"  KMS: {count} keys in {account['account_name']} / {region}")


@_safe
def _discover_certificatemanager(session, account, region, cursor):
    acm = session.client("acm", region_name=region)
    count = 0
    paginator = acm.get_paginator("list_certificates")
    for page in paginator.paginate():
        for c in page.get("CertificateSummaryList", []):
            arn = c["CertificateArn"]
            name = c.get("DomainName", arn)
            _upsert_resource(cursor, account["id"], "certificatemanager", arn,
                              name, {}, region)
            count += 1
    logger.info(f"  ACM: {count} certificates in {account['account_name']} / {region}")


@_safe
def _discover_backup(session, account, region, cursor):
    bk = session.client("backup", region_name=region)
    count = 0
    paginator = bk.get_paginator("list_backup_vaults")
    for page in paginator.paginate():
        for v in page.get("BackupVaultList", []):
            name = v["BackupVaultName"]
            _upsert_resource(cursor, account["id"], "backup", name, name, {}, region)
            count += 1
    logger.info(f"  Backup: {count} vaults in {account['account_name']} / {region}")


@_safe
def _discover_cognito(session, account, region, cursor):
    cog = session.client("cognito-idp", region_name=region)
    count = 0
    paginator = cog.get_paginator("list_user_pools")
    for page in paginator.paginate(PaginationConfig={"PageSize": 60}):
        for p in page.get("UserPools", []):
            pool_id = p["Id"]
            name = p.get("Name", pool_id)
            _upsert_resource(cursor, account["id"], "cognito", pool_id, name, {}, region)
            count += 1
    logger.info(f"  Cognito: {count} user pools in {account['account_name']} / {region}")


@_safe
def _discover_logs(session, account, region, cursor):
    logs = session.client("logs", region_name=region)
    count = 0
    paginator = logs.get_paginator("describe_log_groups")
    for page in paginator.paginate():
        for g in page.get("logGroups", []):
            name = g["logGroupName"]
            _upsert_resource(cursor, account["id"], "logs", name, name, {}, region)
            count += 1
    logger.info(f"  CloudWatch Logs: {count} log groups in {account['account_name']} / {region}")


@_safe
def _discover_dms(session, account, region, cursor):
    dms = session.client("dms", region_name=region)
    count = 0
    paginator = dms.get_paginator("describe_replication_instances")
    for page in paginator.paginate():
        for ri in page.get("ReplicationInstances", []):
            rid = ri["ReplicationInstanceIdentifier"]
            _upsert_resource(cursor, account["id"], "dms", rid, rid, {}, region)
            count += 1
    logger.info(f"  DMS: {count} replication instances in {account['account_name']} / {region}")


@_safe
def _discover_directconnect(session, account, region, cursor):
    dx = session.client("directconnect", region_name=region)
    count = 0
    resp = dx.describe_connections()
    for c in resp.get("connections", []):
        cid = c["connectionId"]
        name = c.get("connectionName", cid)
        _upsert_resource(cursor, account["id"], "directconnect", cid, name, {}, region)
        count += 1
    logger.info(f"  Direct Connect: {count} connections in {account['account_name']} / {region}")


@_safe
def _discover_eks(session, account, region, cursor):
    eks = session.client("eks", region_name=region)
    count = 0
    paginator = eks.get_paginator("list_clusters")
    for page in paginator.paginate():
        for name in page.get("clusters", []):
            _upsert_resource(cursor, account["id"], "eks", name, name, {}, region)
            count += 1
    logger.info(f"  EKS: {count} clusters in {account['account_name']} / {region}")


@_safe
def _discover_documentdb(session, account, region, cursor):
    # DocumentDB shares the RDS-shaped API surface but under its own
    # client/engine namespace -- boto3 service name is "docdb".
    docdb = session.client("docdb", region_name=region)
    count = 0
    paginator = docdb.get_paginator("describe_db_clusters")
    for page in paginator.paginate():
        for c in page.get("DBClusters", []):
            cid = c["DBClusterIdentifier"]
            _upsert_resource(cursor, account["id"], "documentdb", cid, cid, {}, region)
            count += 1
    logger.info(f"  DocumentDB: {count} clusters in {account['account_name']} / {region}")


@_safe
def _discover_neptune(session, account, region, cursor):
    neptune = session.client("neptune", region_name=region)
    count = 0
    paginator = neptune.get_paginator("describe_db_clusters")
    for page in paginator.paginate():
        for c in page.get("DBClusters", []):
            cid = c["DBClusterIdentifier"]
            _upsert_resource(cursor, account["id"], "neptune", cid, cid, {}, region)
            count += 1
    logger.info(f"  Neptune: {count} clusters in {account['account_name']} / {region}")


@_safe
def _discover_apigateway(session, account, region, cursor):
    # REST APIs (v1) only, matching this app's curated AWS/ApiGateway
    # entry -- HTTP APIs (v2, apigatewayv2) publish under the same
    # namespace but a different dimension shape and aren't covered here.
    apigw = session.client("apigateway", region_name=region)
    count = 0
    paginator = apigw.get_paginator("get_rest_apis")
    for page in paginator.paginate():
        for api in page.get("items", []):
            name = api["name"]
            api_id = api["id"]
            # AWS/ApiGateway's dimension is the API's NAME, not its ID --
            # store the name as resource_id since that's what
            # GetMetricData actually needs, keep the real id in tags.
            _upsert_resource(cursor, account["id"], "apigateway", name, name,
                              {"api_id": api_id}, region)
            count += 1
    logger.info(f"  API Gateway: {count} REST APIs in {account['account_name']} / {region}")


@_safe
def _discover_route53(session, account, region, cursor):
    # Route 53 itself is a global service, but health checks (the only
    # Route53 resource CloudWatch integrates with directly) are listed
    # from any region's endpoint -- no region pinning needed here.
    r53 = session.client("route53", region_name=region)
    count = 0
    paginator = r53.get_paginator("list_health_checks")
    for page in paginator.paginate():
        for hc in page.get("HealthChecks", []):
            hc_id = hc["Id"]
            _upsert_resource(cursor, account["id"], "route53", hc_id, hc_id,
                              {}, region)
            count += 1
    logger.info(f"  Route 53: {count} health checks in {account['account_name']} / {region}")


# ── NEEDS EXTRA CARE: multi-dimension / region-pinned services ──────

@_safe
def _discover_cloudfront(session, account, region, cursor):
    # Global service -- must call the CloudFront API in us-east-1
    # regardless of the account's default region, and CloudWatch
    # metrics for it require BOTH DistributionId AND a literal
    # Region="Global" dimension together (confirmed against AWS's
    # CloudFront CloudWatch docs) -- stashed in tags for the collector.
    cf = session.client("cloudfront", region_name="us-east-1")
    count = 0
    paginator = cf.get_paginator("list_distributions")
    for page in paginator.paginate():
        items = (page.get("DistributionList") or {}).get("Items") or []
        for d in items:
            dist_id = d["Id"]
            name = d.get("DomainName", dist_id)
            _upsert_resource(cursor, account["id"], "cloudfront", dist_id, name,
                              {"cw_extra_dims": {"Region": "Global"}}, "global")
            count += 1
    logger.info(f"  CloudFront: {count} distributions in {account['account_name']}")


@_safe
def _discover_opensearch(session, account, region, cursor):
    # AWS/ES CloudWatch metrics require BOTH ClientId (= AWS account ID)
    # AND DomainName dimensions together -- stashed in tags.
    aos = session.client("opensearch", region_name=region)
    count = 0
    resp = aos.list_domain_names()
    account_id_str = account.get("account_id") or ""
    for d in resp.get("DomainNames", []):
        name = d["DomainName"]
        _upsert_resource(cursor, account["id"], "opensearch", name, name,
                          {"cw_extra_dims": {"ClientId": account_id_str}}, region)
        count += 1
    logger.info(f"  OpenSearch: {count} domains in {account['account_name']} / {region}")


@_safe
def _discover_wafv2(session, account, region, cursor):
    # REGIONAL scope only -- CLOUDFRONT-scope WebACLs live in us-east-1
    # regardless of account region and are out of scope for this first
    # pass (flagged in the module docstring). CloudWatch requires BOTH
    # WebACL (name) AND Region dimensions together.
    waf = session.client("wafv2", region_name=region)
    count = 0
    resp = waf.list_web_acls(Scope="REGIONAL")
    for acl in resp.get("WebACLs", []):
        name = acl["Name"]
        acl_id = acl["Id"]
        _upsert_resource(cursor, account["id"], "wafv2", name, name,
                          {"acl_id": acl_id, "cw_extra_dims": {"Region": region}}, region)
        count += 1
    logger.info(f"  WAFv2 (REGIONAL): {count} web ACLs in {account['account_name']} / {region}")


@_safe
def _discover_msk(session, account, region, cursor):
    # AWS's own CloudWatch dimension name for MSK is literally
    # "Cluster Name" (with a space) -- not a typo, confirmed against
    # AWS's MSK monitoring docs. Cluster-level metrics only here;
    # per-broker ("Broker ID" dimension) metrics are not covered.
    kafka = session.client("kafka", region_name=region)
    count = 0
    paginator = kafka.get_paginator("list_clusters_v2")
    for page in paginator.paginate():
        for c in page.get("ClusterInfoList", []):
            name = c["ClusterName"]
            arn = c.get("ClusterArn", name)
            _upsert_resource(cursor, account["id"], "msk", name, name,
                              {"arn": arn}, region)
            count += 1
    logger.info(f"  MSK: {count} clusters in {account['account_name']} / {region}")


@_safe
def _discover_transitgateway(session, account, region, cursor):
    # Dimension name is "TransitGateway" (value = tgw-xxxx), not
    # "TransitGatewayId" -- confirmed against AWS's Transit Gateway
    # CloudWatch docs.
    ec2 = session.client("ec2", region_name=region)
    count = 0
    paginator = ec2.get_paginator("describe_transit_gateways")
    for page in paginator.paginate(Filters=[{"Name": "state", "Values": ["available"]}]):
        for tgw in page.get("TransitGateways", []):
            tgw_id = tgw["TransitGatewayId"]
            tags = {t["Key"]: t["Value"] for t in tgw.get("Tags", []) if t.get("Key")}
            _upsert_resource(cursor, account["id"], "transitgateway", tgw_id,
                              tags.get("Name", tgw_id), tags, region)
            count += 1
    logger.info(f"  Transit Gateway: {count} gateways in {account['account_name']} / {region}")


@_safe
def _discover_vpn(session, account, region, cursor):
    # Connection-level only (dimension "VpnId"). Per-tunnel metrics
    # (dimension "TunnelIpAddress") would need a separate dig into each
    # connection's TunnelOptions and are not covered in this first pass.
    ec2 = session.client("ec2", region_name=region)
    count = 0
    resp = ec2.describe_vpn_connections(
        Filters=[{"Name": "state", "Values": ["available"]}]
    )
    for vpn in resp.get("VpnConnections", []):
        vpn_id = vpn["VpnConnectionId"]
        tags = {t["Key"]: t["Value"] for t in vpn.get("Tags", []) if t.get("Key")}
        _upsert_resource(cursor, account["id"], "vpn", vpn_id,
                          tags.get("Name", vpn_id), tags, region)
        count += 1
    logger.info(f"  VPN: {count} connections in {account['account_name']} / {region}")


@_safe
def _discover_globalaccelerator(session, account, region, cursor):
    # Global Accelerator's control-plane API AND its CloudWatch metrics
    # both live only in us-west-2, regardless of the account's normal
    # region -- confirmed against AWS's Global Accelerator docs.
    ga = session.client("globalaccelerator", region_name="us-west-2")
    count = 0
    paginator = ga.get_paginator("list_accelerators")
    for page in paginator.paginate():
        for a in page.get("Accelerators", []):
            arn = a["AcceleratorArn"]
            name = a.get("Name", arn)
            _upsert_resource(cursor, account["id"], "globalaccelerator", arn,
                              name, {}, "us-west-2")
            count += 1
    logger.info(f"  Global Accelerator: {count} accelerators in {account['account_name']}")


# ── Dispatch ──────────────────────────────────────────────────

EXTENDED_DISCOVERERS = {
    "dynamodb":           _discover_dynamodb,
    "sqs":                _discover_sqs,
    "sns":                _discover_sns,
    "kinesis":            _discover_kinesis,
    "firehose":           _discover_firehose,
    "autoscaling":        _discover_autoscaling,
    "natgateway":         _discover_natgateway,
    "efs":                _discover_efs,
    "elasticache":        _discover_elasticache,
    "redshift":           _discover_redshift,
    "memorydb":           _discover_memorydb,
    "dax":                _discover_dax,
    "states":             _discover_states,
    "events":             _discover_events,
    "kms":                _discover_kms,
    "certificatemanager": _discover_certificatemanager,
    "backup":             _discover_backup,
    "cognito":            _discover_cognito,
    "logs":               _discover_logs,
    "dms":                _discover_dms,
    "directconnect":      _discover_directconnect,
    "eks":                _discover_eks,
    "documentdb":         _discover_documentdb,
    "neptune":            _discover_neptune,
    "apigateway":         _discover_apigateway,
    "route53":            _discover_route53,
    "cloudfront":         _discover_cloudfront,
    "opensearch":         _discover_opensearch,
    "wafv2":              _discover_wafv2,
    "msk":                _discover_msk,
    "transitgateway":     _discover_transitgateway,
    "vpn":                _discover_vpn,
    "globalaccelerator":  _discover_globalaccelerator,
}


def discover_extended_services(session, account, region, cursor):
    """
    Runs every extended-tier discoverer for one account/region. Each
    discoverer is individually try/except-wrapped (via @_safe) so one
    service's missing IAM permission or regional unavailability can
    never block the others -- same resilience contract as the 5 core
    discoverers in discovery/runner.py.
    """
    for service_key, fn in EXTENDED_DISCOVERERS.items():
        fn(session, account, region, cursor)
