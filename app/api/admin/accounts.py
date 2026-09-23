# app/api/admin/accounts.py
from fastapi import APIRouter, HTTPException, Body, Query, Depends
from app.db import get_connection
from app.auth.deps import get_current_user
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids
import datetime
import json
import logging
import re

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/accounts", tags=["Admin - Accounts"])

# Azure region short-names (eastus2, centralindia, westeurope, ...) are
# pure lowercase letters/digits -- validated at onboarding so a bad/
# malicious value can never reach app/providers/azure/metrics_collector.py's
# endpoint construction (f"https://{region}.metrics.monitor.azure.com"),
# which would otherwise be an SSRF + live credential-token-exfiltration
# vector for any editor with accounts.onboard permission. See that
# module's matching validation for the full writeup.
_VALID_AZURE_REGION_RE = re.compile(r"^[a-z0-9]+$")


def _serialize(obj):
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    return obj


from app.audit import write_audit as _write_audit


def _bust_accounts_cache():
    """Force live_data accounts cache to expire immediately."""
    try:
        from app.api.live_data import _accounts_cache
        _accounts_cache["ts"] = 0
        _accounts_cache["data"] = None
    except Exception as e:
        print(f"Cache bust error: {e}")


@router.get("")
def list_accounts(current_user: dict = Depends(require_permission("accounts.view"))):
    conn = get_connection()
    try:
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
    finally:
        # Caught live while verifying the new RBAC admin frontend page
        # against a real schema -- an unrelated SQL error here leaked
        # the connection because conn.close() had no try/finally, same
        # bug class fixed elsewhere this audit pass (app/audit.py,
        # app/api/auth.py). This is the account-listing endpoint every
        # other page in the app calls to populate an account picker,
        # so it's one of the more frequently-hit call sites in the app.
        conn.close()

    accessible = get_accessible_account_ids(current_user)
    if accessible is not None:
        rows = [r for r in rows if r["id"] in accessible]

    # Never leak secrets: these columns only ever hold identifiers, never
    # the client secret / SA key JSON (those live encrypted in
    # provider_credentials and are only decrypted server-side on demand).
    return [_serialize(r) for r in rows]


@router.get("/{account_id}")
def get_account(account_id: int, current_user: dict = Depends(require_permission("accounts.view"))):
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM aws_accounts WHERE id = %s", (account_id,))
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Account not found")
    return _serialize(row)


def _check_duplicate_account_id(account_id_value: str, id_label: str, current_user: dict) -> None:
    """Guard against onboarding the same underlying account twice under a
    different-but-equivalent identifier string.

    `aws_accounts.account_id` already has a DB-level UNIQUE constraint, but
    it only enforces an *exact* string match -- it does nothing for a typo,
    stray whitespace, or a case difference (Azure subscription_id/GCP
    project_id can both vary in case in the wild). Two such near-identical
    values sail straight past that constraint and land as two separate rows.

    This is the confirmed root cause of two real incidents: the "U4RAD"
    duplicate (ids 9 and 10, onboarded ~2 hours apart) and the account
    7/10 identically-named CloudWatch Log Group collision that caused the
    cross-account metric_history leak fixed in 20dbcac. Checking a
    normalized (trimmed, case-insensitive) match here -- before any DB
    write or external credential validation -- closes that gap and fails
    fast with a clear message instead of a generic 500 or a silent dup.

    SECURITY (fix: 2026-09 B04 audit -- HIGH, cross-tenant info
    disclosure): this used to raise the detailed 409 (account_name,
    internal id, provider, status) for a MATCH REGARDLESS OF THE CALLER'S
    RBAC SCOPE. Any user holding only accounts.onboard -- which can be
    scoped to a handful of accounts, same as accounts.view -- could probe
    arbitrary account/subscription/project id strings during "add
    account" and read back the name/id/status of accounts they have no
    accounts.view visibility into at all; no AWS/Azure/GCP call, no
    accounts.view permission, and no correct guess required (a WRONG
    guess that happens to collide still returns the real account's
    details). Now the identifying detail is only included when the
    matched account is inside the caller's own accessible scope (or the
    caller has FULL_ACCESS); otherwise the 409 is generic.

    FUNCTIONAL (fix: 2026-09 B04 audit -- reactivation was unreachable):
    a previously-removed account (delete_account only ever sets
    status='inactive', see that function's updated docstring -- the row
    and its account_id/subscription_id/project_id live on forever) could
    never be re-onboarded through this endpoint: this check unconditionally
    raised 409 for ANY match, active or inactive, before add_account's own
    `INSERT ... ON DUPLICATE KEY UPDATE` reactivation logic (already
    present further down in _add_aws_account/_add_azure_account/
    _add_gcp_account) ever got a chance to run. An inactive match inside
    the caller's own scope is no longer blocked here, so reactivation
    actually works; an inactive match OUTSIDE the caller's scope is still
    blocked (with the same generic message as an active out-of-scope
    match) -- a lower-scoped onboarder should not be able to silently
    reactivate/take over an account outside their assigned scope.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, account_name, provider, status FROM aws_accounts "
            "WHERE LOWER(TRIM(account_id)) = LOWER(TRIM(%s))",
            (account_id_value,),
        )
        existing = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if not existing:
        return

    accessible = get_accessible_account_ids(current_user)
    in_scope = accessible is None or existing["id"] in accessible

    if not in_scope:
        raise HTTPException(
            status_code=409,
            detail=(
                "This external account ID is already onboarded under a "
                "monitoring-hub account you do not have visibility into. "
                "Ask an administrator to resolve the conflict."
            ),
        )

    if existing["status"] == "inactive":
        # Previously removed and back in the caller's own scope -- let
        # the caller's INSERT ... ON DUPLICATE KEY UPDATE reactivate it.
        return

    raise HTTPException(
        status_code=409,
        detail=(
            f"This account is already onboarded as '{existing['account_name']}' "
            f"(id={existing['id']}, provider={existing['provider']}, status={existing['status']}). "
            f"Double-check the {id_label} for a typo or case difference if you meant to "
            f"onboard a different account."
        ),
    )


def _add_aws_account(payload: dict, current_user: dict) -> tuple[int, str, str]:
    import json as _json
    from app.credentials import save_credential, new_credential_ref

    account_name = (payload.get("account_name") or "").strip()
    account_id   = (payload.get("account_id")   or "").strip()
    region       = (payload.get("default_region") or "").strip()

    if not account_name:
        raise HTTPException(status_code=400, detail="account_name is required")
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id is required")
    if not region:
        raise HTTPException(status_code=400, detail="default_region is required")

    _check_duplicate_account_id(account_id, "AWS account ID", current_user)

    region = region.split(" ")[0]
    role_arn    = (payload.get("role_arn") or payload.get("iam_role_arn") or "").strip()
    external_id = (payload.get("external_id") or "").strip()
    owner_team  = (payload.get("owner_team") or "").strip()
    environment = (payload.get("environment") or "PROD").strip().upper()
    description = (payload.get("description") or "").strip()
    if role_arn.lower() in ["n/a", "none", "na", ""]:
        role_arn = ""

    # Static access key/secret key -- an alternative to AssumeRole for
    # accounts where a cross-account trust policy isn't wanted (e.g. a
    # dedicated ReadOnlyAccess IAM user in the target account instead).
    # frontend's AccountOnboarding.jsx (auth_method="access_keys") sends
    # these; role_arn/external_id are sent empty in that mode and vice
    # versa, so presence of both keys is the signal, not a separate flag.
    access_key = (payload.get("access_key") or "").strip()
    secret_key = (payload.get("secret_key") or "").strip()
    auth_mode  = "static_keys" if (access_key and secret_key) else "assume_role"
    if auth_mode == "static_keys":
        role_arn = ""  # keys and role_arn are mutually exclusive for a given account

    conn   = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO aws_accounts
              (account_name, account_id, provider, role_arn, auth_mode, external_id,
               default_region, status, description, owner_team, environment)
            VALUES (%s, %s, 'aws', %s, %s, %s, %s, 'active', %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              account_name   = VALUES(account_name),
              role_arn       = VALUES(role_arn),
              auth_mode      = VALUES(auth_mode),
              external_id    = VALUES(external_id),
              default_region = VALUES(default_region),
              status         = 'active',
              description    = VALUES(description),
              owner_team     = VALUES(owner_team),
              environment    = VALUES(environment)
        """, (account_name, account_id, role_arn, auth_mode, external_id,
              region, description, owner_team, environment))
        conn.commit()
        if cursor.lastrowid:
            new_id = cursor.lastrowid
        else:
            cursor.execute("SELECT id FROM aws_accounts WHERE account_id = %s", (account_id,))
            new_id = cursor.fetchone()[0]
    except HTTPException:
        raise
    except Exception as e:
        # Fix: 2026-09 B04 audit -- raw DB exception text (could include
        # table/column names or other internal detail) no longer goes to
        # the client; logged server-side instead, matching this file's
        # other internal-error handling.
        logger.error(f"add_account (aws): DB error inserting account_id={account_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not save the account -- see server logs.")
    finally:
        cursor.close()
        conn.close()

    if auth_mode == "static_keys":
        # Fix: 2026-09 B04 audit -- MEDIUM, partial-onboarding rollback
        # gap. The account row above is already committed by this point;
        # if the credential save or the credential_ref link-back below
        # failed with no try/except at all, the row was left behind as
        # status='active', auth_mode='static_keys', with NO credential --
        # exactly the broken state get_boto3_session()'s own RuntimeError
        # message already anticipates ("onboarding may have failed
        # partway through"), except nothing actually caught or cleaned it
        # up, so it kept trying (and failing) on every future discovery/
        # collection cycle. Deactivate it the same way the "wrong AWS
        # account resolved" check just below already does for its own
        # failure mode, instead of leaving a silently-broken active row.
        ref = new_credential_ref()
        try:
            save_credential(new_id, "aws", _json.dumps({
                "access_key_id": access_key,
                "secret_access_key": secret_key,
            }), ref)
            conn = get_connection(); cursor = conn.cursor()
            cursor.execute("UPDATE aws_accounts SET credential_ref = %s WHERE id = %s", (ref, new_id))
            conn.commit(); cursor.close(); conn.close()
        except Exception as e:
            logger.error(f"add_account (aws): credential save failed for new id={new_id}: {e}")
            conn = get_connection(); cursor = conn.cursor()
            cursor.execute("UPDATE aws_accounts SET status = 'inactive' WHERE id = %s", (new_id,))
            conn.commit(); cursor.close(); conn.close()
            raise HTTPException(
                status_code=500,
                detail=(
                    "The account row was created but saving its credential failed, "
                    "so the account has been deactivated rather than left broken. "
                    "Try onboarding again."
                ),
            )

    # Verify the credentials actually land in the AWS account number typed
    # into the form. account_id above is just a label the operator typed;
    # nothing previously checked it against the account STS actually
    # resolves to, so a wrong/reused/copy-pasted role ARN (or key pair)
    # onboarded successfully and then silently monitored a completely
    # different AWS account under this account's name on every discovery
    # cycle afterwards, with no error anywhere.
    # (2026-09-16 incident: U4RAD's role ARN in fact assumed into AuroGov
    # Mumbai's own account, 924922671984, so U4RAD's dashboard showed
    # AuroGov Mumbai's real EC2/EBS/S3/Lambda resources.)
    try:
        from app.aws.sts import get_boto3_session
        verify_session = get_boto3_session({
            "id": new_id, "auth_mode": auth_mode,
            "role_arn": role_arn, "external_id": external_id,
        })
        assumed_account = verify_session.client("sts").get_caller_identity()["Account"]
    except Exception as e:
        assumed_account = None
        logger.warning(f"Could not verify assumed AWS account for new account id={new_id}: {e}")

    if assumed_account and assumed_account != account_id:
        conn = get_connection(); cursor = conn.cursor()
        cursor.execute("UPDATE aws_accounts SET status = 'inactive' WHERE id = %s", (new_id,))
        conn.commit(); cursor.close(); conn.close()
        if auth_mode == "static_keys":
            from app.credentials import delete_credential
            delete_credential(new_id)
        raise HTTPException(
            status_code=400,
            detail=(
                f"These credentials resolve to AWS account {assumed_account}, "
                f"not {account_id} as entered. The account was not activated -- "
                f"fix the Role ARN/access keys (or the account ID) and try again."
            ),
        )

    return new_id, account_name, "aws"


def _add_azure_account(payload: dict, current_user: dict) -> tuple[int, str, str]:
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
    if not _VALID_AZURE_REGION_RE.match(region):
        raise HTTPException(
            status_code=400,
            detail="default_region must be a valid Azure region short-name (e.g. 'centralindia', 'eastus2') -- lowercase letters/digits only",
        )

    _check_duplicate_account_id(subscription_id, "Azure subscription ID", current_user)

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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"add_account (azure): DB error inserting subscription_id={subscription_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not save the account -- see server logs.")
    finally:
        cursor.close()
        conn.close()

    # Fix: 2026-09 B04 audit -- same partial-onboarding rollback gap as
    # the AWS static_keys path above: this save was previously
    # unguarded, so a failure here left an active row with no credential.
    ref = new_credential_ref()
    try:
        save_credential(new_id, "azure", client_secret, ref)
        conn = get_connection(); cursor = conn.cursor()
        cursor.execute("UPDATE aws_accounts SET credential_ref = %s WHERE id = %s", (ref, new_id))
        conn.commit(); cursor.close(); conn.close()
    except Exception as e:
        logger.error(f"add_account (azure): credential save failed for new id={new_id}: {e}")
        conn = get_connection(); cursor = conn.cursor()
        cursor.execute("UPDATE aws_accounts SET status = 'inactive' WHERE id = %s", (new_id,))
        conn.commit(); cursor.close(); conn.close()
        raise HTTPException(
            status_code=500,
            detail=(
                "The account row was created but saving its credential failed, "
                "so the account has been deactivated rather than left broken. "
                "Try onboarding again."
            ),
        )

    return new_id, account_name, "azure"


def _add_gcp_account(payload: dict, current_user: dict) -> tuple[int, str, str]:
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

    _check_duplicate_account_id(project_id, "GCP project ID", current_user)

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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"add_account (gcp): DB error inserting project_id={project_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not save the account -- see server logs.")
    finally:
        cursor.close()
        conn.close()

    # Fix: 2026-09 B04 audit -- same partial-onboarding rollback gap as
    # the AWS static_keys path above.
    ref = new_credential_ref()
    try:
        save_credential(new_id, "gcp", service_account_key, ref)
        conn = get_connection(); cursor = conn.cursor()
        cursor.execute("UPDATE aws_accounts SET credential_ref = %s WHERE id = %s", (ref, new_id))
        conn.commit(); cursor.close(); conn.close()
    except Exception as e:
        logger.error(f"add_account (gcp): credential save failed for new id={new_id}: {e}")
        conn = get_connection(); cursor = conn.cursor()
        cursor.execute("UPDATE aws_accounts SET status = 'inactive' WHERE id = %s", (new_id,))
        conn.commit(); cursor.close(); conn.close()
        raise HTTPException(
            status_code=500,
            detail=(
                "The account row was created but saving its credential failed, "
                "so the account has been deactivated rather than left broken. "
                "Try onboarding again."
            ),
        )

    return new_id, account_name, "gcp"


@router.post("")
def add_account(payload: dict = Body(...), current_user: dict = Depends(require_permission("accounts.onboard"))):
    provider_name = (payload.get("provider") or "aws").strip().lower()

    if provider_name == "azure":
        new_id, account_name, provider_name = _add_azure_account(payload, current_user)
    elif provider_name == "gcp":
        new_id, account_name, provider_name = _add_gcp_account(payload, current_user)
    else:
        new_id, account_name, provider_name = _add_aws_account(payload, current_user)

    # Optional: list of metric_catalog IDs the user explicitly picked in the
    # onboarding wizard's "Metrics to Monitor" step (manual override always
    # wins — respected first, no auto-detection runs). If omitted, we try to
    # detect what's actually in the account/region and enable exactly those
    # services' default metrics; only if detection finds nothing at all
    # (brand-new account, insufficient permissions, non-AWS provider) do we
    # fall back to the generic template so the account isn't left blank.
    selected_metric_ids = payload.get("selected_metric_ids")
    try:
        from app.api.metric_catalog import seed_account_defaults
        if selected_metric_ids:
            from app.api.metric_catalog import _set_account_metrics_internal
            _set_account_metrics_internal(new_id, {"enabled_metric_ids": selected_metric_ids},
                                           actor=current_user["username"],
                                           actor_role=current_user["role"].upper())
        elif provider_name == "aws":
            from app.api.metric_catalog import enable_metrics_for_services
            from app.aws.resource_discovery import discover_all_service_keys
            from app.aws.sts import get_boto3_session

            role_arn = (payload.get("role_arn") or payload.get("iam_role_arn") or "").strip()
            if role_arn.lower() in ("n/a", "none", "na"):
                role_arn = ""
            region = (payload.get("default_region") or "ap-south-1").split(" ")[0]

            # Same signal _add_aws_account() used to decide auth_mode and
            # (if static_keys) already wrote the credential for new_id via
            # save_credential -- get_boto3_session's static_keys branch
            # reads it straight back via load_credential(new_id) below.
            access_key = (payload.get("access_key") or "").strip()
            secret_key = (payload.get("secret_key") or "").strip()
            auth_mode  = "static_keys" if (access_key and secret_key) else "assume_role"

            detected = set()
            try:
                session = get_boto3_session({
                    "id": new_id, "auth_mode": auth_mode,
                    "role_arn": role_arn, "external_id": payload.get("external_id"),
                })
                detected = discover_all_service_keys(session, region)
            except Exception as e:
                logger.warning(f"Onboarding auto-detection failed, falling back to template: {e}")

            result = enable_metrics_for_services(new_id, detected, provider="aws", source="discovered")
            if not result["added"]:
                seed_account_defaults(new_id, provider=provider_name)
        elif provider_name == "azure":
            from app.api.metric_catalog import enable_metrics_for_services
            from app.providers.azure.discovery import discover_account_resources

            detected = set()
            try:
                account_for_discovery = {
                    "id": new_id,
                    "account_name": account_name,
                    "tenant_id":       (payload.get("tenant_id") or "").strip(),
                    "subscription_id": (payload.get("subscription_id") or "").strip(),
                    "client_id":       (payload.get("client_id") or "").strip(),
                }
                secret = (payload.get("client_secret") or "").strip()
                counts = discover_account_resources(account_for_discovery, secret)
                detected = {k for k, v in counts.items() if v}
            except Exception as e:
                logger.warning(f"Azure onboarding auto-detection failed, falling back to template: {e}")

            result = enable_metrics_for_services(new_id, detected, provider="azure", source="discovered")
            if not result["added"]:
                seed_account_defaults(new_id, provider=provider_name)

        elif provider_name == "gcp":
            from app.api.metric_catalog import enable_metrics_for_services
            from app.providers.gcp.discovery import discover_account_resources

            detected = set()
            try:
                account_for_discovery = {
                    "id": new_id,
                    "account_name": account_name,
                    "project_id": (payload.get("project_id") or "").strip(),
                }
                sa_key_json = (payload.get("service_account_key") or "").strip()
                counts = discover_account_resources(account_for_discovery, sa_key_json)
                detected = {k for k, v in counts.items() if v}
            except Exception as e:
                logger.warning(f"GCP onboarding auto-detection failed, falling back to template: {e}")

            result = enable_metrics_for_services(new_id, detected, provider="gcp", source="discovered")
            if not result["added"]:
                seed_account_defaults(new_id, provider=provider_name)

        else:
            seed_account_defaults(new_id, provider=provider_name)
    except Exception as e:
        print(f"Metric template seed error: {e}")

    _bust_accounts_cache()
    _write_audit(current_user["username"], "Account onboarded", f"{account_name} ({provider_name}) id={new_id}", role=current_user["role"].upper())
    return {"status": "added", "id": new_id, "account_name": account_name, "provider": provider_name}


@router.delete("/{account_id}")
def delete_account(account_id: int, current_user: dict = Depends(require_permission("accounts.delete"))):
    # Admin-only: no existing permission code covers "delete an entire
    # monitored account" (accounts.onboard is scoped to ADDING one in the
    # permission catalog's own description), and this is irreversible --
    # deliberately not extending accounts.onboard to also cover deletion.
    #
    # IMPORTANT: this is a SOFT delete (status set to 'inactive') -- the
    # aws_accounts row itself is never removed. Every ON DELETE CASCADE
    # foreign key that references aws_accounts(id) (provider_credentials,
    # account_metric_selections, escalation_policies, cloud_events,
    # incidents, resource_health, ...) therefore NEVER FIRES, because
    # nothing ever deletes that row. Every child table must be cleaned up
    # explicitly here, exactly like this function was already doing for
    # alerts/metrics/resources/resource_relationships.
    #
    # SECURITY/CORRECTNESS (fix: 2026-09 B04 audit -- CRITICAL, complete
    # cascade): a repo-wide grep for `aws_account_id`/`account_id` columns
    # found 12 more tables this function left completely untouched:
    # provider_credentials, account_metric_selections, escalation_policies,
    # cloud_events, incidents (which cascades incident_alerts via its own
    # FK once incidents rows are deleted), resource_health,
    # synthetic_checks (cascades synthetic_check_results via its own FK),
    # slo_definitions, security_findings, maintenance_windows,
    # status_page_components, alert_pending, metric_baseline. Left as-is,
    # a "removed" account kept: (a) its encrypted credential sitting in
    # provider_credentials indefinitely, readable by load_credential() by
    # anyone who could still reach it via id; (b) stale
    # incidents/security_findings/synthetic-check results/SLO data,
    # invisible in the (correctly status='active'-filtered) accounts list
    # but still directly queryable by id through every other endpoint in
    # this app that doesn't itself filter by account status; and (c) most
    # seriously, since _check_duplicate_account_id matches the SAME
    # account_id string regardless of status, re-onboarding the exact same
    # AWS/Azure/GCP account later (see that function's reactivation fix
    # above) reactivated this same row -- resurrecting all of that stale
    # data as if it belonged to the "new" onboarding, with no indication
    # any of it was actually left over from before the account was removed.
    # op_events (nullable aws_account_id -- this app's OWN operational
    # health log, not account-facing data) and reports/report_jobs
    # (historical report artifacts, out of this slice's file scope) are
    # deliberately NOT touched here -- see this chat's handoff notes.
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT account_name, account_id FROM aws_accounts WHERE id = %s",
            (account_id,)
        )
        account = cursor.fetchone()
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")

        cursor.execute(
            "UPDATE aws_accounts SET status = 'inactive' WHERE id = %s",
            (account_id,)
        )

        # Clean up everything this account left behind so it can't show up
        # as stale/orphaned alerts later (this was previously a bug — removed
        # accounts left their resources/metrics/alerts behind indefinitely).
        cursor.execute("DELETE FROM alerts WHERE aws_account_id = %s", (account_id,))
        cursor.execute("""
            DELETE m FROM metrics m
            JOIN resources r ON r.id = m.resource_id
            WHERE r.aws_account_id = %s
        """, (account_id,))
        cursor.execute("DELETE FROM resources WHERE aws_account_id = %s", (account_id,))
        cursor.execute("DELETE FROM resource_relationships WHERE aws_account_id = %s", (account_id,))

        # -- Fix: 2026-09 B04 audit -- the remaining account-scoped tables --
        cursor.execute("DELETE FROM account_metric_selections WHERE aws_account_id = %s", (account_id,))
        cursor.execute("DELETE FROM escalation_policies WHERE aws_account_id = %s", (account_id,))
        cursor.execute("DELETE FROM cloud_events WHERE aws_account_id = %s", (account_id,))
        # incident_alerts cascades automatically (FK ON DELETE CASCADE on
        # incident_id) once the matching incidents rows are removed.
        cursor.execute("DELETE FROM incidents WHERE aws_account_id = %s", (account_id,))
        cursor.execute("DELETE FROM resource_health WHERE aws_account_id = %s", (account_id,))
        # synthetic_check_results cascades automatically (FK ON DELETE
        # CASCADE on check_id) once the matching synthetic_checks rows
        # are removed; slo_definitions.synthetic_check_id also cascades.
        cursor.execute("DELETE FROM synthetic_checks WHERE aws_account_id = %s", (account_id,))
        cursor.execute("DELETE FROM slo_definitions WHERE aws_account_id = %s", (account_id,))
        cursor.execute("DELETE FROM security_findings WHERE aws_account_id = %s", (account_id,))
        cursor.execute("DELETE FROM maintenance_windows WHERE aws_account_id = %s", (account_id,))
        cursor.execute("DELETE FROM status_page_components WHERE aws_account_id = %s", (account_id,))
        cursor.execute("DELETE FROM alert_pending WHERE aws_account_id = %s", (account_id,))
        cursor.execute("DELETE FROM metric_baseline WHERE aws_account_id = %s", (account_id,))

        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    # provider_credentials: use the existing dedicated helper (same
    # encryption-key module every other credential write/read goes
    # through) rather than a raw DELETE here.
    try:
        from app.credentials import delete_credential
        delete_credential(account_id)
    except Exception as e:
        logger.warning(f"delete_account: credential cleanup failed for id={account_id}: {e}")

    # Bust cache so next poll doesn't return deleted account
    _bust_accounts_cache()

    _write_audit(current_user["username"], "Account removed",
                 f"{account['account_name']} ({account['account_id']}) removed from monitoring",
                 role=current_user["role"].upper())

    return {"status": "removed", "id": account_id, "account_name": account["account_name"]}


@router.post("/{account_id}/console-url")
def get_account_console_url(
    account_id: int,
    service: str = Query(None),
    resource_id: str = Query(None),
    region: str = Query(None),
    resource_name: str = Query(None),
    ecs_service_name: str = Query(None),
    user: dict = Depends(require_permission("accounts.view")),
):
    """
    Generic account-scoped console deep link — the single backend source
    ServiceDetail/AccountDetail call instead of building console URLs
    client-side (same pattern the Alerts page already used). Dispatches
    through the provider layer so this also works for Azure/GCP once
    those providers implement get_console_url.

    POST, not GET, despite this only fetching a URL: this endpoint
    writes an audit-log entry as a side effect (_write_console_open_audit
    in app/aws/federation.py), which violates the HTTP "GET is safe/
    side-effect-free" contract. Under SameSite=Lax cookies (see
    app/api/auth.py's login()), a GET version would still send the
    session cookie on a plain top-level navigation (e.g. a crafted
    <a href> link), letting a CSRF attacker force a spurious "console
    opened" audit entry under the victim's name -- low severity (no
    data exposure, since CORS_ALLOWED_ORIGINS is an explicit allowlist
    so the attacker's page can trigger this but never read the
    response) but a real correctness gap, fixed 2026-09-12. All
    frontend callers already use fetch()/apiFetch() rather than a raw
    browser navigation, so this required no other behavior change.
    """
    accessible = get_accessible_account_ids(user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM aws_accounts WHERE id = %s AND status = 'active'", (account_id,))
        account = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found or inactive")

    region = region or account.get("default_region")

    try:
        from app.providers.registry import get_provider
        from app.aws.federation import NoConsoleCredentialsError
        provider = get_provider(account.get("provider") or "aws")
        url = provider.get_console_url(
            account, resource_id, region,
            service=service, resource_name=resource_name,
            ecs_service_name=ecs_service_name,
            requested_by=user["username"],
        )
    except NoConsoleCredentialsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not generate console link: {e}")

    return {"url": url, "account_id": account["account_id"]}


@router.post("/test-role")
def test_role(payload: dict = Body(...), current_user: dict = Depends(require_permission("accounts.onboard"))):
    role_arn   = (payload.get("role_arn") or "").strip()
    ext_id     = (payload.get("external_id") or "").strip()
    access_key = (payload.get("access_key") or "").strip()
    secret_key = (payload.get("secret_key") or "").strip()

    region = (payload.get("region") or payload.get("default_region") or "ap-south-1").strip()

    # Two mutually exclusive auth paths, matching _add_aws_account()'s
    # signal (presence of both keys means static-key mode, regardless of
    # whether role_arn is also present in the payload).
    if access_key and secret_key:
        try:
            import boto3 as _boto3
            session  = _boto3.Session(
                aws_access_key_id=access_key, aws_secret_access_key=secret_key,
            )
            sts      = session.client("sts")
            identity = sts.get_caller_identity()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Access key validation failed: {str(e)}")
    else:
        if not role_arn or not role_arn.startswith("arn:aws:"):
            raise HTTPException(status_code=400, detail="Valid IAM Role ARN required")
        try:
            from app.aws.sts import assume_role
            session  = assume_role(role_arn, ext_id)
            sts      = session.client("sts")
            identity = sts.get_caller_identity()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Role assumption failed: {str(e)}")

    # Best-effort service detection for the onboarding wizard preview
    # ("Detected: EC2, RDS, ALB — monitoring will be enabled automatically").
    # Never fails the role-test itself — a role that can AssumeRole but is
    # still missing a Describe/Tagging permission should still onboard;
    # discovery just runs again on the next 15-min cycle either way.
    detected_services = []
    try:
        from app.aws.resource_discovery import discover_all_service_keys
        detected_services = sorted(discover_all_service_keys(session, region))
    except Exception as e:
        logger.warning(f"test-role service detection skipped: {e}")

    return {
        "status": "success",
        "assumed_account": identity["Account"],
        "assumed_arn": identity["Arn"],
        "detected_services": detected_services,
    }


@router.post("/test-azure-credentials")
def test_azure_credentials(payload: dict = Body(...), current_user: dict = Depends(require_permission("accounts.onboard"))):
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
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Azure credential validation failed: {e}")

    # Best-effort service detection for the onboarding wizard preview, same
    # rationale as test_role's AWS equivalent above: never fails the
    # credential-test response itself if detection hits a permissions gap
    # (Resource Graph is a separate RBAC surface from the per-service Reader
    # roles already needed for the 19 curated discovery functions).
    detected_services = []
    try:
        from azure.identity import ClientSecretCredential
        from app.providers.azure.discovery import detect_extended_service_keys

        cred = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
        conn = get_connection(); cur = conn.cursor()
        try:
            detected_services = sorted(detect_extended_service_keys(cred, subscription_id, cur))
        finally:
            cur.close(); conn.close()
    except Exception as e:
        logger.warning(f"test-azure-credentials service detection skipped: {e}")

    result["detected_services"] = detected_services
    return result


@router.post("/test-gcp-credentials")
def test_gcp_credentials(payload: dict = Body(...), current_user: dict = Depends(require_permission("accounts.onboard"))):
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
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"GCP credential validation failed: {e}")

    # Best-effort service detection for the onboarding wizard preview, same
    # rationale as test_role's AWS equivalent above: never fails the
    # credential-test response itself if detection hits a permissions gap
    # (Cloud Asset Inventory needs its own roles/cloudasset.viewer grant,
    # separate from the per-service Viewer roles already needed for the 16
    # curated discovery functions).
    detected_services = []
    try:
        import json as _json
        from google.oauth2 import service_account as gcp_service_account
        from app.providers.gcp.discovery import detect_extended_service_keys

        info = _json.loads(service_account_key)
        creds = gcp_service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform.read-only"]
        )
        conn = get_connection(); cur = conn.cursor()
        try:
            detected_services = sorted(detect_extended_service_keys(creds, project_id, cur))
        finally:
            cur.close(); conn.close()
    except Exception as e:
        logger.warning(f"test-gcp-credentials service detection skipped: {e}")

    result["detected_services"] = detected_services
    return result


@router.post("/{account_id}/discover")
def discover_account(account_id: int, current_user: dict = Depends(require_permission("accounts.onboard"))):
    # SECURITY: this endpoint had no account-scope check at all --
    # every other account_id-taking route in this file (get_account,
    # get_account_console_url) checks get_accessible_account_ids
    # first; this one didn't, so any editor holding the role-level
    # accounts.onboard permission could pass an arbitrary account_id
    # and (a) learn whether it exists/is active from the 404 vs 200
    # response (account enumeration outside their scope) and (b)
    # trigger real discovery/AWS-API calls against an account they
    # have no assigned access to at all. Brought in line with the
    # pattern already used elsewhere in this file.
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM aws_accounts WHERE id = %s AND status = 'active'", (account_id,))
        account = cursor.fetchone()
    finally:
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
    try:
        cursor.execute("UPDATE aws_accounts SET last_discovered_at = NOW() WHERE id = %s", (account_id,))
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    _write_audit(current_user["username"], "Account discovery triggered", f"{account['account_name']} ({account['account_id']})", role=current_user["role"].upper())
    return {"status": "discovery triggered", "account_id": account_id}