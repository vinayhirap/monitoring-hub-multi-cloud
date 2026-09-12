#!/usr/bin/env python3
"""
resource_inventory_check.py

Read-only ground truth check: for every AWS namespace flagged by
audit_all_metrics.py as "wired but not live," ask AWS directly whether any
resource of that type actually exists in the account/region. This is the
only way to tell apart:

  (a) no resource of that type exists -> "wired but not live" is correct
      and expected, nothing to fix
  (b) resource(s) DO exist -> "wired but not live" means the tier config
      was never actually deployed to the server (or the IAM role/region
      is wrong), which IS a real gap to fix

Uses only list_*/describe_* calls (read-only control plane), consistent
with the no-agent/no-SSM constraint - never touches the instances
themselves.

Usage:
    python resource_inventory_check.py [account_id]

    account_id defaults to 5 (AuroGov Mumbai). Pulls default_region from
    aws_accounts for that id and uses it for regional services; a few
    services (S3, CloudFront, Route53, IAM) are global and checked once
    regardless of region.

Uses the default boto3 credential chain (~/.aws/credentials [default],
i.e. CDAC-USER per project notes) - same as the rest of the app.
"""
import sys
import boto3
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from dotenv import load_dotenv
load_dotenv()
from app.db import get_connection


def count_or_error(fn):
    try:
        result = fn()
        return result, None
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
            return None, f"ACCESS DENIED ({code}) - IAM role likely missing this permission"
        return None, f"ClientError: {code}"
    except EndpointConnectionError as e:
        return None, f"EndpointConnectionError: {e}"
    except NoCredentialsError:
        return None, "NoCredentialsError - check ~/.aws/credentials"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def main():
    account_id = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT account_name, default_region FROM aws_accounts WHERE id = %s", (account_id,))
    acc = cur.fetchone()
    cur.close(); conn.close()
    if not acc:
        print(f"Account {account_id} not found.")
        sys.exit(1)

    region = acc["default_region"] or "us-east-1"
    print(f"Account: {acc['account_name']}  region: {region}\n")

    session = boto3.Session(region_name=region)
    global_session = boto3.Session(region_name="us-east-1")  # ACM/CloudFront/Route53/IAM endpoints

    # namespace -> (label, callable returning a count)
    checks = {
        "AWS/ApiGateway": lambda: len(session.client("apigateway").get_rest_apis()["items"]),
        "AWS/AutoScaling": lambda: len(session.client("autoscaling").describe_auto_scaling_groups()["AutoScalingGroups"]),
        "AWS/Backup": lambda: len(session.client("backup").list_backup_vaults()["BackupVaultList"]),
        "AWS/CertificateManager": lambda: len(global_session.client("acm", region_name=region).list_certificates()["CertificateSummaryList"]),
        "AWS/CloudFront": lambda: len(global_session.client("cloudfront").list_distributions().get("DistributionList", {}).get("Items", [])),
        "AWS/Cognito": lambda: len(session.client("cognito-idp").list_user_pools(MaxResults=60)["UserPools"]),
        "AWS/DAX": lambda: len(session.client("dax").describe_clusters()["Clusters"]),
        "AWS/DMS": lambda: len(session.client("dms").describe_replication_instances()["ReplicationInstances"]),
        "AWS/DocDB": lambda: len(session.client("docdb").describe_db_clusters()["DBClusters"]),
        "AWS/DX": lambda: len(session.client("directconnect").describe_connections()["connections"]),
        "AWS/DynamoDB": lambda: len(session.client("dynamodb").list_tables()["TableNames"]),
        "AWS/EFS": lambda: len(session.client("efs").describe_file_systems()["FileSystems"]),
        "AWS/EKS": lambda: len(session.client("eks").list_clusters()["clusters"]),
        "AWS/ElastiCache": lambda: len(session.client("elasticache").describe_cache_clusters()["CacheClusters"]),
        "AWS/ES": lambda: len(session.client("es").list_domain_names()["DomainNames"]),
        "AWS/Events": lambda: len(session.client("events").list_rules()["Rules"]),
        "AWS/Firehose": lambda: len(session.client("firehose").list_delivery_streams()["DeliveryStreamNames"]),
        "AWS/Kafka": lambda: len(session.client("kafka").list_clusters()["ClusterInfoList"]),
        "AWS/Kinesis": lambda: len(session.client("kinesis").list_streams()["StreamNames"]),
        "AWS/Logs": lambda: len(session.client("logs").describe_log_groups()["logGroups"]),
        "AWS/MemoryDB": lambda: len(session.client("memorydb").describe_clusters()["Clusters"]),
        "AWS/Neptune": lambda: len(session.client("neptune").describe_db_clusters()["DBClusters"]),
        "AWS/NetworkELB": lambda: len([lb for lb in session.client("elbv2").describe_load_balancers()["LoadBalancers"] if lb["Type"] == "network"]),
        "AWS/Redshift": lambda: len(session.client("redshift").describe_clusters()["Clusters"]),
        "AWS/SNS": lambda: len(session.client("sns").list_topics()["Topics"]),
        "AWS/SQS": lambda: len(session.client("sqs").list_queues().get("QueueUrls", [])),
        "AWS/States": lambda: len(session.client("stepfunctions").list_state_machines()["stateMachines"]),
        "AWS/TransitGateway": lambda: len(session.client("ec2").describe_transit_gateways()["TransitGateways"]),
        "AWS/VPN": lambda: len(session.client("ec2").describe_vpn_connections()["VpnConnections"]),
        "AWS/WAFV2": lambda: len(session.client("wafv2").list_web_acls(Scope="REGIONAL")["WebACLs"]),
        "AWS/Lambda": lambda: len(session.client("lambda").list_functions()["Functions"]),
        "AWS/RDS": lambda: len(session.client("rds").describe_db_instances()["DBInstances"]),
        "AWS/Route53": lambda: len(global_session.client("route53").list_health_checks()["HealthChecks"]),
        "AWS/ECS": lambda: len(session.client("ecs").list_clusters()["clusterArns"]),
        "AWS/S3": lambda: len(global_session.client("s3").list_buckets()["Buckets"]),
    }

    print(f"{'namespace':<26}{'resource_count':<16}notes")
    print("-" * 90)

    results = {}
    for ns, fn in checks.items():
        count, err = count_or_error(fn)
        results[ns] = (count, err)
        if err:
            print(f"{ns:<26}{'?':<16}{err}")
        else:
            print(f"{ns:<26}{count:<16}")

    zero = [ns for ns, (c, e) in results.items() if c == 0]
    nonzero = [ns for ns, (c, e) in results.items() if c and c > 0]
    errored = [ns for ns, (c, e) in results.items() if e]

    print("\n=== SUMMARY ===")
    print(f"Namespaces with 0 resources (no-live-data is CORRECT/expected): {len(zero)}")
    print(f"  {', '.join(zero)}")
    print(f"\nNamespaces WITH resources but showing no live data - REAL gap, "
          f"config likely never deployed to server ({len(nonzero)}):")
    print(f"  {', '.join(nonzero)}")
    if errored:
        print(f"\nNamespaces that couldn't be checked (permission/API issue) ({len(errored)}):")
        print(f"  {', '.join(errored)}")


if __name__ == "__main__":
    main()
