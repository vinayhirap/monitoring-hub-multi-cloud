# app/providers/azure/discovery.py
"""
Real Azure resource discovery via azure-mgmt SDKs. Mirrors
app.collector.discovery.runner's contract: writes/updates rows in
`resources`, scoped to a single account, called from AzureProvider.

Only three resource types for now (VMs, Storage Accounts, SQL Databases) —
matches the CORE tier in metric_catalog_data.CURATED. Extending to more
Azure resource types is just adding another _discover_* function here and
wiring it into discover_account_resources, same pattern as AWS's runner.py.
"""
import json
import logging

from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.sql import SqlManagementClient
from azure.mgmt.web import WebSiteManagementClient

from app.db import get_connection

logger = logging.getLogger(__name__)


def _credential(account: dict, secret: str) -> ClientSecretCredential:
    return ClientSecretCredential(
        tenant_id=account["tenant_id"],
        client_id=account["client_id"],
        client_secret=secret,
    )


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


def _discover_vms(compute_client, account_id, cursor) -> int:
    count = 0
    for vm in compute_client.virtual_machines.list_all():
        _upsert_resource(
            cursor, account_id, "vm", vm.id, vm.name,
            dict(vm.tags or {}), vm.location, "compute",
        )
        count += 1
    return count


def _discover_storage_accounts(storage_client, account_id, cursor) -> int:
    count = 0
    for sa in storage_client.storage_accounts.list():
        _upsert_resource(
            cursor, account_id, "storage_account", sa.id, sa.name,
            dict(sa.tags or {}), sa.location, "storage",
        )
        count += 1
    return count


def _discover_sql_databases(sql_client, account_id, cursor) -> int:
    count = 0
    for server in sql_client.servers.list():
        # server.id looks like .../resourceGroups/{rg}/providers/Microsoft.Sql/servers/{name}
        rg = server.id.split("/resourceGroups/")[1].split("/")[0]
        for db in sql_client.databases.list_by_server(rg, server.name):
            if db.name == "master":
                continue
            _upsert_resource(
                cursor, account_id, "sql_database", db.id, f"{server.name}/{db.name}",
                dict(db.tags or {}), db.location, "database",
            )
            count += 1
    return count


def _discover_app_services(web_client, account_id, cursor) -> int:
    count = 0
    for site in web_client.web_apps.list():
        _upsert_resource(
            cursor, account_id, "app_service", site.id, site.name,
            dict(site.tags or {}), site.location, "compute",
        )
        count += 1
    return count


def discover_account_resources(account: dict, secret: str) -> dict:
    """Run discovery for a single Azure account. Returns a per-type count."""
    cred = _credential(account, secret)
    sub_id = account["subscription_id"]

    compute_client = ComputeManagementClient(cred, sub_id)
    storage_client = StorageManagementClient(cred, sub_id)
    sql_client = SqlManagementClient(cred, sub_id)
    web_client = WebSiteManagementClient(cred, sub_id)

    conn = get_connection()
    cursor = conn.cursor()
    counts = {"vm": 0, "storage_account": 0, "sql_database": 0, "app_service": 0}
    try:
        counts["vm"] = _discover_vms(compute_client, account["id"], cursor)
        counts["storage_account"] = _discover_storage_accounts(storage_client, account["id"], cursor)
        counts["sql_database"] = _discover_sql_databases(sql_client, account["id"], cursor)
        counts["app_service"] = _discover_app_services(web_client, account["id"], cursor)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    logger.info(f"Azure discovery for {account.get('account_name')}: {counts}")
    return counts
