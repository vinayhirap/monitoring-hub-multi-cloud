# app/providers/gcp/provider.py
"""
GCPProvider — CloudProvider implementation for GCP, backed by a real
Service Account key (JSON) and google-cloud-* / google-api-python-client.
No stubs: validate_credentials really calls the Cloud Resource Manager API,
discover_resources really enumerates resources, get_console_url builds a
real Cloud Console deep link.
"""
from app.providers.base import CloudProvider


class GCPProvider(CloudProvider):
    name = "gcp"

    def validate_credentials(self, account: dict) -> dict:
        import json
        from google.oauth2 import service_account as gcp_service_account
        from google.cloud import resourcemanager_v3
        from app.credentials import load_credential

        project_id = (account.get("project_id") or "").strip()
        sa_key_json = account.get("service_account_key") or load_credential(account.get("id"))

        if not (project_id and sa_key_json):
            raise ValueError("project_id and service_account_key (JSON) are required")

        try:
            info = json.loads(sa_key_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"service_account_key is not valid JSON: {e}")

        creds = gcp_service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform.read-only"]
        )
        client = resourcemanager_v3.ProjectsClient(credentials=creds)
        project = client.get_project(name=f"projects/{project_id}")
        return {
            "status": "success",
            "project_display_name": project.display_name,
            "project_state": str(project.state),
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
        project_id = account.get("project_id") or ""
        service = (service or "").lower()
        name = resource_name or (resource_id or "").split("/")[-1]

        if service == "compute_instance":
            zone = region or ""
            return (f"https://console.cloud.google.com/compute/instancesDetail/"
                    f"zones/{zone}/instances/{name}?project={project_id}")
        if service == "gcs_bucket":
            return f"https://console.cloud.google.com/storage/browser/{name}?project={project_id}"
        if service == "cloudsql_instance":
            return f"https://console.cloud.google.com/sql/instances/{name}/overview?project={project_id}"
        return f"https://console.cloud.google.com/home/dashboard?project={project_id}"

    def discover_resources(self) -> None:
        from app.db import get_connection
        from app.credentials import load_credential
        from app.providers.gcp.discovery import discover_account_resources

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, account_name, account_id, project_id,
                   service_account_email, default_region
            FROM aws_accounts
            WHERE status = 'active' AND provider = 'gcp'
        """)
        accounts = cursor.fetchall()
        cursor.close()
        conn.close()

        for account in accounts:
            try:
                sa_key_json = load_credential(account["id"])
                if not sa_key_json:
                    continue
                counts = discover_account_resources(account, sa_key_json)

                from app.api.metric_catalog import enable_metrics_for_services
                detected = {k for k, v in counts.items() if v}
                result = enable_metrics_for_services(account["id"], detected, provider="gcp", source="discovered")
                if result["added"]:
                    import logging
                    logging.getLogger(__name__).info(
                        f"GCP: auto-enabled {result['added']} metric(s) for "
                        f"{account['account_name']} across services={result['services']}"
                    )
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(
                    f"GCP discovery failed for {account['account_name']}: {e}"
                )

    def get_metric_catalog(self) -> dict:
        from app.providers.gcp.metric_catalog_data import CURATED

        return CURATED
