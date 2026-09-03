# app/providers/azure/discovery.py
"""
Real Azure resource discovery via azure-mgmt SDKs. Mirrors
app.collector.discovery.runner's contract: writes/updates rows in
`resources`, scoped to a single account, called from AzureProvider.

Covers all 19 services curated in app.providers.azure.metric_catalog_data
(4 core + 15 extended). Every _discover_* function below was written
against azure-mgmt SDK client/method signatures verified by instantiating
each client with a dummy credential and introspecting its operations
group in a sandbox (no live Azure account was available to exercise these
end-to-end) — see the per-function notes for method choices that weren't
obvious (e.g. Redis/KeyVault have no namespace-wide `.list()`, VNet
Gateways require enumerating resource groups first).
"""
import json
import logging

from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.sql import SqlManagementClient
from azure.mgmt.web import WebSiteManagementClient
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.cosmosdb import CosmosDBManagementClient
from azure.mgmt.redis import RedisManagementClient
from azure.mgmt.servicebus import ServiceBusManagementClient
from azure.mgmt.eventhub import EventHubManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.keyvault import KeyVaultManagementClient
from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.mgmt.cdn import CdnManagementClient
from azure.mgmt.datafactory import DataFactoryManagementClient
# ResourceManagementClient is NOT importable from azure.mgmt.resource's
# top-level package on the azure-mgmt-resource version range pinned in
# requirements.txt (23-27) — confirmed in sandbox; must come from the
# .resources submodule. Same gotcha noted previously for SubscriptionClient.
from azure.mgmt.resource.resources import ResourceManagementClient

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
    # web_apps.list() returns BOTH regular Web Apps and Function Apps —
    # Function Apps are just Sites with "functionapp" in their `kind`
    # string (e.g. "functionapp", "functionapp,linux"). Split them out
    # here so each lands under its own metric_catalog service key instead
    # of function apps silently being counted (and monitored) as app_service.
    count = 0
    for site in web_client.web_apps.list():
        if "functionapp" in (site.kind or ""):
            continue
        _upsert_resource(
            cursor, account_id, "app_service", site.id, site.name,
            dict(site.tags or {}), site.location, "compute",
        )
        count += 1
    return count


def _discover_function_apps(web_client, account_id, cursor) -> int:
    count = 0
    for site in web_client.web_apps.list():
        if "functionapp" not in (site.kind or ""):
            continue
        _upsert_resource(
            cursor, account_id, "function_app", site.id, site.name,
            dict(site.tags or {}), site.location, "compute",
        )
        count += 1
    return count


def _discover_vmss(compute_client, account_id, cursor) -> int:
    count = 0
    for vmss in compute_client.virtual_machine_scale_sets.list_all():
        _upsert_resource(
            cursor, account_id, "vmss", vmss.id, vmss.name,
            dict(vmss.tags or {}), vmss.location, "compute",
        )
        count += 1
    return count


def _discover_aks_clusters(cred, sub_id, account_id, cursor) -> int:
    client = ContainerServiceClient(cred, sub_id)
    count = 0
    for cluster in client.managed_clusters.list():
        _upsert_resource(
            cursor, account_id, "aks_cluster", cluster.id, cluster.name,
            dict(cluster.tags or {}), cluster.location, "compute",
        )
        count += 1
    return count


def _discover_cosmosdb_accounts(cred, sub_id, account_id, cursor) -> int:
    client = CosmosDBManagementClient(cred, sub_id)
    count = 0
    for acct in client.database_accounts.list():
        _upsert_resource(
            cursor, account_id, "cosmosdb_account", acct.id, acct.name,
            dict(acct.tags or {}), acct.location, "database",
        )
        count += 1
    return count


def _discover_redis_caches(cred, sub_id, account_id, cursor) -> int:
    # RedisManagementClient.redis has no subscription-wide `.list()` (only
    # `.list_by_resource_group` / `.list_by_subscription`) — confirmed by
    # inspecting the operations group directly; `.list_by_subscription()`
    # is the one that doesn't require enumerating resource groups first.
    client = RedisManagementClient(cred, sub_id)
    count = 0
    for cache in client.redis.list_by_subscription():
        _upsert_resource(
            cursor, account_id, "redis_cache", cache.id, cache.name,
            dict(cache.tags or {}), cache.location, "database",
        )
        count += 1
    return count


def _discover_service_bus_namespaces(cred, sub_id, account_id, cursor) -> int:
    client = ServiceBusManagementClient(cred, sub_id)
    count = 0
    for ns in client.namespaces.list():
        _upsert_resource(
            cursor, account_id, "service_bus_namespace", ns.id, ns.name,
            dict(ns.tags or {}), ns.location, "messaging",
        )
        count += 1
    return count


def _discover_eventhub_namespaces(cred, sub_id, account_id, cursor) -> int:
    client = EventHubManagementClient(cred, sub_id)
    count = 0
    for ns in client.namespaces.list():
        _upsert_resource(
            cursor, account_id, "eventhub_namespace", ns.id, ns.name,
            dict(ns.tags or {}), ns.location, "messaging",
        )
        count += 1
    return count


def _discover_load_balancers(net_client, account_id, cursor) -> int:
    count = 0
    for lb in net_client.load_balancers.list_all():
        _upsert_resource(
            cursor, account_id, "load_balancer", lb.id, lb.name,
            dict(lb.tags or {}), lb.location, "networking",
        )
        count += 1
    return count


def _discover_application_gateways(net_client, account_id, cursor) -> int:
    count = 0
    for agw in net_client.application_gateways.list_all():
        _upsert_resource(
            cursor, account_id, "application_gateway", agw.id, agw.name,
            dict(agw.tags or {}), agw.location, "networking",
        )
        count += 1
    return count


def _discover_vpn_gateways(net_client, resource_client, account_id, cursor) -> int:
    # VirtualNetworkGateways has no subscription-wide list — its `.list()`
    # requires a resource_group_name (verified via signature inspection),
    # unlike load_balancers/application_gateways which have `.list_all()`.
    # Enumerate resource groups first, same pattern AWS/GCP don't need but
    # Azure's ARM hierarchy does.
    count = 0
    for rg in resource_client.resource_groups.list():
        try:
            for gw in net_client.virtual_network_gateways.list(rg.name):
                _upsert_resource(
                    cursor, account_id, "vpn_gateway", gw.id, gw.name,
                    dict(gw.tags or {}), gw.location, "networking",
                )
                count += 1
        except Exception as e:
            logger.debug(f"VPN gateway discovery skipped for resource group {rg.name}: {e}")
    return count


def _discover_key_vaults(cred, sub_id, account_id, cursor) -> int:
    # vaults.list() (no suffix) returns generic Resource objects missing
    # location/tags — list_by_subscription() returns the full Vault model
    # these fields actually need. Confirmed via signature/return-type
    # inspection in sandbox.
    client = KeyVaultManagementClient(cred, sub_id)
    count = 0
    for kv in client.vaults.list_by_subscription():
        _upsert_resource(
            cursor, account_id, "key_vault", kv.id, kv.name,
            dict(kv.tags or {}), kv.location, "security",
        )
        count += 1
    return count


def _discover_container_instances(cred, sub_id, account_id, cursor) -> int:
    client = ContainerInstanceManagementClient(cred, sub_id)
    count = 0
    for cg in client.container_groups.list():
        _upsert_resource(
            cursor, account_id, "container_instance", cg.id, cg.name,
            dict(cg.tags or {}), cg.location, "compute",
        )
        count += 1
    return count


def _discover_cdn_profiles(cred, sub_id, account_id, cursor) -> int:
    client = CdnManagementClient(cred, sub_id)
    count = 0
    for profile in client.profiles.list():
        _upsert_resource(
            cursor, account_id, "cdn_profile", profile.id, profile.name,
            dict(profile.tags or {}), getattr(profile, "location", "global") or "global", "networking",
        )
        count += 1
    return count


def _discover_data_factories(cred, sub_id, account_id, cursor) -> int:
    client = DataFactoryManagementClient(cred, sub_id)
    count = 0
    for df in client.factories.list():
        _upsert_resource(
            cursor, account_id, "data_factory", df.id, df.name,
            dict(df.tags or {}), df.location, "analytics",
        )
        count += 1
    return count


def _discover_managed_disks(compute_client, account_id, cursor) -> int:
    count = 0
    for disk in compute_client.disks.list():
        _upsert_resource(
            cursor, account_id, "managed_disk", disk.id, disk.name,
            dict(disk.tags or {}), disk.location, "storage",
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
    net_client = NetworkManagementClient(cred, sub_id)
    resource_client = ResourceManagementClient(cred, sub_id)

    conn = get_connection()
    cursor = conn.cursor()
    counts = {
        "vm": 0, "storage_account": 0, "sql_database": 0, "app_service": 0,
        "vmss": 0, "aks_cluster": 0, "function_app": 0, "cosmosdb_account": 0,
        "redis_cache": 0, "service_bus_namespace": 0, "eventhub_namespace": 0,
        "load_balancer": 0, "application_gateway": 0, "key_vault": 0,
        "container_instance": 0, "cdn_profile": 0, "vpn_gateway": 0,
        "data_factory": 0, "managed_disk": 0,
    }
    # (count_key, fn) pairs. Each service is wrapped individually rather
    # than in one big try/except — a single service that's unregistered
    # for this subscription (e.g. Microsoft.ContainerService never
    # enabled) or hits a permissions gap shouldn't take down discovery for
    # every other service. Matches the fail-open design already used for
    # AWS's per-namespace collector fallback.
    steps = [
        ("vm",                    lambda: _discover_vms(compute_client, account["id"], cursor)),
        ("storage_account",       lambda: _discover_storage_accounts(storage_client, account["id"], cursor)),
        ("sql_database",          lambda: _discover_sql_databases(sql_client, account["id"], cursor)),
        ("app_service",           lambda: _discover_app_services(web_client, account["id"], cursor)),
        ("function_app",          lambda: _discover_function_apps(web_client, account["id"], cursor)),
        ("vmss",                  lambda: _discover_vmss(compute_client, account["id"], cursor)),
        ("managed_disk",          lambda: _discover_managed_disks(compute_client, account["id"], cursor)),
        ("aks_cluster",           lambda: _discover_aks_clusters(cred, sub_id, account["id"], cursor)),
        ("cosmosdb_account",      lambda: _discover_cosmosdb_accounts(cred, sub_id, account["id"], cursor)),
        ("redis_cache",           lambda: _discover_redis_caches(cred, sub_id, account["id"], cursor)),
        ("service_bus_namespace", lambda: _discover_service_bus_namespaces(cred, sub_id, account["id"], cursor)),
        ("eventhub_namespace",    lambda: _discover_eventhub_namespaces(cred, sub_id, account["id"], cursor)),
        ("load_balancer",         lambda: _discover_load_balancers(net_client, account["id"], cursor)),
        ("application_gateway",   lambda: _discover_application_gateways(net_client, account["id"], cursor)),
        ("vpn_gateway",           lambda: _discover_vpn_gateways(net_client, resource_client, account["id"], cursor)),
        ("key_vault",             lambda: _discover_key_vaults(cred, sub_id, account["id"], cursor)),
        ("container_instance",    lambda: _discover_container_instances(cred, sub_id, account["id"], cursor)),
        ("cdn_profile",           lambda: _discover_cdn_profiles(cred, sub_id, account["id"], cursor)),
        ("data_factory",          lambda: _discover_data_factories(cred, sub_id, account["id"], cursor)),
    ]

    try:
        for key, fn in steps:
            try:
                counts[key] = fn()
            except Exception as e:
                logger.warning(f"Azure {key} discovery failed for {account.get('account_name')}: {e}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    logger.info(f"Azure discovery for {account.get('account_name')}: {counts}")
    return counts
