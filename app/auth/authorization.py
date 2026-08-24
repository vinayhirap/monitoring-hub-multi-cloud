"""
app/auth/authorization.py

Centralized authorization service for scope-based RBAC. Nothing
outside this module should reason about roles/scopes directly — every
"can this user do X" or "what accounts/regions can this user see"
decision routes through here. Scattering this logic through
controllers is exactly what leads to one endpoint enforcing it
correctly and another forgetting.

Terminology:
  - "role" (admin/editor/viewer) answers WHAT a user can do.
  - "scope" (cloud/account/region/resource) answers WHERE they can do it.
  - "effective scope" = the union of all of a user's access_scopes rows.
    Admins have implicit full effective scope and never need rows here
    — this module always checks role == "admin" first and short-circuits.
"""
import json
from dataclasses import dataclass
from typing import Optional

from app.db import get_connection

FULL_ACCESS = "FULL_ACCESS"  # sentinel: this user's effective scope is "everything"

ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}


@dataclass
class ScopeGrant:
    id: int
    user_id: int
    cloud: str
    account_ref_id: Optional[int]     # None = every account under `cloud`
    regions: Optional[list]           # None/[] = every region
    resource_groups: Optional[list]
    resource_types: Optional[list]
    resource_ids: Optional[list]
    granted_by: int


def _parse_json_list(value):
    if value is None:
        return None
    if isinstance(value, list):
        return value
    parsed = json.loads(value)
    return parsed if parsed else None


def get_effective_scope(user: dict):
    """
    Returns FULL_ACCESS for admins, or list[ScopeGrant] for
    editor/viewer. An empty list means no access to anything — deny by
    default, per spec.
    """
    if user["role"] == "admin":
        return FULL_ACCESS

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, user_id, cloud, account_ref_id, regions, resource_groups, "
        "resource_types, resource_ids, granted_by FROM access_scopes WHERE user_id = %s",
        (user["id"],),
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    return [
        ScopeGrant(
            id=r["id"], user_id=r["user_id"], cloud=r["cloud"],
            account_ref_id=r["account_ref_id"],
            regions=_parse_json_list(r["regions"]),
            resource_groups=_parse_json_list(r["resource_groups"]),
            resource_types=_parse_json_list(r["resource_types"]),
            resource_ids=_parse_json_list(r["resource_ids"]),
            granted_by=r["granted_by"],
        )
        for r in rows
    ]


def get_accessible_account_ids(user: dict) -> Optional[set]:
    """
    None => FULL_ACCESS, caller should not filter by account at all.
    A set (possibly empty) => exactly the aws_accounts.id values this
    user may see. Empty set means "no accounts", not "unfiltered" —
    callers must treat None and set() differently.
    """
    scope = get_effective_scope(user)
    if scope == FULL_ACCESS:
        return None
    if not scope:
        return set()

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, provider FROM aws_accounts")
    all_accounts = cursor.fetchall()
    cursor.close()
    conn.close()

    explicit_ids = {g.account_ref_id for g in scope if g.account_ref_id is not None}
    wildcard_clouds = {g.cloud for g in scope if g.account_ref_id is None}

    return {
        acc["id"] for acc in all_accounts
        if acc["id"] in explicit_ids or acc["provider"] in wildcard_clouds
    }


def get_accessible_regions_for_account(user: dict, account_id: int) -> Optional[set]:
    """
    None => unrestricted (all regions). A set => only these regions.
    Caller must have already confirmed account_id is accessible at all
    via get_accessible_account_ids — this only answers the region
    question for an account the user can already see.
    """
    scope = get_effective_scope(user)
    if scope == FULL_ACCESS:
        return None

    regions = set()
    for g in scope:
        if g.account_ref_id is not None and g.account_ref_id != account_id:
            continue
        if not g.regions:
            return None  # this grant covers ALL regions for this account
        regions.update(g.regions)
    return regions


def can_manage_role(actor: dict, target_role: str) -> bool:
    """ADMIN manages editor + viewer. EDITOR manages VIEWER only. VIEWER manages nobody."""
    if actor["role"] == "admin":
        return True
    if actor["role"] == "editor":
        return target_role == "viewer"
    return False


def _covers(actor_list: Optional[list], requested_list: Optional[list]) -> bool:
    """
    actor_list None/empty => actor is unrestricted on this dimension,
    covers anything requested. Otherwise the requested list must be a
    non-empty, explicit subset of actor_list — requesting "no
    restriction" on a dimension the actor themselves doesn't have
    unrestricted would BE the escalation.
    """
    if not actor_list:
        return True
    if not requested_list:
        return False
    return set(requested_list).issubset(set(actor_list))


def _single_scope_within(requested: dict, actor_scope: list) -> bool:
    req_cloud = requested.get("cloud")
    req_account = requested.get("account_ref_id")

    for grant in actor_scope:
        if grant.cloud != req_cloud:
            continue
        if grant.account_ref_id is not None and grant.account_ref_id != req_account:
            continue
        # grant.account_ref_id is None => actor covers every account
        # under this cloud, so any requested account (including
        # "every account", i.e. req_account is also None) is covered.
        if not _covers(grant.regions, requested.get("regions")):
            continue
        if not _covers(grant.resource_groups, requested.get("resource_groups")):
            continue
        if not _covers(grant.resource_types, requested.get("resource_types")):
            continue
        if not _covers(grant.resource_ids, requested.get("resource_ids")):
            continue
        return True
    return False


def scope_within(requested_scopes: list, actor_scope) -> bool:
    """
    THE privilege-escalation gate. True iff every dict in
    requested_scopes is fully covered by some single grant in the
    actor's own effective scope. Admin (FULL_ACCESS) can grant
    anything. An editor can never grant a viewer more than the editor
    themselves has.
    """
    if actor_scope == FULL_ACCESS:
        return True
    if not requested_scopes:
        return False
    return all(_single_scope_within(req, actor_scope) for req in requested_scopes)


def validate_scope_shape(scope: dict, valid_account_ids_by_cloud: dict) -> Optional[str]:
    """
    Structural/referential validation of a single requested scope dict
    BEFORE it's checked against the actor's own scope. Returns an
    error string, or None if valid.
    valid_account_ids_by_cloud: {"aws": {1,2,3}, "azure": {...}, ...}
    """
    cloud = scope.get("cloud")
    if cloud not in ("aws", "azure", "gcp"):
        return f"Invalid cloud '{cloud}' \u2014 must be aws, azure, or gcp"

    account_ref_id = scope.get("account_ref_id")
    if account_ref_id is not None:
        if account_ref_id not in valid_account_ids_by_cloud.get(cloud, set()):
            return f"account_ref_id {account_ref_id} does not exist under cloud '{cloud}'"

    for field_name in ("regions", "resource_groups", "resource_types", "resource_ids"):
        value = scope.get(field_name)
        if value is not None and not isinstance(value, list):
            return f"{field_name} must be a list or null"

    return None


def serialize_scope(user: dict):
    """Used by GET /api/auth/me and the access-management UI. Returns
    the sentinel string for admins, or a JSON-serializable list for
    everyone else."""
    scope = get_effective_scope(user)
    if scope == FULL_ACCESS:
        return FULL_ACCESS
    return [
        {
            "id": g.id, "cloud": g.cloud, "account_ref_id": g.account_ref_id,
            "regions": g.regions, "resource_groups": g.resource_groups,
            "resource_types": g.resource_types, "resource_ids": g.resource_ids,
            "granted_by": g.granted_by,
        }
        for g in scope
    ]
