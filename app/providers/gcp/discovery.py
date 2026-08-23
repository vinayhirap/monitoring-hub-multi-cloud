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


def discover_account_resources(account: dict, sa_key_json: str) -> dict:
    """Run discovery for a single GCP account. Returns a per-type count."""
    creds = _credentials(sa_key_json)
    project_id = account["project_id"]

    from app.db import get_connection
    conn = get_connection()
    cursor = conn.cursor()
    counts = {"compute_instance": 0, "gcs_bucket": 0, "cloudsql_instance": 0}
    try:
        counts["compute_instance"] = _discover_compute_instances(creds, project_id, account["id"], cursor)
        counts["gcs_bucket"] = _discover_gcs_buckets(creds, project_id, account["id"], cursor)
        counts["cloudsql_instance"] = _discover_cloudsql_instances(creds, project_id, account["id"], cursor)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    logger.info(f"GCP discovery for {account.get('account_name')}: {counts}")
    return counts
