"""
app/auth/authorization.py

Centralized authorization service for scope-based RBAC. Nothing
outside this module should reason about roles/scopes/groups directly
-- every "can this user do X" or "what accounts/regions can this user
see" decision routes through here. Scattering this logic through
controllers is exactly what leads to one endpoint enforcing it
correctly and another forgetting.

Terminology:
  - "role" (admin/editor/viewer) answers WHAT a user can do.
  - "scope" (cloud/account/region/resource) answers WHERE they can do it.
  - "group" (L1/L2/L3 org unit) is a reusable BUNDLE of scope, applied
    to every user who is a member of it, or a member of one of its
    descendant groups. See get_effective_scope for the inheritance
    rule.
  - "effective scope" = the union of a user's own access_scopes rows
    PLUS every group_policies row attached to any group the user
    belongs to or any ANCESTOR of that group. Admins have implicit
    full effective scope and never need rows here -- this module
    always checks role == "admin" first and short-circuits.

GROUP HIERARCHY (L1 / L2 / L3)
  L1 -- top-level org unit, no parent (e.g. "APAC", "Platform-Eng").
  L2 -- child of exactly one L1 (e.g. "APAC" -> "India-NOC").
  L3 -- child of exactly one L2 (e.g. "India-NOC" -> "L3-OnCall").

  A user placed in an L3 group inherits that L3 group's OWN policy
  PLUS its L2 parent's PLUS its L1 grandparent's -- ADDITIVE (union),
  not restrictive (not an SCP-style narrowing). This mirrors how
  AWS IAM Identity Center permission sets attached at different OU
  levels all apply to a principal beneath them, and matches the
  "tiered support" mental model (L1/L2/L3) this was built for: an L3
  on-call engineer should see everything their L2 team and L1 org see,
  plus whatever extra the L3 tier itself was granted -- never less.

  Every group policy, at any level, is itself account/region specific
  (same shape as a user's own access_scopes row) -- there is no
  "whole cloud, every account, every region" grant implied merely by
  being high in the hierarchy. An L1 group with no policies attached
  grants nothing on its own; it only becomes meaningful once policies
  are attached to it (or to one of its descendants).
"""
import json
from dataclasses import dataclass
from typing import Optional

from app.db import get_connection

FULL_ACCESS = "FULL_ACCESS"  # sentinel: this user's effective scope is "everything"

ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}

GROUP_LEVELS = ("L1", "L2", "L3")
# What level a group's parent MUST be, keyed by the child's own level.
# L1 -> None (top of the tree, no parent allowed).
GROUP_PARENT_LEVEL = {"L1": None, "L2": "L1", "L3": "L2"}

# The role a user is given automatically when added as a member of a
# group at each level. L1 = Viewer (least access), L2 = Editor (mid),
# L3 = Admin (full access) -- referenced by app/api/admin/groups.py's
# add_group_members() and mirrored client-side in UserManagement.jsx
# purely for instant UI feedback; this dict here is the one and only
# authoritative source. (Previously referenced from three places in
# this codebase but never actually defined -- every group-membership
# write has been crashing with AttributeError until this fix.)
GROUP_LEVEL_ROLE = {"L1": "viewer", "L2": "editor", "L3": "admin"}


@dataclass
class ScopeGrant:
    id: int
    user_id: Optional[int]
    cloud: str
    account_ref_id: Optional[int]     # None = every account under `cloud`
    regions: Optional[list]           # None/[] = every region
    resource_groups: Optional[list]
    resource_types: Optional[list]
    resource_ids: Optional[list]
    granted_by: int
    # Provenance -- purely informational, never consulted by the
    # containment/allow logic below (scope_within etc. treat a
    # group-inherited grant exactly the same as a directly-assigned
    # one). Lets callers (serialize_scope, the admin UI) explain WHY a
    # user has a given grant without a second query.
    source: str = "user"              # "user" | "group"
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    group_level: Optional[str] = None


def _parse_json_list(value):
    if value is None:
        return None
    if isinstance(value, list):
        return value
    parsed = json.loads(value)
    return parsed if parsed else None


# ─────────────────────────────────────────────────────────────────────────
# Group hierarchy helpers
# ─────────────────────────────────────────────────────────────────────────

def get_group(conn, group_id: int) -> Optional[dict]:
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, name, level, parent_group_id, description, created_by "
        "FROM org_groups WHERE id = %s",
        (group_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    return row


def get_group_chain(conn, group_id: int) -> list:
    """
    Returns [self, parent, grandparent, ...] up to the L1 root,
    closest first. Walked in a plain Python loop rather than a
    recursive CTE -- the hierarchy is capped at exactly three levels
    by construction (L1/L2/L3, enforced in validate_group_level_and_parent),
    so a loop is simpler and doesn't depend on recursive-CTE support
    being available in every MySQL deployment this runs against. The
    `seen` guard is a defensive backstop against a corrupted
    parent_group_id cycle, not something that should ever trigger.
    """
    chain = []
    current_id = group_id
    seen = set()
    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        g = get_group(conn, current_id)
        if not g:
            break
        chain.append(g)
        current_id = g["parent_group_id"]
    return chain


def get_user_group_memberships(conn, user_id: int) -> list:
    """Direct (non-inherited) group ids this user is a member of."""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT group_id FROM user_group_memberships WHERE user_id = %s",
        (user_id,),
    )
    rows = cursor.fetchall()
    cursor.close()
    return [r["group_id"] for r in rows]


def get_user_effective_groups(user_id: int) -> list:
    """
    Every group this user's group-derived access is drawn from: each
    group they're a direct member of, PLUS every ancestor of each of
    those groups, deduplicated. Shared by get_effective_scope and the
    /api/groups/users/{id}/groups introspection endpoint so the two
    can never drift apart.
    """
    conn = get_connection()
    try:
        direct_ids = get_user_group_memberships(conn, user_id)
        by_id = {}
        for gid in direct_ids:
            for g in get_group_chain(conn, gid):
                by_id[g["id"]] = g
        return list(by_id.values())
    finally:
        conn.close()


def _group_policy_rows(conn, group_ids: list) -> list:
    if not group_ids:
        return []
    placeholders = ",".join(["%s"] * len(group_ids))
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        f"SELECT gp.id, gp.group_id, gp.cloud, gp.account_ref_id, gp.regions, "
        f"gp.resource_groups, gp.resource_types, gp.resource_ids, gp.granted_by, "
        f"og.name AS group_name, og.level AS group_level "
        f"FROM group_policies gp JOIN org_groups og ON og.id = gp.group_id "
        f"WHERE gp.group_id IN ({placeholders})",
        tuple(group_ids),
    )
    rows = cursor.fetchall()
    cursor.close()
    return rows


def validate_group_level_and_parent(conn, level: str, parent_group_id: Optional[int]) -> Optional[str]:
    """
    Structural validation for creating a group. Returns an error
    string, or None if valid.
      - level must be one of L1/L2/L3.
      - L1 groups must have no parent.
      - L2 groups must have a parent that exists and is an L1.
      - L3 groups must have a parent that exists and is an L2.
    This is what keeps the hierarchy exactly three levels deep and
    prevents e.g. an L3 being parented directly under an L1, which
    would break the "inherit everything above you" guarantee callers
    of get_effective_scope rely on.
    """
    if level not in GROUP_LEVELS:
        return f"Invalid level '{level}' \u2014 must be one of {', '.join(GROUP_LEVELS)}"

    expected_parent_level = GROUP_PARENT_LEVEL[level]
    if expected_parent_level is None:
        if parent_group_id is not None:
            return "L1 groups are top-level and cannot have a parent_group_id"
        return None

    if parent_group_id is None:
        return f"{level} groups require a parent_group_id (an existing {expected_parent_level} group)"

    parent = get_group(conn, parent_group_id)
    if not parent:
        return f"parent_group_id {parent_group_id} does not exist"
    if parent["level"] != expected_parent_level:
        return (
            f"{level} groups must be parented under an {expected_parent_level} group "
            f"(parent_group_id {parent_group_id} is level {parent['level']})"
        )
    return None


# ─────────────────────────────────────────────────────────────────────────
# Effective scope resolution
# ─────────────────────────────────────────────────────────────────────────

def get_effective_scope(user: dict):
    """
    Returns FULL_ACCESS for admins, or list[ScopeGrant] for
    editor/viewer -- the UNION of:
      1. the user's own access_scopes rows (source="user"), and
      2. every group_policies row attached to any group the user is a
         direct member of, OR to any ANCESTOR of that group
         (source="group") -- this is the L1/L2/L3 inheritance:
         membership in an L3 group pulls in that L3's own policy plus
         its L2 parent's plus its L1 grandparent's.
    An empty combined list still means no access to anything -- deny
    by default is unchanged from Phase 1.
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
    own_rows = cursor.fetchall()
    cursor.close()

    grants = [
        ScopeGrant(
            id=r["id"], user_id=r["user_id"], cloud=r["cloud"],
            account_ref_id=r["account_ref_id"],
            regions=_parse_json_list(r["regions"]),
            resource_groups=_parse_json_list(r["resource_groups"]),
            resource_types=_parse_json_list(r["resource_types"]),
            resource_ids=_parse_json_list(r["resource_ids"]),
            granted_by=r["granted_by"], source="user",
        )
        for r in own_rows
    ]

    direct_group_ids = get_user_group_memberships(conn, user["id"])
    inherited_group_ids = set()
    for gid in direct_group_ids:
        for g in get_group_chain(conn, gid):
            inherited_group_ids.add(g["id"])

    for r in _group_policy_rows(conn, list(inherited_group_ids)):
        grants.append(ScopeGrant(
            id=r["id"], user_id=user["id"], cloud=r["cloud"],
            account_ref_id=r["account_ref_id"],
            regions=_parse_json_list(r["regions"]),
            resource_groups=_parse_json_list(r["resource_groups"]),
            resource_types=_parse_json_list(r["resource_types"]),
            resource_ids=_parse_json_list(r["resource_ids"]),
            granted_by=r["granted_by"], source="group",
            group_id=r["group_id"], group_name=r["group_name"],
            group_level=r["group_level"],
        ))

    conn.close()
    return grants


def get_accessible_account_ids(user: dict) -> Optional[set]:
    """
    None => FULL_ACCESS, caller should not filter by account at all.
    A set (possibly empty) => exactly the aws_accounts.id values this
    user may see (via their own grants and/or group membership).
    Empty set means "no accounts", not "unfiltered" -- callers must
    treat None and set() differently.
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
    via get_accessible_account_ids -- this only answers the region
    question for an account the user can already see. Region grants
    from group membership are folded in automatically since this reads
    from get_effective_scope.
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
    non-empty, explicit subset of actor_list -- requesting "no
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
    actor's own effective scope (their direct grants AND anything they
    have via group membership -- get_effective_scope already merged
    those before this is called). Admin (FULL_ACCESS) can grant
    anything. An editor can never grant a viewer more than the editor
    themselves effectively has, regardless of whether the editor's own
    access came from a direct grant or from a group.
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
    error string, or None if valid. Used identically for a user's own
    access_scopes rows and for group_policies rows -- the two tables
    are schema-identical on purpose.
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
    """Used by GET /api/auth/me, GET /api/users/{id}/access, and the
    access-management UI. Returns the sentinel string for admins, or a
    JSON-serializable list for everyone else -- each entry now also
    says WHERE it came from (a direct grant, or a specific group at a
    specific level), so the UI can render "via India-NOC (L2)" instead
    of just a flat, unexplained list of scopes."""
    scope = get_effective_scope(user)
    if scope == FULL_ACCESS:
        return FULL_ACCESS
    return [
        {
            "id": g.id, "cloud": g.cloud, "account_ref_id": g.account_ref_id,
            "regions": g.regions, "resource_groups": g.resource_groups,
            "resource_types": g.resource_types, "resource_ids": g.resource_ids,
            "granted_by": g.granted_by, "source": g.source,
            "group_id": g.group_id, "group_name": g.group_name,
            "group_level": g.group_level,
        }
        for g in scope
    ]
