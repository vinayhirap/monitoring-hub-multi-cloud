# app/collector/discovery/runner.py
"""
Runs discovery for all services across all active accounts in parallel.
Called by scheduler every 15 minutes.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.db import get_connection
from app.aws.sts import assume_role
import boto3
import json

logger = logging.getLogger(__name__)


def _get_active_accounts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, account_name, account_id, role_arn,
                   external_id, default_region
            FROM aws_accounts
            WHERE status = 'active' AND provider = 'aws'
        """)
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


def _get_session(account):
    """Return boto3 session — same-account uses default, cross-account uses STS."""
    try:
        if account.get("role_arn"):
            return assume_role(account["role_arn"], account.get("external_id"))
        return boto3.Session()
    except Exception as e:
        logger.error(f"Session failed for {account['account_name']}: {e}")
        return None


def _upsert_resource(cursor, aws_account_id, resource_type, resource_id,
                     name, tags, region):
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


# ── Per-service discovery functions ──────────────────────────

def _discover_ec2(session, account, region, cursor):
    try:
        ec2 = session.client("ec2", region_name=region)
        paginator = ec2.get_paginator("describe_instances")
        count = 0
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    iid  = inst["InstanceId"]
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    name = tags.get("Name", iid)
                    state = inst["State"]["Name"]

                    _upsert_resource(cursor, account["id"], "ec2", iid, name, tags, region)

                    # Update instance state
                    cursor.execute(
                        "UPDATE resources SET instance_state=%s WHERE resource_id=%s AND resource_type='ec2'",
                        (state, iid)
                    )

                    # EBS volumes
                    for mapping in inst.get("BlockDeviceMappings", []):
                        ebs = mapping.get("Ebs")
                        if ebs:
                            vol_tags = dict(tags)
                            vol_tags["parent_ec2"] = iid
                            _upsert_resource(cursor, account["id"], "ebs",
                                           ebs["VolumeId"], ebs["VolumeId"], vol_tags, region)

                    # ENI
                    for eni in inst.get("NetworkInterfaces", []):
                        eni_tags = dict(tags)
                        eni_tags["parent_ec2"] = iid
                        _upsert_resource(cursor, account["id"], "eni",
                                       eni["NetworkInterfaceId"], eni["NetworkInterfaceId"], eni_tags, region)
                    count += 1
        logger.info(f"  EC2: {count} instances in {account['account_name']} / {region}")
    except Exception as e:
        logger.error(f"  EC2 discovery failed [{account['account_name']}/{region}]: {e}")


def _discover_rds(session, account, region, cursor):
    try:
        rds = session.client("rds", region_name=region)
        count = 0
        for page in rds.get_paginator("describe_db_instances").paginate():
            for db in page.get("DBInstances", []):
                rid  = db["DBInstanceIdentifier"]
                tags = {}
                try:
                    tag_resp = rds.list_tags_for_resource(ResourceName=db["DBInstanceArn"])
                    tags = {t["Key"]: t["Value"] for t in tag_resp.get("TagList", [])}
                except Exception:
                    pass
                _upsert_resource(cursor, account["id"], "rds", rid, rid, tags, region)
                count += 1
        logger.info(f"  RDS: {count} instances in {account['account_name']} / {region}")
    except Exception as e:
        logger.error(f"  RDS discovery failed [{account['account_name']}/{region}]: {e}")


def _discover_elb(session, account, region, cursor):
    try:
        elb = session.client("elbv2", region_name=region)
        count = 0
        for page in elb.get_paginator("describe_load_balancers").paginate():
            for lb in page.get("LoadBalancers", []):
                rid  = lb["LoadBalancerArn"]
                name = lb["LoadBalancerName"]
                tags = {"type": lb.get("Type", ""), "scheme": lb.get("Scheme", "")}
                _upsert_resource(cursor, account["id"], "elb", rid, name, tags, region)
                count += 1
        logger.info(f"  ELB: {count} load balancers in {account['account_name']} / {region}")
    except Exception as e:
        logger.error(f"  ELB discovery failed [{account['account_name']}/{region}]: {e}")


def _discover_ecs(session, account, region, cursor):
    try:
        ecs = session.client("ecs", region_name=region)
        count = 0
        cluster_arns = ecs.list_clusters().get("clusterArns", [])
        if not cluster_arns:
            return
        clusters = ecs.describe_clusters(clusters=cluster_arns).get("clusters", [])
        for c in clusters:
            cname = c["clusterName"]
            tags  = {"status": c.get("status", "")}
            _upsert_resource(cursor, account["id"], "ecs", c["clusterArn"], cname, tags, region)

            # Also discover services within cluster
            svc_arns = ecs.list_services(cluster=cname).get("serviceArns", [])
            if svc_arns:
                svcs = ecs.describe_services(cluster=cname, services=svc_arns[:10]).get("services", [])
                for s in svcs:
                    svc_tags = {"cluster": cname, "status": s.get("status", "")}
                    _upsert_resource(cursor, account["id"], "ecs_service",
                                   s["serviceArn"], s["serviceName"], svc_tags, region)
            count += 1
        logger.info(f"  ECS: {count} clusters in {account['account_name']} / {region}")
    except Exception as e:
        logger.error(f"  ECS discovery failed [{account['account_name']}/{region}]: {e}")


def _discover_lambda(session, account, region, cursor):
    try:
        lmb = session.client("lambda", region_name=region)
        count = 0
        for page in lmb.get_paginator("list_functions").paginate():
            for fn in page.get("Functions", []):
                rid  = fn["FunctionArn"]
                name = fn["FunctionName"]
                tags = {"runtime": fn.get("Runtime", ""), "memory": str(fn.get("MemorySize", ""))}
                _upsert_resource(cursor, account["id"], "lambda", rid, name, tags, region)
                count += 1
        logger.info(f"  Lambda: {count} functions in {account['account_name']} / {region}")
    except Exception as e:
        logger.error(f"  Lambda discovery failed [{account['account_name']}/{region}]: {e}")


# ── Continuous metric auto-enable ───────────────────────────────
#
# Closes the "new service type shows up, nobody flips it on" gap: every
# discovery cycle (15 min), sweep for services the account has started
# using since the last cycle and turn on their default metrics — additive
# only, never touches a selection a person set manually (see
# enable_metrics_for_services in app/api/metric_catalog.py).
#
# NOTE this only keeps the *metric selection* current. The generated YACE
# config still has to be re-fetched and redeployed to the regional
# monitoring server for a brand-new namespace to actually start getting
# scraped — this app generates config, it doesn't push it (see
# generate_yace_config's docstring). That redeploy step is a real gap
# that's out of this module's scope; flagging rather than silently
# implying full automation.

def _reconcile_service_metrics(session, account, region):
    try:
        from app.aws.resource_discovery import discover_all_service_keys
        from app.api.metric_catalog import enable_metrics_for_services

        detected = discover_all_service_keys(session, region)
        result = enable_metrics_for_services(account["id"], detected, provider="aws", source="discovered")
        if result["added"]:
            logger.info(
                f"  Auto-enabled {result['added']} new metric(s) for "
                f"{account['account_name']} across services={result['services']}"
            )
    except Exception as e:
        logger.error(f"  Metric auto-enable failed [{account['account_name']}]: {e}")


# ── Per-account discovery ─────────────────────────────────────

def _discover_account(account):
    region = account.get("default_region")
    if not region:
        logger.warning(f"Skipping {account['account_name']} — no region set")
        return

    logger.info(f"Discovering account: {account['account_name']} / {region}")
    session = _get_session(account)
    if not session:
        return

    import time
    max_retries = 3
    for attempt in range(max_retries):
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            _discover_ec2(session, account, region, cursor)
            _discover_rds(session, account, region, cursor)
            _discover_elb(session, account, region, cursor)
            _discover_ecs(session, account, region, cursor)
            _discover_lambda(session, account, region, cursor)
            cursor.execute(
                "UPDATE aws_accounts SET last_discovered_at = NOW() WHERE id = %s",
                (account["id"],)
            )
            conn.commit()
            break  # success -- resource inventory succeeded; metric
            # auto-enable runs after the loop below, using its own
            # connection, so a failure there can't roll back inventory
        except Exception as e:
            conn.rollback()
            if "Deadlock" in str(e) and attempt < max_retries - 1:
                logger.warning(f"Deadlock [{account['account_name']}] retry {attempt+1}")
                time.sleep(1 + attempt)
            else:
                logger.error(f"Discovery error [{account['account_name']}]: {e}")
                break
        finally:
            cursor.close()
            conn.close()

    _reconcile_service_metrics(session, account, region)


# ── Main entry point ──────────────────────────────────────────

def run_discovery():
    accounts = _get_active_accounts()
    logger.info(f"Starting discovery for {len(accounts)} accounts")

    # Sequential discovery avoids DB deadlocks from concurrent upserts
    # Discovery runs every 15min so sequential is fine
    for acc in accounts:
        try:
            _discover_account(acc)
        except Exception as e:
            logger.error(f"Discovery failed [{acc['account_name']}]: {e}")

    logger.info("Discovery complete")