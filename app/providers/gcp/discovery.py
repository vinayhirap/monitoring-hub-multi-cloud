# app/providers/gcp/discovery.py
"""
Real GCP resource discovery. Mirrors app.collector.discovery.runner's
contract: writes/updates rows in `resources`, scoped to a single account,
called from GCPProvider.

Compute + Storage use the google-cloud-* client libraries; Cloud SQL uses
the SQL Admin API via google-api-python-client (no dedicated google-cloud
library exists for it).
"""
import json
import logging

from google.oauth2 import service_account as gcp_service_account
from google.cloud import compute_v1
from google.cloud import storage as gcs
from google.cloud import run_v2
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


def discover_account_resources(account: dict, sa_key_json: str) -> dict:
    """Run discovery for a single GCP account. Returns a per-type count."""
    creds = _credentials(sa_key_json)
    project_id = account["project_id"]

    from app.db import get_connection
    conn = get_connection()
    cursor = conn.cursor()
    counts = {"compute_instance": 0, "gcs_bucket": 0, "cloudsql_instance": 0, "cloud_run_service": 0}
    try:
        counts["compute_instance"] = _discover_compute_instances(creds, project_id, account["id"], cursor)
        counts["gcs_bucket"] = _discover_gcs_buckets(creds, project_id, account["id"], cursor)
        counts["cloudsql_instance"] = _discover_cloudsql_instances(creds, project_id, account["id"], cursor)
        counts["cloud_run_service"] = _discover_cloud_run(creds, project_id, account["id"], cursor)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    logger.info(f"GCP discovery for {account.get('account_name')}: {counts}")
    return counts
