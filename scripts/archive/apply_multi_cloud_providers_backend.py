#!/usr/bin/env python3
"""
apply_multi_cloud_providers_backend.py

Steps 5+6 of the multi-cloud refactor plan, backend half: real Azure and
GCP CloudProvider implementations (auth, resource discovery, console
URLs, metric catalog), plus wiring accounts.py's onboarding and
"Discover Now" endpoints to dispatch through whichever provider an
account actually uses instead of assuming AWS.

NEW FILES (additive, nothing existing touched by this part):
    app/providers/azure/__init__.py
    app/providers/azure/provider.py   - real Service Principal auth
                                         (azure-identity), real discovery
                                         of VMs/Storage Accounts/SQL
                                         Servers (azure-mgmt-*), real
                                         portal.azure.com console links.
    app/providers/gcp/__init__.py
    app/providers/gcp/provider.py     - real Service Account auth,
                                         real discovery of Compute
                                         instances/Storage buckets/
                                         Cloud SQL instances
                                         (google-cloud-*), real
                                         console.cloud.google.com links.

EDITED FILES:
    app/providers/__init__.py    - also import azure/gcp packages so
                                    they register on startup.
    app/api/admin/accounts.py    - three changes, each anchor-guarded:
        1. list_accounts() SELECT now includes `provider` so the
           frontend can route each row correctly.
        2. add_account() is now provider-aware: branches on
           payload["provider"] (aws/azure/gcp), validates the
           provider-specific required fields, calls
           provider.validate_credentials() before insert (fails with
           400 + the real error if credentials don't actually work —
           no silent acceptance of bad creds), then inserts into the
           provider-specific columns migration 009/010 already added.
        3. discover_account() dispatches via the account's own
           `provider` column instead of hardcoding get_provider("aws").
    requirements.txt   - adds azure-identity, azure-mgmt-resource,
                          azure-mgmt-compute, azure-mgmt-storage,
                          azure-mgmt-sql, google-cloud-resource-manager,
                          google-cloud-compute, google-cloud-storage,
                          google-api-python-client (only appended if not
                          already present).

WHAT THIS DOES NOT DO
    Metrics collection into VictoriaMetrics for Azure/GCP resources.
    The architecture assessment (section 3) already flags this as its
    own subsystem — YACE has no Azure/GCP equivalent, so this needs a
    purpose-built collector per provider, not a config variation of the
    AWS pipeline. Discovery, console links, credential validation, and
    the metric catalog (what CAN be collected) are real and working
    after this patch; the collector that actually polls and writes
    metric values is the next, separately-scoped piece of work.

SAFETY
    New files are skipped if already present (use --force to
    overwrite). accounts.py edits are anchor-guarded (exact occurrence
    count check before writing) and backed up to
    app/api/admin/accounts.py.bak.pre-multi-cloud-providers. Verified
    with py_compile after writing (auto-reverts on syntax error).
    requirements.txt append is idempotent (skips lines already present).

Run from the project root:
    python apply_multi_cloud_providers_backend.py --dry-run
    python apply_multi_cloud_providers_backend.py
"""

import argparse
import py_compile
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()


def die(msg):
    print(f"\n[ABORTED] {msg}")
    print("No files were modified in this step (earlier steps, if any ran first, already happened).")
    sys.exit(1)


# ── New files ────────────────────────────────────────────────────────

NEW_FILES = {

    "app/providers/azure/__init__.py": '''from app.providers.registry import register
from app.providers.azure.provider import AzureProvider

register("azure", AzureProvider)
''',

    "app/providers/gcp/__init__.py": '''from app.providers.registry import register
from app.providers.gcp.provider import GCPProvider

register("gcp", GCPProvider)
''',

    "app/providers/azure/provider.py": '''"""
AzureProvider — CloudProvider implementation for Azure.

Auth: Service Principal (tenant_id, client_id, client_secret), matching
the plan's "Service Principal first — simplest, production-viable"
sequencing decision. Managed Identity is a later enhancement, not
implemented here (this app runs on-prem, not in Azure, so Managed
Identity doesn't apply to it anyway).

Discovery covers three resource types via their respective ARM SDKs:
  - Virtual Machines   (azure-mgmt-compute)
  - Storage Accounts   (azure-mgmt-storage)
  - SQL Servers        (azure-mgmt-sql)

`resource_id` stored in the `resources` table is the full ARM resource
ID (e.g. /subscriptions/xxx/resourceGroups/yyy/providers/...), not a
short name — this is what makes get_console_url a direct portal deep
link with no extra lookups, and it's globally unique (unlike short
names, which can collide across resource groups).

Metric catalog: Azure Monitor's platform-metric namespaces for these
same three resource types (Microsoft.Compute/virtualMachines,
Microsoft.Storage/storageAccounts, Microsoft.Sql/servers/databases).
This is the curated starter set, not an exhaustive catalog — same
scope decision the existing AWS CURATED catalog made.
"""
import logging

from app.providers.base import CloudProvider

logger = logging.getLogger(__name__)


class AzureProvider(CloudProvider):
    name = "azure"

    # ── auth ─────────────────────────────────────────────────────

    def _credential(self, account: dict):
        from azure.identity import ClientSecretCredential

        tenant_id = (account.get("tenant_id") or "").strip()
        client_id = (account.get("client_id") or "").strip()
        client_secret = (account.get("client_secret") or "").strip()
        if not (tenant_id and client_id and client_secret):
            raise ValueError("tenant_id, client_id and client_secret are all required")

        return ClientSecretCredential(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret,
        )

    def validate_credentials(self, account: dict) -> dict:
        from azure.mgmt.resource import SubscriptionClient

        subscription_id = (account.get("subscription_id") or "").strip()
        if not subscription_id:
            raise ValueError("subscription_id is required")

        credential = self._credential(account)
        sub_client = SubscriptionClient(credential)
        sub = sub_client.subscriptions.get(subscription_id)

        return {
            "status": "success",
            "subscription_id": sub.subscription_id,
            "subscription_name": sub.display_name,
            "state": str(sub.state),
        }

    # ── console URLs ─────────────────────────────────────────────

    def get_console_url(self, account: dict, resource_id: str, region: str,
                         service: str | None = None,
                         resource_name: str | None = None,
                         ecs_service_name: str | None = None) -> str:
        tenant_id = account.get("tenant_id") or ""
        if not resource_id:
            subscription_id = account.get("subscription_id") or ""
            return (f"https://portal.azure.com/#@{tenant_id}"
                    f"/resource/subscriptions/{subscription_id}/resourceGroups")

        # resource_id is already a full ARM resource ID
        # (/subscriptions/.../resourceGroups/.../providers/...)
        rid = resource_id if resource_id.startswith("/") else f"/{resource_id}"
        return f"https://portal.azure.com/#@{tenant_id}/resource{rid}"

    # ── discovery ────────────────────────────────────────────────

    def discover_resources(self) -> None:
        from app.db import get_connection

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM aws_accounts WHERE status = 'active' AND provider = 'azure'"
        )
        accounts = cursor.fetchall()
        cursor.close()

        for account in accounts:
            try:
                self._discover_account(conn, account)
            except Exception as e:
                logger.error(f"Azure discovery failed for {account.get('account_name')}: {e}")

        conn.close()

    def _discover_account(self, conn, account: dict):
        import json

        credential = self._credential(account)
        subscription_id = (account.get("subscription_id") or "").strip()
        cursor = conn.cursor()

        self._discover_vms(credential, subscription_id, account, cursor)
        self._discover_storage_accounts(credential, subscription_id, account, cursor)
        self._discover_sql_servers(credential, subscription_id, account, cursor)

        conn.commit()
        cursor.close()

    @staticmethod
    def _upsert(cursor, account_id, resource_type, resource_id, name, tags, region):
        import json as _json

        cursor.execute("""
            INSERT INTO resources
                (aws_account_id, resource_type, resource_id, name, tags, region)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name   = VALUES(name),
                tags   = VALUES(tags),
                region = VALUES(region)
        """, (account_id, resource_type, resource_id, name, _json.dumps(tags or {}), region))

    def _discover_vms(self, credential, subscription_id, account, cursor):
        from azure.mgmt.compute import ComputeManagementClient

        client = ComputeManagementClient(credential, subscription_id)
        count = 0
        for vm in client.virtual_machines.list_all():
            self._upsert(cursor, account["id"], "azure_vm", vm.id, vm.name,
                         vm.tags, vm.location)
            count += 1
        logger.info(f"  Azure VMs: {count} in {account['account_name']}")

    def _discover_storage_accounts(self, credential, subscription_id, account, cursor):
        from azure.mgmt.storage import StorageManagementClient

        client = StorageManagementClient(credential, subscription_id)
        count = 0
        for sa in client.storage_accounts.list():
            self._upsert(cursor, account["id"], "azure_storage", sa.id, sa.name,
                         sa.tags, sa.location)
            count += 1
        logger.info(f"  Azure Storage Accounts: {count} in {account['account_name']}")

    def _discover_sql_servers(self, credential, subscription_id, account, cursor):
        from azure.mgmt.sql import SqlManagementClient

        client = SqlManagementClient(credential, subscription_id)
        count = 0
        for server in client.servers.list():
            self._upsert(cursor, account["id"], "azure_sql", server.id, server.name,
                         server.tags, server.location)
            count += 1
        logger.info(f"  Azure SQL Servers: {count} in {account['account_name']}")

    # ── metric catalog ───────────────────────────────────────────

    def get_metric_catalog(self) -> dict:
        return {
            "azure_vm": (
                "Virtual Machines", "Microsoft.Compute/virtualMachines", "Compute",
                [
                    ("Percentage CPU", "Percent", "Average", True, "CPU utilization"),
                    ("Network In Total", "Bytes", "Total", True, "Inbound network traffic"),
                    ("Network Out Total", "Bytes", "Total", True, "Outbound network traffic"),
                    ("Disk Read Bytes", "Bytes", "Total", False, "Disk read throughput"),
                    ("Disk Write Bytes", "Bytes", "Total", False, "Disk write throughput"),
                ],
            ),
            "azure_storage": (
                "Storage Accounts", "Microsoft.Storage/storageAccounts", "Storage",
                [
                    ("UsedCapacity", "Bytes", "Average", True, "Total storage used"),
                    ("Transactions", "Count", "Total", True, "Total requests"),
                    ("Availability", "Percent", "Average", True, "Service availability"),
                ],
            ),
            "azure_sql": (
                "SQL Servers", "Microsoft.Sql/servers/databases", "Database",
                [
                    ("cpu_percent", "Percent", "Average", True, "CPU utilization"),
                    ("dtu_consumption_percent", "Percent", "Average", True, "DTU consumption"),
                    ("storage_percent", "Percent", "Average", True, "Storage utilization"),
                    ("connection_successful", "Count", "Total", False, "Successful connections"),
                ],
            ),
        }
''',
    "app/providers/gcp/provider.py": '''"""
GCPProvider — CloudProvider implementation for GCP.

Auth: Service Account key (JSON key file content stored in
aws_accounts.gcp_service_account_key), matching the plan's "Service
Account first" sequencing decision.

Discovery covers three resource types:
  - Compute Engine instances (google-cloud-compute)
  - Cloud Storage buckets    (google-cloud-storage)
  - Cloud SQL instances      (google-api-python-client, sqladmin v1beta4
                               — no first-party google-cloud-sql-admin
                               client library exists, this is GCP's own
                               documented way to reach that API)

`resource_id` stored is the resource's short name (GCP resource IDs
aren't full paths the way Azure ARM IDs are); `region` stores the zone
for compute instances (e.g. "asia-south1-a") so the console deep link
can be built without a second lookup.

No centralized-exporter equivalent to YACE exists for GCP (flagged in
the architecture assessment, section 3) — this provider covers
discovery and console links only. Metrics collection into
VictoriaMetrics needs its own poller against Cloud Monitoring's
timeSeries.list API, which is a separate piece of work, not included
here.
"""
import logging

from app.providers.base import CloudProvider

logger = logging.getLogger(__name__)


class GCPProvider(CloudProvider):
    name = "gcp"

    # ── auth ─────────────────────────────────────────────────────

    def _credentials(self, account: dict):
        import json
        from google.oauth2 import service_account

        key_json = account.get("gcp_service_account_key")
        if not key_json:
            raise ValueError("gcp_service_account_key is required")

        try:
            info = json.loads(key_json)
        except (TypeError, ValueError) as e:
            raise ValueError(f"gcp_service_account_key is not valid JSON: {e}")

        return service_account.Credentials.from_service_account_info(info)

    def validate_credentials(self, account: dict) -> dict:
        from google.cloud import resourcemanager_v3

        project_id = (account.get("project_id") or "").strip()
        if not project_id:
            raise ValueError("project_id is required")

        credentials = self._credentials(account)
        client = resourcemanager_v3.ProjectsClient(credentials=credentials)
        project = client.get_project(name=f"projects/{project_id}")

        return {
            "status": "success",
            "project_id": project_id,
            "project_display_name": project.display_name,
            "state": str(project.state),
        }

    # ── console URLs ─────────────────────────────────────────────

    def get_console_url(self, account: dict, resource_id: str, region: str,
                         service: str | None = None,
                         resource_name: str | None = None,
                         ecs_service_name: str | None = None) -> str:
        project_id = account.get("project_id") or ""
        base = "https://console.cloud.google.com"
        svc = (service or "").lower()

        if not resource_id:
            return f"{base}/home/dashboard?project={project_id}"

        if svc == "gcp_vm":
            zone = region or ""
            return f"{base}/compute/instancesDetail/zones/{zone}/instances/{resource_id}?project={project_id}"
        if svc == "gcp_storage":
            return f"{base}/storage/browser/{resource_id}?project={project_id}"
        if svc == "gcp_sql":
            return f"{base}/sql/instances/{resource_id}/overview?project={project_id}"

        return f"{base}/home/dashboard?project={project_id}"

    # ── discovery ────────────────────────────────────────────────

    def discover_resources(self) -> None:
        from app.db import get_connection

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM aws_accounts WHERE status = 'active' AND provider = 'gcp'"
        )
        accounts = cursor.fetchall()
        cursor.close()

        for account in accounts:
            try:
                self._discover_account(conn, account)
            except Exception as e:
                logger.error(f"GCP discovery failed for {account.get('account_name')}: {e}")

        conn.close()

    def _discover_account(self, conn, account: dict):
        credentials = self._credentials(account)
        project_id = (account.get("project_id") or "").strip()
        cursor = conn.cursor()

        self._discover_instances(credentials, project_id, account, cursor)
        self._discover_buckets(credentials, project_id, account, cursor)
        self._discover_sql_instances(credentials, project_id, account, cursor)

        conn.commit()
        cursor.close()

    @staticmethod
    def _upsert(cursor, account_id, resource_type, resource_id, name, tags, region):
        import json as _json

        cursor.execute("""
            INSERT INTO resources
                (aws_account_id, resource_type, resource_id, name, tags, region)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name   = VALUES(name),
                tags   = VALUES(tags),
                region = VALUES(region)
        """, (account_id, resource_type, resource_id, name, _json.dumps(tags or {}), region))

    def _discover_instances(self, credentials, project_id, account, cursor):
        from google.cloud import compute_v1

        client = compute_v1.InstancesClient(credentials=credentials)
        count = 0
        for zone, response in client.aggregated_list(project=project_id):
            if not response.instances:
                continue
            zone_name = zone.split("/")[-1]
            for inst in response.instances:
                labels = dict(inst.labels) if inst.labels else {}
                self._upsert(cursor, account["id"], "gcp_vm", inst.name, inst.name,
                             labels, zone_name)
                count += 1
        logger.info(f"  GCP Compute instances: {count} in {account['account_name']}")

    def _discover_buckets(self, credentials, project_id, account, cursor):
        from google.cloud import storage

        client = storage.Client(project=project_id, credentials=credentials)
        count = 0
        for bucket in client.list_buckets():
            self._upsert(cursor, account["id"], "gcp_storage", bucket.name, bucket.name,
                         bucket.labels or {}, bucket.location)
            count += 1
        logger.info(f"  GCP Storage buckets: {count} in {account['account_name']}")

    def _discover_sql_instances(self, credentials, project_id, account, cursor):
        from googleapiclient.discovery import build

        service = build("sqladmin", "v1beta4", credentials=credentials, cache_discovery=False)
        count = 0
        request = service.instances().list(project=project_id)
        while request is not None:
            response = request.execute()
            for inst in response.get("items", []):
                name = inst.get("name")
                region = inst.get("region", "")
                labels = inst.get("settings", {}).get("userLabels", {})
                self._upsert(cursor, account["id"], "gcp_sql", name, name, labels, region)
                count += 1
            request = service.instances().list_next(previous_request=request, previous_response=response)
        logger.info(f"  GCP Cloud SQL instances: {count} in {account['account_name']}")

    # ── metric catalog ───────────────────────────────────────────

    def get_metric_catalog(self) -> dict:
        return {
            "gcp_vm": (
                "Compute Engine", "compute.googleapis.com/instance", "Compute",
                [
                    ("cpu/utilization", "Percent", "Average", True, "CPU utilization"),
                    ("network/received_bytes_count", "Bytes", "Total", True, "Received network traffic"),
                    ("network/sent_bytes_count", "Bytes", "Total", True, "Sent network traffic"),
                    ("disk/read_bytes_count", "Bytes", "Total", False, "Disk read throughput"),
                    ("disk/write_bytes_count", "Bytes", "Total", False, "Disk write throughput"),
                ],
            ),
            "gcp_storage": (
                "Cloud Storage", "storage.googleapis.com/storage/v2/Bucket", "Storage",
                [
                    ("storage/total_bytes", "Bytes", "Average", True, "Total storage used"),
                    ("storage/object_count", "Count", "Average", True, "Total object count"),
                    ("api/request_count", "Count", "Total", False, "Total API requests"),
                ],
            ),
            "gcp_sql": (
                "Cloud SQL", "cloudsql.googleapis.com/database", "Database",
                [
                    ("cpu/utilization", "Percent", "Average", True, "CPU utilization"),
                    ("memory/utilization", "Percent", "Average", True, "Memory utilization"),
                    ("disk/utilization", "Percent", "Average", True, "Disk utilization"),
                    ("network/connections", "Count", "Average", False, "Active connections"),
                ],
            ),
        }
''',
}


def write_new_files(dry_run: bool, force: bool):
    for rel_path, content in NEW_FILES.items():
        path = ROOT / rel_path
        if path.exists() and not force:
            print(f"SKIP (exists): {rel_path}")
            continue
        if dry_run:
            print(f"[dry-run] would write: {rel_path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"WROTE: {rel_path}")


# ── app/providers/__init__.py edit ──────────────────────────────────

PROVIDERS_INIT = ROOT / "app/providers/__init__.py"
PROVIDERS_INIT_OLD = '''from app.providers import aws  # noqa: F401  (import registers AWSProvider)'''
PROVIDERS_INIT_NEW = '''from app.providers import aws    # noqa: F401  (import registers AWSProvider)
from app.providers import azure  # noqa: F401  (import registers AzureProvider)
from app.providers import gcp    # noqa: F401  (import registers GCPProvider)'''


# ── accounts.py edits ───────────────────────────────────────────────

ACCOUNTS_PY = ROOT / "app/api/admin/accounts.py"
ACCOUNTS_BACKUP_SUFFIX = ".bak.pre-multi-cloud-providers"

LIST_ACCOUNTS_OLD = '''        SELECT id, account_name, account_id, role_arn,
               external_id, default_region, status, created_at,
               last_synced_at, last_discovered_at, description
        FROM aws_accounts
        WHERE status = 'active'
        ORDER BY created_at DESC'''

LIST_ACCOUNTS_NEW = '''        SELECT id, provider, account_name, account_id, role_arn,
               external_id, default_region, status, created_at,
               last_synced_at, last_discovered_at, description
        FROM aws_accounts
        WHERE status = 'active'
        ORDER BY created_at DESC'''

ADD_ACCOUNT_OLD = '''@router.post("")
def add_account(payload: dict = Body(...)):
    account_name = (payload.get("account_name") or "").strip()
    account_id   = (payload.get("account_id")   or "").strip()
    region       = (payload.get("default_region") or "").strip()

    if not account_name:
        raise HTTPException(status_code=400, detail="account_name is required")
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id is required")
    if not region:
        raise HTTPException(status_code=400, detail="default_region is required")

    region = region.split(" ")[0]
    role_arn    = (payload.get("role_arn") or payload.get("iam_role_arn") or "").strip()
    external_id = (payload.get("external_id") or "").strip()
    owner_team  = (payload.get("owner_team") or "").strip()
    environment = (payload.get("environment") or "PROD").strip().upper()
    description = (payload.get("description") or "").strip()
    if role_arn.lower() in ["n/a", "none", "na", ""]:
        role_arn = ""

    # Optional: list of metric_catalog IDs the user picked in the onboarding
    # wizard's "Metrics to Monitor" step. If omitted, the recommended default
    # template is applied automatically.
    selected_metric_ids = payload.get("selected_metric_ids")

    conn   = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO aws_accounts
              (account_name, account_id, role_arn, external_id,
               default_region, status, description, owner_team, environment)
            VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              account_name   = VALUES(account_name),
              default_region = VALUES(default_region),
              status         = 'active',
              description    = VALUES(description),
              owner_team     = VALUES(owner_team),
              environment    = VALUES(environment)
        """, (account_name, account_id, role_arn, external_id, region, description, owner_team, environment))
        conn.commit()

        if cursor.lastrowid:
            new_id = cursor.lastrowid
        else:
            cursor.execute("SELECT id FROM aws_accounts WHERE account_id = %s", (account_id,))
            new_id = cursor.fetchone()[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {str(e)}")
    finally:
        cursor.close()
        conn.close()'''

ADD_ACCOUNT_NEW = '''@router.post("")
def add_account(payload: dict = Body(...)):
    provider = (payload.get("provider") or "aws").strip().lower()
    if provider not in ("aws", "azure", "gcp"):
        raise HTTPException(status_code=400, detail="provider must be aws, azure or gcp")

    account_name = (payload.get("account_name") or "").strip()
    if not account_name:
        raise HTTPException(status_code=400, detail="account_name is required")

    owner_team  = (payload.get("owner_team") or "").strip()
    environment = (payload.get("environment") or "PROD").strip().upper()
    description = (payload.get("description") or "").strip()
    selected_metric_ids = payload.get("selected_metric_ids")

    role_arn = external_id = ""
    tenant_id = subscription_id = client_id = client_secret = ""
    project_id = service_account_email = gcp_service_account_key = ""

    if provider == "aws":
        account_id = (payload.get("account_id") or "").strip()
        region     = (payload.get("default_region") or "").strip()
        if not account_id:
            raise HTTPException(status_code=400, detail="account_id is required")
        if not region:
            raise HTTPException(status_code=400, detail="default_region is required")
        region = region.split(" ")[0]

        role_arn    = (payload.get("role_arn") or payload.get("iam_role_arn") or "").strip()
        external_id = (payload.get("external_id") or "").strip()
        if role_arn.lower() in ["n/a", "none", "na", ""]:
            role_arn = ""

    elif provider == "azure":
        subscription_id = (payload.get("subscription_id") or "").strip()
        tenant_id        = (payload.get("tenant_id") or "").strip()
        client_id         = (payload.get("client_id") or "").strip()
        client_secret     = (payload.get("client_secret") or "").strip()
        region            = (payload.get("primary_region") or payload.get("default_region") or "").strip()
        if not subscription_id:
            raise HTTPException(status_code=400, detail="subscription_id is required")
        if not (tenant_id and client_id and client_secret):
            raise HTTPException(status_code=400, detail="tenant_id, client_id and client_secret are all required")
        if not region:
            raise HTTPException(status_code=400, detail="primary_region is required")
        account_id = subscription_id

    else:  # gcp
        project_id = (payload.get("project_id") or "").strip()
        gcp_service_account_key = (payload.get("gcp_service_account_key") or "").strip()
        service_account_email = (payload.get("service_account_email") or "").strip()
        region = (payload.get("primary_region") or payload.get("default_region") or "").strip()
        if not project_id:
            raise HTTPException(status_code=400, detail="project_id is required")
        if not gcp_service_account_key:
            raise HTTPException(status_code=400, detail="gcp_service_account_key (service account JSON key) is required")
        if not region:
            raise HTTPException(status_code=400, detail="primary_region is required")
        account_id = project_id

    # Validate credentials actually work before writing anything —
    # no account is stored on unverified/broken credentials.
    try:
        from app.providers.registry import get_provider
        candidate = {
            "role_arn": role_arn, "external_id": external_id,
            "tenant_id": tenant_id, "subscription_id": subscription_id,
            "client_id": client_id, "client_secret": client_secret,
            "project_id": project_id, "gcp_service_account_key": gcp_service_account_key,
        }
        if provider != "aws" or role_arn:
            get_provider(provider).validate_credentials(candidate)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Credential validation failed: {e}")

    conn   = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO aws_accounts
              (provider, account_name, account_id, role_arn, external_id,
               tenant_id, subscription_id, client_id, client_secret,
               project_id, service_account_email, gcp_service_account_key,
               default_region, status, description, owner_team, environment)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              provider        = VALUES(provider),
              account_name    = VALUES(account_name),
              role_arn        = VALUES(role_arn),
              external_id     = VALUES(external_id),
              tenant_id       = VALUES(tenant_id),
              subscription_id = VALUES(subscription_id),
              client_id       = VALUES(client_id),
              client_secret   = VALUES(client_secret),
              project_id      = VALUES(project_id),
              service_account_email   = VALUES(service_account_email),
              gcp_service_account_key = VALUES(gcp_service_account_key),
              default_region  = VALUES(default_region),
              status          = 'active',
              description     = VALUES(description),
              owner_team      = VALUES(owner_team),
              environment     = VALUES(environment)
        """, (provider, account_name, account_id, role_arn, external_id,
              tenant_id, subscription_id, client_id, client_secret,
              project_id, service_account_email, gcp_service_account_key,
              region, description, owner_team, environment))
        conn.commit()

        if cursor.lastrowid:
            new_id = cursor.lastrowid
        else:
            cursor.execute("SELECT id FROM aws_accounts WHERE account_id = %s", (account_id,))
            new_id = cursor.fetchone()[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {str(e)}")
    finally:
        cursor.close()
        conn.close()'''

DISCOVER_ACCOUNT_OLD = '''    if not account:
        raise HTTPException(status_code=404, detail="Account not found or inactive")

    try:
        # Was: app.collector.discovery_ec2.discover_aurogov_ec2 — that
        # function does not exist anywhere in the codebase; this endpoint
        # threw ImportError -> 500 on every click. Fixed to go through
        # the real, live discovery path (the same one the scheduler calls
        # every 15 minutes), routed via the provider layer.
        from app.providers.registry import get_provider
        get_provider("aws").discover_resources()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery failed: {str(e)}")'''

DISCOVER_ACCOUNT_NEW = '''    if not account:
        raise HTTPException(status_code=404, detail="Account not found or inactive")

    try:
        # Was: app.collector.discovery_ec2.discover_aurogov_ec2 — that
        # function does not exist anywhere in the codebase; this endpoint
        # threw ImportError -> 500 on every click. Fixed to go through
        # the real, live discovery path (the same one the scheduler calls
        # every 15 minutes), routed via the provider layer. Dispatches on
        # this account's own provider — was hardcoded to "aws" before,
        # which silently no-op'd discovery for Azure/GCP accounts.
        from app.providers.registry import get_provider
        get_provider(account.get("provider") or "aws").discover_resources()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery failed: {str(e)}")'''


def guarded_replace(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        die(f"anchor '{label}' found {count} times in accounts.py, expected 1. "
            "Local file has diverged from what this script expects.")
    return text.replace(old, new)


def patch_providers_init(dry_run: bool):
    if not PROVIDERS_INIT.exists():
        die(f"{PROVIDERS_INIT} not found.")
    text = PROVIDERS_INIT.read_text(encoding="utf-8")
    if "app.providers import azure" in text:
        print("SKIP (already patched): app/providers/__init__.py")
        return
    text = guarded_replace(text, PROVIDERS_INIT_OLD, PROVIDERS_INIT_NEW, "aws import line")
    if dry_run:
        print("[dry-run] would update app/providers/__init__.py")
        return
    PROVIDERS_INIT.write_text(text, encoding="utf-8")
    print("WROTE: app/providers/__init__.py")


def patch_accounts_py(dry_run: bool):
    if not ACCOUNTS_PY.exists():
        die(f"{ACCOUNTS_PY} not found.")
    text = ACCOUNTS_PY.read_text(encoding="utf-8")

    text = guarded_replace(text, LIST_ACCOUNTS_OLD, LIST_ACCOUNTS_NEW, "list_accounts SELECT")
    text = guarded_replace(text, ADD_ACCOUNT_OLD, ADD_ACCOUNT_NEW, "add_account body")
    text = guarded_replace(text, DISCOVER_ACCOUNT_OLD, DISCOVER_ACCOUNT_NEW, "discover_account body")

    if dry_run:
        print("[dry-run] 3 anchors matched in accounts.py, would write changes.")
        return

    backup = ACCOUNTS_PY.with_suffix(ACCOUNTS_PY.suffix + ACCOUNTS_BACKUP_SUFFIX)
    shutil.copy2(ACCOUNTS_PY, backup)
    print(f"backed up accounts.py -> {backup.name}")

    ACCOUNTS_PY.write_text(text, encoding="utf-8")

    try:
        py_compile.compile(str(ACCOUNTS_PY), doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(backup, ACCOUNTS_PY)
        die(f"accounts.py failed to compile after patching, reverted from backup:\n{e}")

    print("WROTE + compiled OK: app/api/admin/accounts.py")


REQUIREMENTS = ROOT / "requirements.txt"
NEW_REQUIREMENTS = [
    "azure-identity==1.19.0",
    "azure-mgmt-resource==23.1.1",
    "azure-mgmt-compute==33.0.0",
    "azure-mgmt-storage==21.2.1",
    "azure-mgmt-sql==4.0.0b21",
    "google-cloud-resource-manager==1.13.0",
    "google-cloud-compute==1.19.2",
    "google-cloud-storage==2.18.2",
    "google-api-python-client==2.149.0",
]


def patch_requirements(dry_run: bool):
    if not REQUIREMENTS.exists():
        print(f"NOTE: {REQUIREMENTS} not found — skipped.")
        return
    text = REQUIREMENTS.read_text(encoding="utf-8")
    existing_pkgs = {line.split("==")[0].strip().lower() for line in text.splitlines() if line.strip()}
    to_add = [r for r in NEW_REQUIREMENTS if r.split("==")[0].lower() not in existing_pkgs]

    if not to_add:
        print("SKIP (already present): requirements.txt")
        return

    if dry_run:
        print(f"[dry-run] would append {len(to_add)} package(s) to requirements.txt:")
        for r in to_add:
            print(f"  + {r}")
        return

    with REQUIREMENTS.open("a", encoding="utf-8") as f:
        if not text.endswith("\n"):
            f.write("\n")
        for r in to_add:
            f.write(r + "\n")
    print(f"requirements.txt: appended {len(to_add)} package(s).")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="overwrite new files if they already exist")
    args = parser.parse_args()

    if not (ROOT / "app").exists():
        die("app/ not found — run this from the project root.")

    write_new_files(args.dry_run, args.force)
    patch_providers_init(args.dry_run)
    patch_accounts_py(args.dry_run)
    patch_requirements(args.dry_run)

    if args.dry_run:
        print("\nDry run complete. No files written. Re-run without --dry-run to apply.")
    else:
        print("\nDone. Next:")
        print("  pip install -r requirements.txt")
        print("  restart uvicorn")
        print("  python -c \"from app.providers.registry import get_provider; "
              "print(get_provider('azure').name, get_provider('gcp').name)\"")


if __name__ == "__main__":
    main()
