# app/providers/gcp/discovery.py
"""
Real GCP resource discovery. Mirrors app.collector.discovery.runner's
contract: writes/updates rows in `resources`, scoped to a single account,
called from GCPProvider.

Covers all 16 services curated in app.providers.gcp.metric_catalog_data
(4 core + 12 extended). Compute + Storage use the google-cloud-* client
libraries; Cloud SQL uses the SQL Admin API via google-api-python-client
(no dedicated google-cloud library exists for it). Every new client/method
below was verified by import + signature inspection in a sandbox (no live
GCP project was available to exercise these end-to-end) — see per-function
notes for the handful of services (BigQuery, Firestore, Spanner) that
don't fit the "list resources of type X" shape the others share.
"""
import json
import logging

from google.oauth2 import service_account as gcp_service_account
from google.cloud import compute_v1
from google.cloud import storage as gcs
from google.cloud import run_v2
from google.cloud import container_v1
from google.cloud import functions_v2
from google.cloud import pubsub_v1
from google.cloud import redis_v1
from google.cloud import bigquery
from google.cloud import firestore_admin_v1
from google.cloud.spanner_admin_instance_v1 import InstanceAdminClient as SpannerInstanceAdminClient
from googleapiclient.discovery import build as gapi_build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/cloud-platform.read-only"]


def _credentials(sa_key_json: str):
    info = json.loads(sa_key_json)
    return gcp_service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def _upsert_resource(cursor, account_id, resource_type, resource_id, name, tags, region,
                      normalized_resource_type):
    cursor.execute("""
        INSERT INTO resources
            (aws_account_id, resource_type, resource_id, name, tags, region, normalized_resource_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            name = VALUES(name),
            tags = VALUES(tags),
            region = VALUES(region),
            normalized_resource_type = VALUES(normalized_resource_type)
    """, (account_id, resource_type, resource_id, name, json.dumps(tags or {}), region,
          normalized_resource_type))


def _discover_compute_instances(creds, project_id, account_id, cursor) -> int:
    client = compute_v1.InstancesClient(credentials=creds)
    count = 0
    for zone, response in client.aggregated_list(project=project_id):
        if not response.instances:
            continue
        zone_name = zone.split("/")[-1]
        for inst in response.instances:
            labels = dict(inst.labels or {})
            resource_id = f"projects/{project_id}/zones/{zone_name}/instances/{inst.name}"
            _upsert_resource(
                cursor, account_id, "compute_instance", resource_id, inst.name,
                labels, zone_name, "compute",
            )
            count += 1
    return count


def _discover_gcs_buckets(creds, project_id, account_id, cursor) -> int:
    client = gcs.Client(project=project_id, credentials=creds)
    count = 0
    for bucket in client.list_buckets():
        resource_id = f"projects/{project_id}/buckets/{bucket.name}"
        _upsert_resource(
            cursor, account_id, "gcs_bucket", resource_id, bucket.name,
            dict(bucket.labels or {}), bucket.location or "", "storage",
        )
        count += 1
    return count


def _discover_cloudsql_instances(creds, project_id, account_id, cursor) -> int:
    service = gapi_build("sqladmin", "v1beta4", credentials=creds, cache_discovery=False)
    count = 0
    req = service.instances().list(project=project_id)
    while req is not None:
        resp = req.execute()
        for inst in resp.get("items", []):
            resource_id = f"projects/{project_id}/instances/{inst['name']}"
            _upsert_resource(
                cursor, account_id, "cloudsql_instance", resource_id, inst["name"],
                dict(inst.get("settings", {}).get("userLabels", {}) or {}),
                inst.get("region", ""), "database",
            )
            count += 1
        req = service.instances().list_next(previous_request=req, previous_response=resp)
    return count


# Cloud Run is regional with no "list across all regions" call, so we probe
# the same set of regions offered in the onboarding UI's GCP region picker.
# A region with no Cloud Run services (or where the API isn't enabled)
# raises here — caught and skipped per-region rather than failing the
# whole discovery run.
CLOUD_RUN_REGIONS = [
    "asia-south1", "asia-south2", "asia-southeast1", "asia-east1", "asia-northeast1",
    "australia-southeast1", "us-central1", "us-east1", "us-west1",
    "europe-west1", "europe-west2", "europe-central2",
]


def _discover_cloud_run(creds, project_id, account_id, cursor) -> int:
    client = run_v2.ServicesClient(credentials=creds)
    count = 0
    for region in CLOUD_RUN_REGIONS:
        parent = f"projects/{project_id}/locations/{region}"
        try:
            for svc in client.list_services(parent=parent):
                name = svc.name.split("/")[-1]
                resource_id = svc.name  # projects/{p}/locations/{r}/services/{name}
                _upsert_resource(
                    cursor, account_id, "cloud_run_service", resource_id, name,
                    dict(svc.labels or {}), region, "compute",
                )
                count += 1
        except Exception as e:
            logger.debug(f"Cloud Run discovery skipped for {project_id}/{region}: {e}")
    return count


def _discover_gke_clusters(creds, project_id, account_id, cursor) -> int:
    client = container_v1.ClusterManagerClient(credentials=creds)
    count = 0
    resp = client.list_clusters(parent=f"projects/{project_id}/locations/-")
    for cluster in resp.clusters:
        location = cluster.location or cluster.zone
        resource_id = f"projects/{project_id}/locations/{location}/clusters/{cluster.name}"
        _upsert_resource(
            cursor, account_id, "gke_cluster", resource_id, cluster.name,
            dict(cluster.resource_labels or {}), location, "compute",
        )
        count += 1
    return count


def _discover_gke_nodes(creds, project_id, account_id, cursor) -> int:
    # The Cluster Manager API doesn't expose individual nodes directly —
    # node counts live per node pool (cluster.node_pools[].initial_node_count
    # / cluster.node_pools[].autoscaling). Register one resource per node
    # pool (matching the "GKE Nodes" curated service, whose metrics are
    # pool-scoped anyway), rather than trying to enumerate raw Compute
    # Engine instances a second time (already covered by compute_instance).
    client = container_v1.ClusterManagerClient(credentials=creds)
    count = 0
    resp = client.list_clusters(parent=f"projects/{project_id}/locations/-")
    for cluster in resp.clusters:
        location = cluster.location or cluster.zone
        for pool in cluster.node_pools:
            resource_id = (
                f"projects/{project_id}/locations/{location}/clusters/"
                f"{cluster.name}/nodePools/{pool.name}"
            )
            _upsert_resource(
                cursor, account_id, "gke_node", resource_id, f"{cluster.name}/{pool.name}",
                {}, location, "compute",
            )
            count += 1
    return count


def _discover_cloudfunctions(creds, project_id, account_id, cursor) -> int:
    client = functions_v2.FunctionServiceClient(credentials=creds)
    count = 0
    for fn in client.list_functions(parent=f"projects/{project_id}/locations/-"):
        name = fn.name.split("/")[-1]
        # location is the 4th path segment: projects/{p}/locations/{loc}/functions/{name}
        parts = fn.name.split("/")
        location = parts[3] if len(parts) > 3 else ""
        _upsert_resource(
            cursor, account_id, "cloudfunctions_function", fn.name, name,
            dict(fn.labels or {}), location, "compute",
        )
        count += 1
    return count


def _discover_pubsub_topics(creds, project_id, account_id, cursor) -> int:
    client = pubsub_v1.PublisherClient(credentials=creds)
    count = 0
    for topic in client.list_topics(project=f"projects/{project_id}"):
        name = topic.name.split("/")[-1]
        _upsert_resource(
            cursor, account_id, "pubsub_topic", topic.name, name,
            dict(topic.labels or {}), "global", "messaging",
        )
        count += 1
    return count


def _discover_pubsub_subscriptions(creds, project_id, account_id, cursor) -> int:
    client = pubsub_v1.SubscriberClient(credentials=creds)
    count = 0
    for sub in client.list_subscriptions(project=f"projects/{project_id}"):
        name = sub.name.split("/")[-1]
        _upsert_resource(
            cursor, account_id, "pubsub_subscription", sub.name, name,
            dict(sub.labels or {}), "global", "messaging",
        )
        count += 1
    return count


def _discover_cloud_lb(creds, project_id, account_id, cursor) -> int:
    # "Cloud Load Balancing" spans several underlying resource kinds
    # (global HTTP(S) LB, regional internal/network LB). Forwarding rules
    # are the one object type that exists for every LB flavor — a global
    # forwarding rule for global LBs, a regional one for regional LBs —
    # so counting both gives an accurate total without double-counting
    # backend services, which can be shared across multiple LBs.
    count = 0
    global_client = compute_v1.GlobalForwardingRulesClient(credentials=creds)
    for rule in global_client.list(project=project_id):
        resource_id = f"projects/{project_id}/global/forwardingRules/{rule.name}"
        _upsert_resource(
            cursor, account_id, "cloud_lb", resource_id, rule.name,
            dict(rule.labels or {}), "global", "networking",
        )
        count += 1

    regional_client = compute_v1.ForwardingRulesClient(credentials=creds)
    for region, response in regional_client.aggregated_list(project=project_id):
        if not response.forwarding_rules:
            continue
        region_name = region.split("/")[-1]
        for rule in response.forwarding_rules:
            resource_id = f"projects/{project_id}/regions/{region_name}/forwardingRules/{rule.name}"
            _upsert_resource(
                cursor, account_id, "cloud_lb", resource_id, rule.name,
                dict(rule.labels or {}), region_name, "networking",
            )
            count += 1
    return count


def _discover_redis_instances(creds, project_id, account_id, cursor) -> int:
    client = redis_v1.CloudRedisClient(credentials=creds)
    count = 0
    for inst in client.list_instances(parent=f"projects/{project_id}/locations/-"):
        name = inst.name.split("/")[-1]
        parts = inst.name.split("/")
        location = parts[3] if len(parts) > 3 else ""
        _upsert_resource(
            cursor, account_id, "redis_instance", inst.name, name,
            dict(inst.labels or {}), location, "database",
        )
        count += 1
    return count


def _discover_bigquery_datasets(creds, project_id, account_id, cursor) -> int:
    # BigQuery has no "instance" concept — a dataset is the closest
    # analog to a monitorable resource unit. list_datasets() also
    # confirms the API is enabled / project actually uses BigQuery at all
    # (matches the "does this account use this service" question the
    # rest of discovery is answering).
    client = bigquery.Client(project=project_id, credentials=creds)
    count = 0
    for ds in client.list_datasets():
        resource_id = f"projects/{project_id}/datasets/{ds.dataset_id}"
        _upsert_resource(
            cursor, account_id, "bigquery_project", resource_id, ds.dataset_id,
            dict(ds.labels or {}) if hasattr(ds, "labels") else {}, "global", "analytics",
        )
        count += 1
    return count


def _discover_spanner_instances(creds, project_id, account_id, cursor) -> int:
    client = SpannerInstanceAdminClient(credentials=creds)
    count = 0
    for inst in client.list_instances(parent=f"projects/{project_id}"):
        name = inst.name.split("/")[-1]
        _upsert_resource(
            cursor, account_id, "spanner_instance", inst.name, name,
            dict(inst.labels or {}), inst.config.split("/")[-1] if inst.config else "", "database",
        )
        count += 1
    return count


def _discover_firestore_databases(creds, project_id, account_id, cursor) -> int:
    client = firestore_admin_v1.FirestoreAdminClient(credentials=creds)
    count = 0
    resp = client.list_databases(parent=f"projects/{project_id}")
    for db in resp.databases:
        name = db.name.split("/")[-1]
        _upsert_resource(
            cursor, account_id, "firestore_database", db.name, name,
            {}, getattr(db, "location_id", "") or "", "database",
        )
        count += 1
    return count


def _discover_nat_gateways(creds, project_id, account_id, cursor) -> int:
    # Cloud NAT is a sub-resource of a Router, not a standalone listable
    # object — enumerate routers, then each router's `.nats` entries.
    client = compute_v1.RoutersClient(credentials=creds)
    count = 0
    for region, response in client.aggregated_list(project=project_id):
        if not response.routers:
            continue
        region_name = region.split("/")[-1]
        for router in response.routers:
            for nat in (router.nats or []):
                resource_id = (
                    f"projects/{project_id}/regions/{region_name}/routers/"
                    f"{router.name}/nats/{nat.name}"
                )
                _upsert_resource(
                    cursor, account_id, "nat_gateway", resource_id, nat.name,
                    {}, region_name, "networking",
                )
                count += 1
    return count


def _discover_persistent_disks(creds, project_id, account_id, cursor) -> int:
    client = compute_v1.DisksClient(credentials=creds)
    count = 0
    for zone, response in client.aggregated_list(project=project_id):
        if not response.disks:
            continue
        zone_name = zone.split("/")[-1]
        for disk in response.disks:
            resource_id = f"projects/{project_id}/zones/{zone_name}/disks/{disk.name}"
            _upsert_resource(
                cursor, account_id, "gce_persistent_disk", resource_id, disk.name,
                dict(disk.labels or {}), zone_name, "storage",
            )
            count += 1
    return count


def discover_account_resources(account: dict, sa_key_json: str) -> dict:
    """Run discovery for a single GCP account. Returns a per-type count."""
    creds = _credentials(sa_key_json)
    project_id = account["project_id"]

    from app.db import get_connection
    conn = get_connection()
    cursor = conn.cursor()
    counts = {
        "compute_instance": 0, "gcs_bucket": 0, "cloudsql_instance": 0, "cloud_run_service": 0,
        "gke_cluster": 0, "gke_node": 0, "cloudfunctions_function": 0, "pubsub_topic": 0,
        "pubsub_subscription": 0, "cloud_lb": 0, "redis_instance": 0, "bigquery_project": 0,
        "spanner_instance": 0, "firestore_database": 0, "nat_gateway": 0, "gce_persistent_disk": 0,
    }
    # Same fail-open pattern as Azure: a service that's never been enabled
    # for this project (API not turned on) or hits a permissions gap
    # shouldn't abort discovery for every other service.
    steps = [
        ("compute_instance",         lambda: _discover_compute_instances(creds, project_id, account["id"], cursor)),
        ("gcs_bucket",               lambda: _discover_gcs_buckets(creds, project_id, account["id"], cursor)),
        ("cloudsql_instance",        lambda: _discover_cloudsql_instances(creds, project_id, account["id"], cursor)),
        ("cloud_run_service",        lambda: _discover_cloud_run(creds, project_id, account["id"], cursor)),
        ("gke_cluster",              lambda: _discover_gke_clusters(creds, project_id, account["id"], cursor)),
        ("gke_node",                 lambda: _discover_gke_nodes(creds, project_id, account["id"], cursor)),
        ("cloudfunctions_function",  lambda: _discover_cloudfunctions(creds, project_id, account["id"], cursor)),
        ("pubsub_topic",             lambda: _discover_pubsub_topics(creds, project_id, account["id"], cursor)),
        ("pubsub_subscription",      lambda: _discover_pubsub_subscriptions(creds, project_id, account["id"], cursor)),
        ("cloud_lb",                 lambda: _discover_cloud_lb(creds, project_id, account["id"], cursor)),
        ("redis_instance",           lambda: _discover_redis_instances(creds, project_id, account["id"], cursor)),
        ("bigquery_project",         lambda: _discover_bigquery_datasets(creds, project_id, account["id"], cursor)),
        ("spanner_instance",         lambda: _discover_spanner_instances(creds, project_id, account["id"], cursor)),
        ("firestore_database",       lambda: _discover_firestore_databases(creds, project_id, account["id"], cursor)),
        ("nat_gateway",              lambda: _discover_nat_gateways(creds, project_id, account["id"], cursor)),
        ("gce_persistent_disk",      lambda: _discover_persistent_disks(creds, project_id, account["id"], cursor)),
    ]

    try:
        for key, fn in steps:
            try:
                counts[key] = fn()
            except Exception as e:
                logger.warning(f"GCP {key} discovery failed for {account.get('account_name')}: {e}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    logger.info(f"GCP discovery for {account.get('account_name')}: {counts}")
    return counts
