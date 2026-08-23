# app/api/admin/accounts.py
from fastapi import APIRouter, HTTPException, Body, Query
from app.db import get_connection
import datetime
import json

router = APIRouter(prefix="/api/admin/accounts", tags=["Admin - Accounts"])


def _serialize(obj):
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    return obj


def _write_audit(actor: str, action: str, detail: str, role: str = "ADMIN"):
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audit_logs (actor, action, payload) VALUES (%s, %s, %s)",
            (actor, action, json.dumps({"detail": detail, "role": role}))
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Audit write error: {e}")


def _bust_accounts_cache():
    """Force live_data accounts cache to expire immediately."""
    try:
        from app.api.live_data import _accounts_cache
        _accounts_cache["ts"] = 0
        _accounts_cache["data"] = None
    except Exception as e:
        print(f"Cache bust error: {e}")


@router.get("")
def list_accounts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, account_name, account_id, role_arn, provider,
               external_id, default_region, status, created_at,
               last_synced_at, last_discovered_at, description,
               tenant_id, subscription_id, client_id,
               project_id, service_account_email
        FROM aws_accounts
        WHERE status = 'active'
        ORDER BY created_at DESC
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    # Never leak secrets: these columns only ever hold identifiers, never
    # the client secret / SA key JSON (those live encrypted in
    # provider_credentials and are only decrypted server-side on demand).
    return [_serialize(r) for r in rows]


@router.get("/{account_id}")
def get_account(account_id: int):
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM aws_accounts WHERE id = %s", (account_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Account not found")
    return _serialize(row)


def _add_aws_account(payload: dict) -> tuple[int, str, str]:
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

    conn   = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO aws_accounts
              (account_name, account_id, provider, role_arn, external_id,
               default_region, status, description, owner_team, environment)
            VALUES (%s, %s, 'aws', %s, %s, %s, 'active', %s, %s, %s)
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
        conn.close()
    return new_id, account_name, "aws"


def _add_azure_account(payload: dict) -> tuple[int, str, str]:
    from app.providers.registry import get_provider
    from app.credentials import save_credential, new_credential_ref

    account_name    = (payload.get("account_name") or "").strip()
    tenant_id       = (payload.get("tenant_id") or "").strip()
    subscription_id = (payload.get("subscription_id") or "").strip()
    client_id       = (payload.get("client_id") or "").strip()
    client_secret   = (payload.get("client_secret") or "").strip()
    region          = (payload.get("default_region") or "").strip()
    owner_team      = (payload.get("owner_team") or "").strip()
    environment     = (payload.get("environment") or "PROD").strip().upper()
    description     = (payload.get("description") or "").strip()

    missing = [f for f, v in [("account_name", account_name), ("tenant_id", tenant_id),
                               ("subscription_id", subscription_id), ("client_id", client_id),
                               ("client_secret", client_secret), ("default_region", region)]
               if not v]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required field(s): {', '.join(missing)}")

    # Validate against real Azure ARM before writing anything.
    provider = get_provider("azure")
    try:
        provider.validate_credentials({
            "tenant_id": tenant_id, "client_id": client_id,
            "subscription_id": subscription_id, "client_secret": client_secret,
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Azure credential validation failed: {e}")

    conn   = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO aws_accounts
              (account_name, account_id, provider, tenant_id, subscription_id, client_id,
               default_region, status, description, owner_team, environment)
            VALUES (%s, %s, 'azure', %s, %s, %s, %s, 'active', %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              account_name   = VALUES(account_name),
              default_region = VALUES(default_region),
              status         = 'active',
              description    = VALUES(description),
              owner_team     = VALUES(owner_team),
              environment    = VALUES(environment)
        """, (account_name, subscription_id, tenant_id, subscription_id, client_id,
              region, description, owner_team, environment))
        conn.commit()
        if cursor.lastrowid:
            new_id = cursor.lastrowid
        else:
            cursor.execute("SELECT id FROM aws_accounts WHERE account_id = %s AND provider = 'azure'",
                            (subscription_id,))
            new_id = cursor.fetchone()[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {str(e)}")
    finally:
        cursor.close()
        conn.close()

    ref = new_credential_ref()
    save_credential(new_id, "azure", client_secret, ref)
    conn = get_connection(); cursor = conn.cursor()
    cursor.execute("UPDATE aws_accounts SET credential_ref = %s WHERE id = %s", (ref, new_id))
    conn.commit(); cursor.close(); conn.close()

    return new_id, account_name, "azure"


def _add_gcp_account(payload: dict) -> tuple[int, str, str]:
    from app.providers.registry import get_provider
    from app.credentials import save_credential, new_credential_ref
    import json as _json

    account_name          = (payload.get("account_name") or "").strip()
    project_id            = (payload.get("project_id") or "").strip()
    service_account_key   = (payload.get("service_account_key") or "").strip()
    region                = (payload.get("default_region") or "").strip()
    owner_team            = (payload.get("owner_team") or "").strip()
    environment           = (payload.get("environment") or "PROD").strip().upper()
    description           = (payload.get("description") or "").strip()

    missing = [f for f, v in [("account_name", account_name), ("project_id", project_id),
                               ("service_account_key", service_account_key), ("default_region", region)]
               if not v]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required field(s): {', '.join(missing)}")

    try:
        key_obj = _json.loads(service_account_key)
        service_account_email = key_obj.get("client_email", "")
    except _json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="service_account_key must be valid JSON (the SA key file contents)")

    # Validate against real GCP Resource Manager before writing anything.
    provider = get_provider("gcp")
    try:
        provider.validate_credentials({"project_id": project_id, "service_account_key": service_account_key})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"GCP credential validation failed: {e}")

    conn   = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO aws_accounts
              (account_name, account_id, provider, project_id, service_account_email,
               default_region, status, description, owner_team, environment)
            VALUES (%s, %s, 'gcp', %s, %s, %s, 'active', %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              account_name   = VALUES(account_name),
              default_region = VALUES(default_region),
              status         = 'active',
              description    = VALUES(description),
              owner_team     = VALUES(owner_team),
              environment    = VALUES(environment)
        """, (account_name, project_id, project_id, service_account_email,
              region, description, owner_team, environment))
        conn.commit()
        if cursor.lastrowid:
            new_id = cursor.lastrowid
        else:
            cursor.execute("SELECT id FROM aws_accounts WHERE account_id = %s AND provider = 'gcp'",
                            (project_id,))
            new_id = cursor.fetchone()[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {str(e)}")
    finally:
        cursor.close()
        conn.close()

    ref = new_credential_ref()
    save_credential(new_id, "gcp", service_account_key, ref)
    conn = get_connection(); cursor = conn.cursor()
    cursor.execute("UPDATE aws_accounts SET credential_ref = %s WHERE id = %s", (ref, new_id))
    conn.commit(); cursor.close(); conn.close()

    return new_id, account_name, "gcp"


@router.post("")
def add_account(payload: dict = Body(...)):
    provider_name = (payload.get("provider") or "aws").strip().lower()

    if provider_name == "azure":
        new_id, account_name, provider_name = _add_azure_account(payload)
    elif provider_name == "gcp":
        new_id, account_name, provider_name = _add_gcp_account(payload)
    else:
        new_id, account_name, provider_name = _add_aws_account(payload)

    # Optional: list of metric_catalog IDs the user picked in the onboarding
    # wizard's "Metrics to Monitor" step. If omitted, the recommended default
    # template (scoped to this provider) is applied automatically.
    selected_metric_ids = payload.get("selected_metric_ids")
    try:
        from app.api.metric_catalog import seed_account_defaults
        if selected_metric_ids:
            from app.api.metric_catalog import set_account_metrics
            set_account_metrics(new_id, {"enabled_metric_ids": selected_metric_ids})
        else:
            seed_account_defaults(new_id, provider=provider_name)
    except Exception as e:
        print(f"Metric template seed error: {e}")

    _bust_accounts_cache()
    _write_audit("admin", "Account onboarded", f"{account_name} ({provider_name}) id={new_id}")
    return {"status": "added", "id": new_id, "account_name": account_name, "provider": provider_name}


@router.delete("/{account_id}")
def delete_account(account_id: int):
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        "SELECT account_name, account_id FROM aws_accounts WHERE id = %s",
        (account_id,)
    )
    account = cursor.fetchone()
    if not account:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Account not found")

    cursor.execute(
        "UPDATE aws_accounts SET status = 'inactive' WHERE id = %s",
        (account_id,)
    )

    # Clean up everything this account left behind so it can't show up
    # as stale/orphaned alerts later (this was previously a bug — removed
    # accounts left their resources/metrics/alerts behind indefinitely).
    cursor.execute("""
        DELETE a FROM alerts a
        JOIN resources r ON r.resource_id = a.resource_id
        WHERE r.aws_account_id = %s
    """, (account_id,))
    cursor.execute("""
        DELETE m FROM metrics m
        JOIN resources r ON r.id = m.resource_id
        WHERE r.aws_account_id = %s
    """, (account_id,))
    cursor.execute("DELETE FROM resources WHERE aws_account_id = %s", (account_id,))

    conn.commit()
    cursor.close()
    conn.close()

    # Bust cache so next poll doesn't return deleted account
    _bust_accounts_cache()

    _write_audit("admin", "Account removed",
                 f"{account['account_name']} ({account['account_id']}) removed from monitoring")

    return {"status": "removed", "id": account_id, "account_name": account["account_name"]}


@router.get("/{account_id}/console-url")
def get_account_console_url(
    account_id: int,
    service: str = Query(None),
    resource_id: str = Query(None),
    region: str = Query(None),
    resource_name: str = Query(None),
    ecs_service_name: str = Query(None),
):
    """
    Generic account-scoped console deep link — the single backend source
    ServiceDetail/AccountDetail call instead of building console URLs
    client-side (same pattern the Alerts page already used). Dispatches
    through the provider layer so this also works for Azure/GCP once
    those providers implement get_console_url.
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM aws_accounts WHERE id = %s AND status = 'active'", (account_id,))
    account = cursor.fetchone()
    cursor.close()
    conn.close()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found or inactive")
    if (account.get("provider") or "aws") == "aws" and not account.get("role_arn"):
        raise HTTPException(status_code=400, detail="No AWS role configured for this account")

    region = region or account.get("default_region")

    try:
        from app.providers.registry import get_provider
        provider = get_provider(account.get("provider") or "aws")
        url = provider.get_console_url(
            account, resource_id, region,
            service=service, resource_name=resource_name,
            ecs_service_name=ecs_service_name,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not generate console link: {e}")

    return {"url": url, "account_id": account["account_id"]}


@router.post("/test-role")
def test_role(payload: dict = Body(...)):
    role_arn = (payload.get("role_arn") or "").strip()
    ext_id   = (payload.get("external_id") or "").strip()

    if not role_arn or not role_arn.startswith("arn:aws:"):
        raise HTTPException(status_code=400, detail="Valid IAM Role ARN required")

    try:
        from app.aws.sts import assume_role
        session  = assume_role(role_arn, ext_id)
        sts      = session.client("sts")
        identity = sts.get_caller_identity()
        return {"status": "success", "assumed_account": identity["Account"], "assumed_arn": identity["Arn"]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Role assumption failed: {str(e)}")


@router.post("/test-azure-credentials")
def test_azure_credentials(payload: dict = Body(...)):
    """Onboarding-wizard 'Test Connection' for Azure — validates a Service
    Principal against real Azure Resource Manager before the account is saved."""
    from app.providers.registry import get_provider

    tenant_id       = (payload.get("tenant_id") or "").strip()
    subscription_id = (payload.get("subscription_id") or "").strip()
    client_id       = (payload.get("client_id") or "").strip()
    client_secret   = (payload.get("client_secret") or "").strip()

    if not all([tenant_id, subscription_id, client_id, client_secret]):
        raise HTTPException(status_code=400, detail="tenant_id, subscription_id, client_id and client_secret are required")

    try:
        result = get_provider("azure").validate_credentials({
            "tenant_id": tenant_id, "client_id": client_id,
            "subscription_id": subscription_id, "client_secret": client_secret,
        })
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Azure credential validation failed: {e}")


@router.post("/test-gcp-credentials")
def test_gcp_credentials(payload: dict = Body(...)):
    """Onboarding-wizard 'Test Connection' for GCP — validates a Service
    Account key against the real Cloud Resource Manager API before the
    account is saved."""
    from app.providers.registry import get_provider

    project_id           = (payload.get("project_id") or "").strip()
    service_account_key  = (payload.get("service_account_key") or "").strip()

    if not project_id or not service_account_key:
        raise HTTPException(status_code=400, detail="project_id and service_account_key are required")

    try:
        result = get_provider("gcp").validate_credentials({
            "project_id": project_id, "service_account_key": service_account_key,
        })
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"GCP credential validation failed: {e}")


@router.post("/{account_id}/discover")
def discover_account(account_id: int):
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM aws_accounts WHERE id = %s AND status = 'active'", (account_id,))
    account = cursor.fetchone()
    cursor.close()
    conn.close()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found or inactive")

    try:
        # Was: app.collector.discovery_ec2.discover_aurogov_ec2 — that
        # function does not exist anywhere in the codebase; this endpoint
        # threw ImportError -> 500 on every click. Fixed to go through
        # the real, live discovery path (the same one the scheduler calls
        # every 15 minutes), routed via the provider layer. Each provider's
        # discover_resources() runs for ALL of that provider's active
        # accounts (matches the AWS scheduler's existing contract), so this
        # single call also refreshes this account.
        from app.providers.registry import get_provider
        get_provider(account.get("provider") or "aws").discover_resources()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery failed: {str(e)}")

    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE aws_accounts SET last_discovered_at = NOW() WHERE id = %s", (account_id,))
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit("admin", "Account discovery triggered", f"{account['account_name']} ({account['account_id']})")
    return {"status": "discovery triggered", "account_id": account_id}