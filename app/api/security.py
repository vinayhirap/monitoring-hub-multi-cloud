# app/api/security.py
"""
Read-only-plus-console-link API for security_findings
(db/migrations/035_security_findings.sql, app/collector/cspm.py).
Findings themselves are entirely system-generated (like incidents --
see app/api/incidents.py's own docstring for the same reasoning) --
no create/edit/delete surface, gated on security.view only. The one
write side effect anywhere in this module is the audit-log entry the
console-url endpoint's underlying provider call makes for AWS (see
app/aws/federation.py's _write_console_open_audit) -- same pattern as
app/api/alerts.py's and app/api/admin/accounts.py's console-url
endpoints, not something specific to security findings.

2026-09-17 additions (see repo audit): an `account_id` filter (the
existing `accessible` scoping only ever restricted *which* accounts a
caller could see across every request -- there was no way to narrow
to just one on top of that, unlike every other list page in this
app), a lightweight `/accounts` endpoint to populate that filter's
dropdown without depending on the separate accounts.view permission,
and a `/{finding_id}/console-url` endpoint so a finding's Resource
column can deep-link straight into the correct account's console
instead of leaving "go find this yourself in AWS/Azure/GCP" implicit.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/security-findings", tags=["Security"])

# check_id -> console-link (service, resource_id, resource_name) resolver.
#
# AWS check_ids map to the `service` keys app/aws/federation.py's
# resource_console_destination() already understands (extended
# alongside this change for "security_group"/"iam_user" -- see that
# file). Azure check_ids need no mapping at all: cspm.py stores the
# full ARM resource ID as resource_id, and AzureProvider.get_console_url
# builds its link directly from that path, ignoring `service` entirely.
# GCP check_ids map to the service keys app/providers/gcp/provider.py's
# get_console_url() dispatches on (added "gce_firewall_rule" alongside
# this change; "gcs_bucket" already existed for the metrics/discovery
# side and is reused as-is here).
def _console_params_for_finding(check_id: str, resource_id: str):
    """Returns (service, resource_id, resource_name) for the
    provider.get_console_url() call. Falls back to (None, resource_id,
    None) for any check_id not listed here -- Azure's provider ignores
    `service` anyway, and an unrecognized AWS/GCP service degrades to
    that provider's generic account/project console home rather than
    erroring, same "never fake precision" fallback every provider's
    get_console_url already documents."""
    if check_id == "s3_bucket_public":
        return "s3", resource_id, None
    if check_id == "ebs_unencrypted":
        return "ebs", resource_id, None
    if check_id == "sg_open_to_world":
        return "security_group", resource_id, None
    if check_id == "iam_user_no_mfa":
        return "iam_user", resource_id, resource_id
    if check_id == "iam_stale_access_key":
        # cspm.py stores this as "username:key-id" -- see that file's
        # _check_stale_access_keys for why the username has to travel
        # with the finding at all.
        username = resource_id.split(":", 1)[0] if ":" in resource_id else resource_id
        return "iam_user", resource_id, username
    if check_id == "gcp_firewall_open_to_world":
        return "gce_firewall_rule", resource_id, resource_id
    if check_id == "gcp_gcs_bucket_public":
        return "gcs_bucket", resource_id, resource_id
    return None, resource_id, None


def _accessible_where(current_user: dict, where: list, params: list) -> bool:
    """Appends the account-scoping clause in place; returns False if
    the caller should get an empty result immediately (accessible ==
    [] -- a real scope with zero accounts in it, distinct from
    accessible is None meaning FULL_ACCESS)."""
    accessible = get_accessible_account_ids(current_user)
    if accessible is None:
        return True
    if not accessible:
        return False
    placeholders = ", ".join(["%s"] * len(accessible))
    where.append(f"f.aws_account_id IN ({placeholders})")
    params.extend(accessible)
    return True


@router.get("")
def list_findings(
    status: str = Query("open", pattern="^(open|resolved|all)$"),
    severity: str = Query(None, pattern="^(HIGH|MEDIUM|LOW)$"),
    account_id: int = Query(None, description="Filter to a single aws_accounts.id"),
    current_user: dict = Depends(require_permission("security.view")),
):
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        where = ["1=1"]
        params = []
        if status != "all":
            where.append("f.status = %s")
            params.append(status)
        if severity:
            where.append("f.severity = %s")
            params.append(severity)
        if not _accessible_where(current_user, where, params):
            return []
        if account_id is not None:
            where.append("f.aws_account_id = %s")
            params.append(account_id)

        cursor.execute(f"""
            SELECT f.*, acc.account_name, acc.provider AS account_provider
            FROM security_findings f
            JOIN aws_accounts acc ON acc.id = f.aws_account_id
            WHERE {' AND '.join(where)}
            ORDER BY FIELD(f.severity, 'HIGH', 'MEDIUM', 'LOW'), f.last_seen_at DESC
        """, tuple(params))
        return cursor.fetchall()
    finally:
        cursor.close(); conn.close()


@router.get("/accounts")
def list_findings_accounts(current_user: dict = Depends(require_permission("security.view"))):
    """
    Every account this caller can see findings for, regardless of
    whether it currently has any -- used to populate the Account
    filter dropdown on the frontend. Deliberately its own endpoint
    (gated on security.view, same as the rest of this router) rather
    than reusing GET /api/admin/accounts, which requires the separate
    accounts.view permission -- a viewer with security.view but not
    accounts.view should still get a working account filter here.
    """
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        where = ["status = 'active'"]
        params = []
        if accessible is not None:
            if not accessible:
                return []
            placeholders = ", ".join(["%s"] * len(accessible))
            where.append(f"id IN ({placeholders})")
            params.extend(accessible)

        cursor.execute(f"""
            SELECT id AS account_id, account_name, provider
            FROM aws_accounts
            WHERE {' AND '.join(where)}
            ORDER BY account_name
        """, tuple(params))
        return cursor.fetchall()
    finally:
        cursor.close(); conn.close()


@router.get("/summary")
def findings_summary(current_user: dict = Depends(require_permission("security.view"))):
    """
    Per-account open-finding counts by severity -- the "posture score"
    view for a fleet overview page, same shape as
    app/api/incidents.py's fleet-wide health summary.
    """
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        where = ["f.status = 'open'"]
        params = []
        if accessible is not None:
            if not accessible:
                return []
            placeholders = ", ".join(["%s"] * len(accessible))
            where.append(f"f.aws_account_id IN ({placeholders})")
            params.extend(accessible)

        cursor.execute(f"""
            SELECT acc.id AS account_id, acc.account_name,
                   SUM(f.severity = 'HIGH')   AS high_count,
                   SUM(f.severity = 'MEDIUM') AS medium_count,
                   SUM(f.severity = 'LOW')    AS low_count,
                   COUNT(*) AS total_open
            FROM security_findings f
            JOIN aws_accounts acc ON acc.id = f.aws_account_id
            WHERE {' AND '.join(where)}
            GROUP BY acc.id, acc.account_name
            ORDER BY high_count DESC, total_open DESC
        """, tuple(params))
        return cursor.fetchall()
    finally:
        cursor.close(); conn.close()


def _get_finding_and_account(finding_id: int):
    """Returns the finding row joined with its full aws_accounts row
    (dict, prefixed so nothing collides -- f_* / everything else is
    the account), or None if the finding doesn't exist."""
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT f.id AS f_id, f.check_id AS f_check_id, f.resource_id AS f_resource_id,
                   f.aws_account_id AS f_account_id, acc.*
            FROM security_findings f
            JOIN aws_accounts acc ON acc.id = f.aws_account_id
            WHERE f.id = %s
        """, (finding_id,))
        return cursor.fetchone()
    finally:
        cursor.close(); conn.close()


@router.post("/{finding_id}/console-url")
def get_finding_console_url(
    finding_id: int,
    current_user: dict = Depends(require_permission("security.view")),
):
    """
    Deep link into this finding's resource, in this finding's account,
    on whichever cloud that account actually lives on -- dispatched
    through the provider layer exactly the way app/api/alerts.py's
    /{alert_id}/console-url does, so this works for AWS, Azure and GCP
    findings alike without this endpoint needing to know the
    difference.

    POST, not GET: mirrors alerts.py/admin/accounts.py's identical
    reasoning -- AWS's leg of this call writes an audit-log entry as a
    side effect (app/aws/federation.py's _write_console_open_audit),
    which a GET would let a CSRF'd top-level navigation trigger under
    SameSite=Lax cookies.
    """
    row = _get_finding_and_account(finding_id)
    if not row:
        raise HTTPException(status_code=404, detail="Finding not found")

    # SECURITY: scope check BEFORE building any link -- same
    # account_id-not-in-accessible pattern as _require_alert_access in
    # app/api/alerts.py. Without this, any user holding the ROLE-level
    # security.view permission (not itself account-scoped) could pull
    # a live console link into an account entirely outside their
    # assigned scope just by iterating finding_id.
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None and row["f_account_id"] not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this finding")

    service, resource_id, resource_name = _console_params_for_finding(row["f_check_id"], row["f_resource_id"])

    try:
        from app.providers.registry import get_provider
        from app.aws.federation import NoConsoleCredentialsError
        provider = get_provider(row.get("provider") or "aws")
        url = provider.get_console_url(
            row, resource_id, row.get("default_region"),
            service=service, resource_name=resource_name,
            requested_by=current_user["username"],
        )
    except NoConsoleCredentialsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Failed to build console URL for security finding %s", finding_id)
        raise HTTPException(status_code=502, detail="Could not generate console link")

    return {"url": url, "account_id": row["f_account_id"]}
