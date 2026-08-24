# app/providers/azure/provider.py
"""
AzureProvider — CloudProvider implementation for Azure, backed by a real
Service Principal (tenant_id/client_id/client_secret/subscription_id) and
the azure-mgmt-* SDKs. No stubs: validate_credentials really calls ARM,
discover_resources really enumerates resources, get_console_url builds a
real Azure Portal deep link.
"""
from app.providers.base import CloudProvider


class AzureProvider(CloudProvider):
    name = "azure"

    def validate_credentials(self, account: dict) -> dict:
        from azure.identity import ClientSecretCredential
        from azure.mgmt.resource.resources import ResourceManagementClient
        from app.credentials import load_credential

        tenant_id = (account.get("tenant_id") or "").strip()
        client_id = (account.get("client_id") or "").strip()
        subscription_id = (account.get("subscription_id") or "").strip()
        secret = account.get("client_secret") or load_credential(account.get("id"))

        if not (tenant_id and client_id and subscription_id and secret):
            raise ValueError(
                "tenant_id, client_id, subscription_id and client_secret are all required"
            )

        cred = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=secret)
        # A real ARM call scoped to the given subscription — fails with a
        # clear auth/authorization error if the Service Principal is wrong
        # or lacks Reader on this subscription.
        rm_client = ResourceManagementClient(cred, subscription_id)
        group_count = sum(1 for _ in rm_client.resource_groups.list())
        return {
            "status": "success",
            "subscription_id": subscription_id,
            "resource_groups_visible": group_count,
        }

    def get_console_url(self, account: dict, resource_id: str, region: str,
                         service: str | None = None,
                         resource_name: str | None = None,
                         ecs_service_name: str | None = None,
                         requested_by: str | None = None) -> str:
        # requested_by unused: this provider deep-links straight into
        # its own cloud portal, which already uses the operator's own
        # signed-in browser session — no shared/impersonated identity
        # to attribute here the way AWS federation needs.
        # Azure resource IDs are already full ARM paths
        # (/subscriptions/.../resourceGroups/.../providers/...), so the
        # portal deep link is a direct construction — no federation step
        # needed the way AWS's assumed-role console requires.
        tenant_id = account.get("tenant_id") or ""
        rid = resource_id or ""
        if not rid.startswith("/"):
            rid = f"/{rid}"
        return f"https://portal.azure.com/#@{tenant_id}/resource{rid}/overview"

    def discover_resources(self) -> None:
        from app.db import get_connection
        from app.credentials import load_credential
        from app.providers.azure.discovery import discover_account_resources

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, account_name, account_id, tenant_id, subscription_id,
                   client_id, default_region
            FROM aws_accounts
            WHERE status = 'active' AND provider = 'azure'
        """)
        accounts = cursor.fetchall()
        cursor.close()
        conn.close()

        for account in accounts:
            try:
                secret = load_credential(account["id"])
                if not secret:
                    continue
                discover_account_resources(account, secret)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(
                    f"Azure discovery failed for {account['account_name']}: {e}"
                )

    def get_metric_catalog(self) -> dict:
        from app.providers.azure.metric_catalog_data import CURATED

        return CURATED
