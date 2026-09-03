#!/usr/bin/env python3
"""
apply_org_group_rbac.py — Phase 2 of the RBAC project: hierarchical
ORGANIZATION GROUPS (L1 / L2 / L3), AWS-Organizations style.

WHAT THIS ADDS ON TOP OF PHASE 1 (access_scopes / authorization.py)
  - db/migrations/013_org_group_rbac.sql (new): three additive tables.
      org_groups              L1/L2/L3 hierarchy (self-referencing FK)
      group_policies          account/region-specific policy attached
                               to a group — schema-identical to
                               access_scopes, just keyed by group_id
                               instead of user_id
      user_group_memberships  which users belong to which group(s)

  - app/auth/authorization.py (rewritten, superset of Phase 1): adds
    get_group / get_group_chain / get_user_group_memberships /
    get_user_effective_groups / validate_group_level_and_parent, and
    changes get_effective_scope() to be the UNION of a user's own
    access_scopes rows AND every group_policies row attached to any
    group they belong to OR any ANCESTOR of that group. Everything
    downstream (get_accessible_account_ids, get_accessible_regions_
    for_account, scope_within, serialize_scope, and therefore every
    existing endpoint in users.py / auth.py / alerts.py / etc.) picks
    up group-derived access automatically — nothing outside this one
    module had to change to become group-aware.

  - app/api/admin/groups.py (new): admin-only CRUD for groups, policy
    grants at any level, and membership management —
      POST   /api/groups                          create L1/L2/L3 group
      GET    /api/groups                           list all groups
      GET    /api/groups/{id}                       detail incl. chain,
                                                      own policies, members
      DELETE /api/groups/{id}                       delete (409 if it
                                                      has children)
      POST   /api/groups/{id}/policies               attach account/
                                                      region-specific
                                                      policy grant(s)
      DELETE /api/groups/policies/{policy_id}        revoke one grant
      POST   /api/groups/{id}/members                add user(s)
      DELETE /api/groups/{id}/members/{user_id}       remove a user
      GET    /api/groups/users/{user_id}/groups       a user's direct +
                                                      inherited groups
                                                      and resolved
                                                      effective scope

  - app/main.py: registers the new router.

INHERITANCE MODEL (see authorization.py docstring for the full case)
  L1 (top-level org unit) -> L2 (child of one L1) -> L3 (child of one
  L2). A user in an L3 group inherits that L3's own policy PLUS its L2
  parent's PLUS its L1 grandparent's — ADDITIVE (union), never a
  restriction. Every policy at every level is itself account- and
  region-specific — being high in the hierarchy grants nothing on its
  own; only attached policies do.

PREREQUISITE — Phase 1 must already be applied (it ships with this
repo as of the multi-cloud + RBAC build; this script checks for it and
aborts with a clear message if access_scopes / app/auth/authorization.py
aren't present yet, rather than half-applying on top of nothing).

Usage:
    python apply_org_group_rbac.py --dry-run
    python apply_org_group_rbac.py                  # code + DB migration
    python apply_org_group_rbac.py --skip-db         # code changes only
    python apply_org_group_rbac.py --skip-db-backup  # skip mysqldump step
"""
import argparse
import datetime
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-org-group-rbac"
MIGRATION_SQL_PATH = REPO_ROOT / "db" / "migrations" / "013_org_group_rbac.sql"
DB_BACKUP_DIR = REPO_ROOT / "db" / "backups"

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "monitoring_hub")


class PatchError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────
# File contents
# ─────────────────────────────────────────────────────────────────────────
MIGRATION_SQL = r'''-- db/migrations/013_org_group_rbac.sql
--
-- Phase 2 of the RBAC project: hierarchical ORGANIZATION GROUPS
-- (L1 / L2 / L3), modeled after AWS Organizations OUs / IAM Identity
-- Center permission sets.
--
-- WHY THIS EXISTS
-- Phase 1 (access_scopes, migration 011) gave every user their own
-- flat list of scope grants. That works, but at real org scale
-- ("give everyone on the India NOC team read access to ap-south-1,
-- give the platform team owners access to everything") it means
-- re-granting the same account/region combination to every single
-- user individually, and redoing it again whenever someone new joins.
--
-- This migration adds a GROUP layer that sits ABOVE users:
--   org_groups              -- the L1/L2/L3 hierarchy itself
--   group_policies          -- account/region-specific policy attached
--                               to a group (same shape as access_scopes)
--   user_group_memberships  -- which users belong to which group(s)
--
-- INHERITANCE MODEL (see app/auth/authorization.py for the resolver)
-- L1 is a top-level org unit (e.g. "APAC", "Platform-Eng"). L2 is a
-- child of exactly one L1 (e.g. "APAC > India-NOC"). L3 is a child of
-- exactly one L2 (e.g. "APAC > India-NOC > L3-OnCall"). A user placed
-- in an L3 group inherits that L3 group's OWN policy PLUS its L2
-- parent's policy PLUS its L1 grandparent's policy -- ADDITIVE
-- (union), the same way multiple IAM Identity Center permission sets
-- attached at different OU levels all apply to a principal beneath
-- them. This is a permission-INHERITANCE hierarchy, not an SCP-style
-- restriction hierarchy: a child group only ever has AT LEAST as much
-- access as its parent, never less, by construction.
--
-- Account/region SPECIFICITY is enforced exactly the way Phase 1 does
-- it for users: account_ref_id NULL = every account under `cloud`,
-- regions NULL/[] = every region under that account. group_policies
-- is intentionally schema-identical to access_scopes so the same
-- validation / containment logic (authz.validate_scope_shape) covers
-- both without duplicating it.
--
-- Purely additive: three new tables, zero ALTERs on existing tables.

CREATE TABLE IF NOT EXISTS org_groups (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    name             VARCHAR(150) NOT NULL,
    level            ENUM('L1','L2','L3') NOT NULL,
    parent_group_id  BIGINT NULL,
    description      VARCHAR(500) NULL,
    created_by       BIGINT NOT NULL,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_org_groups_parent
        FOREIGN KEY (parent_group_id) REFERENCES org_groups(id) ON DELETE RESTRICT,
    CONSTRAINT fk_org_groups_created_by
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT,

    UNIQUE KEY uq_org_groups_name (name),
    INDEX idx_org_groups_parent (parent_group_id),
    INDEX idx_org_groups_level (level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ON DELETE RESTRICT on parent_group_id: you cannot drop an L1/L2
-- group while children still point at it. app/api/admin/groups.py
-- also checks this up front for a clean 409 instead of a raw FK
-- error, but this constraint is the real backstop.

CREATE TABLE IF NOT EXISTS group_policies (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    group_id        BIGINT NOT NULL,
    cloud           ENUM('aws','azure','gcp') NOT NULL,
    account_ref_id  BIGINT NULL,
    regions         JSON NULL,
    resource_groups JSON NULL,
    resource_types  JSON NULL,
    resource_ids    JSON NULL,
    granted_by      BIGINT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_group_policies_group
        FOREIGN KEY (group_id) REFERENCES org_groups(id) ON DELETE CASCADE,
    CONSTRAINT fk_group_policies_account
        FOREIGN KEY (account_ref_id) REFERENCES aws_accounts(id) ON DELETE CASCADE,
    CONSTRAINT fk_group_policies_granted_by
        FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE RESTRICT,

    INDEX idx_group_policies_group (group_id),
    INDEX idx_group_policies_account (account_ref_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_group_memberships (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    group_id     BIGINT NOT NULL,
    assigned_by  BIGINT NOT NULL,
    assigned_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_ugm_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_ugm_group
        FOREIGN KEY (group_id) REFERENCES org_groups(id) ON DELETE CASCADE,
    CONSTRAINT fk_ugm_assigned_by
        FOREIGN KEY (assigned_by) REFERENCES users(id) ON DELETE RESTRICT,

    UNIQUE KEY uq_ugm_user_group (user_id, group_id),
    INDEX idx_ugm_user (user_id),
    INDEX idx_ugm_group (group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
'''

AUTHORIZATION_PY = r'''"""
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
'''

GROUPS_PY = r'''# app/api/admin/groups.py
"""
app/api/admin/groups.py

Organization-group management: an AWS-Organizations-style L1/L2/L3
hierarchy, account/region-specific policies attached at any level, and
user membership. Every "what can a user actually see" question this
produces is resolved through app.auth.authorization.get_effective_scope
-- this module only manages the STRUCTURE (groups, policies,
membership); it never reasons about effective access itself, the same
discipline app/api/admin/users.py follows for individual grants.

Design decisions:
  - Group CRUD, policy grants, and membership changes are admin-only.
    Unlike users.py's "editor may manage viewers within their own
    scope" delegation, org-UNIT structure itself (creating an L1,
    reparenting an L2, attaching a policy at the L1/L2 level that many
    people will inherit) is treated like the AWS Organizations
    management account -- one root of authority, not delegated. This
    can be loosened later the same way editor delegation was added
    for individual users, if the org needs it.
  - Because only admins manage groups, and admins are FULL_ACCESS,
    there is no privilege-escalation surface on group policy grants
    the way there is for editor-created user grants -- but
    validate_scope_shape (structural + referential integrity against
    real accounts) still applies, so a group can't be pointed at an
    account_ref_id that doesn't exist or a malformed regions list.
  - Deleting a group with children is refused (409) rather than
    cascading. Silently deleting an entire L2/L3 subtree because
    someone deleted the L1 by mistake is exactly the kind of
    org-structure footgun this endpoint set exists to prevent; the DB
    FK (ON DELETE RESTRICT on parent_group_id) is the real backstop,
    this is just a clean error before hitting it.
  - Deleting a group DOES cascade its own group_policies and
    user_group_memberships rows (ON DELETE CASCADE) -- removing a leaf
    group is expected to remove what was attached directly to it;
    users who were members simply stop inheriting that group's policy
    and fall back to whatever else they have.
"""
from fastapi import APIRouter, HTTPException, Body, Depends
from app.db import get_connection
from app.auth.deps import require_role
from app.auth import authorization as authz
import datetime
import json

router = APIRouter(prefix="/api/groups", tags=["Organization Groups"])


def _serialize(obj):
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    return obj


def _write_audit(actor: str, action: str, detail: str):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        payload = json.dumps({"detail": detail, "role": "ADMIN"})
        cursor.execute(
            "INSERT INTO audit_logs (actor, action, payload) VALUES (%s, %s, %s)",
            (actor, action, payload),
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Audit log write error: {e}")


def _account_ids_by_cloud(conn) -> dict:
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, provider FROM aws_accounts")
    rows = cursor.fetchall()
    cursor.close()
    result = {"aws": set(), "azure": set(), "gcp": set()}
    for r in rows:
        result.setdefault(r["provider"], set()).add(r["id"])
    return result


def _group_own_policies(conn, group_id: int) -> list:
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, group_id, cloud, account_ref_id, regions, resource_groups, "
        "resource_types, resource_ids, granted_by, created_at FROM group_policies "
        "WHERE group_id = %s ORDER BY created_at ASC",
        (group_id,),
    )
    rows = cursor.fetchall()
    cursor.close()
    for r in rows:
        for field_name in ("regions", "resource_groups", "resource_types", "resource_ids"):
            r[field_name] = authz._parse_json_list(r[field_name])
    return rows


def _group_members(conn, group_id: int) -> list:
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT u.id, u.username, u.role, ugm.assigned_at FROM user_group_memberships ugm "
        "JOIN users u ON u.id = ugm.user_id WHERE ugm.group_id = %s ORDER BY ugm.assigned_at ASC",
        (group_id,),
    )
    rows = cursor.fetchall()
    cursor.close()
    return rows


def _serialize_group(conn, g: dict, include_details: bool = False) -> dict:
    out = {
        "id": g["id"], "name": g["name"], "level": g["level"],
        "parent_group_id": g["parent_group_id"], "description": g["description"],
        "created_by": g["created_by"], "created_at": g["created_at"],
    }
    if include_details:
        chain = authz.get_group_chain(conn, g["id"])
        out["chain"] = [{"id": c["id"], "name": c["name"], "level": c["level"]} for c in chain]
        out["own_policies"] = _group_own_policies(conn, g["id"])
        out["members"] = _group_members(conn, g["id"])
    return out


# Every endpoint below requires an authenticated admin (structure
# changes) or admin/editor (read-only listing) -- see the module
# docstring for why group management itself isn't delegated to
# editors the way individual user-scope grants are.


@router.get("")
def list_groups(current_user: dict = Depends(require_role("admin", "editor"))):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, name, level, parent_group_id, description, created_by, created_at "
        "FROM org_groups ORDER BY level ASC, name ASC"
    )
    rows = cursor.fetchall()
    result = [_serialize_group(conn, r) for r in rows]
    conn.close()
    return _serialize(result)


@router.get("/users/{user_id}/groups")
def get_user_groups(user_id: int, current_user: dict = Depends(require_role("admin", "editor"))):
    """
    Direct memberships AND the fully-resolved inherited scope for this
    user in one call -- lets the admin UI show e.g. "member of
    L3-OnCall, inheriting from India-NOC (L2) and APAC (L1)" plus the
    actual resolved account/region access, without three round trips.
    Registered before /{group_id} below so "users" is never mistaken
    for a group id.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, role FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()
    cursor.close()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT ugm.group_id, og.name, og.level FROM user_group_memberships ugm "
        "JOIN org_groups og ON og.id = ugm.group_id WHERE ugm.user_id = %s",
        (user_id,),
    )
    direct = cursor.fetchall()
    cursor.close()
    conn.close()

    effective_groups = authz.get_user_effective_groups(user_id)
    return _serialize({
        "user_id": user_id,
        "username": user["username"],
        "direct_memberships": direct,
        "effective_groups": [
            {"id": g["id"], "name": g["name"], "level": g["level"]} for g in effective_groups
        ],
        "effective_scope": authz.serialize_scope(user),
    })


@router.post("")
def create_group(payload: dict = Body(...), current_user: dict = Depends(require_role("admin"))):
    name = (payload.get("name") or "").strip()
    level = (payload.get("level") or "").strip().upper()
    parent_group_id = payload.get("parent_group_id")
    description = (payload.get("description") or "").strip() or None

    if not name:
        raise HTTPException(status_code=400, detail="name required")

    conn = get_connection()
    err = authz.validate_group_level_and_parent(conn, level, parent_group_id)
    if err:
        conn.close()
        raise HTTPException(status_code=400, detail=err)

    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO org_groups (name, level, parent_group_id, description, created_by) "
            "VALUES (%s, %s, %s, %s, %s)",
            (name, level, parent_group_id, description, current_user["id"]),
        )
        conn.commit()
        new_id = cursor.lastrowid
    except Exception as e:
        if "Duplicate" in str(e) or "1062" in str(e):
            raise HTTPException(status_code=409, detail=f"Group '{name}' already exists")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()

    _write_audit(
        current_user["username"], "Group created",
        f"{name} ({level})" + (f", parent #{parent_group_id}" if parent_group_id else ""),
    )
    return {
        "status": "created", "id": new_id, "name": name,
        "level": level, "parent_group_id": parent_group_id,
    }


@router.get("/{group_id}")
def get_group_detail(group_id: int, current_user: dict = Depends(require_role("admin", "editor"))):
    conn = get_connection()
    g = authz.get_group(conn, group_id)
    if not g:
        conn.close()
        raise HTTPException(status_code=404, detail="Group not found")
    result = _serialize_group(conn, g, include_details=True)
    conn.close()
    return _serialize(result)


@router.delete("/{group_id}")
def delete_group(group_id: int, current_user: dict = Depends(require_role("admin"))):
    conn = get_connection()
    g = authz.get_group(conn, group_id)
    if not g:
        conn.close()
        raise HTTPException(status_code=404, detail="Group not found")

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM org_groups WHERE parent_group_id = %s", (group_id,))
    child_count = cursor.fetchone()[0]
    if child_count:
        cursor.close()
        conn.close()
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete '{g['name']}' \u2014 it has {child_count} child group(s). "
                "Delete or reparent them first."
            ),
        )

    cursor.execute("DELETE FROM org_groups WHERE id = %s", (group_id,))  # policies + memberships cascade via FK
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(current_user["username"], "Group deleted", f"{g['name']} ({g['level']}) removed")
    return {"status": "deleted", "id": group_id, "name": g["name"]}


@router.post("/{group_id}/policies")
def add_group_policy(group_id: int, payload: dict = Body(...), current_user: dict = Depends(require_role("admin"))):
    """
    Attach one or more account/region-specific policy grants to this
    group. Every user who is a direct member of this group, OR a
    direct member of any descendant group, inherits every one of
    these grants (union with whatever else they have).
    """
    scopes = payload.get("scopes") or []
    if not scopes:
        raise HTTPException(status_code=400, detail="scopes required")

    conn = get_connection()
    g = authz.get_group(conn, group_id)
    if not g:
        conn.close()
        raise HTTPException(status_code=404, detail="Group not found")

    valid_accounts = _account_ids_by_cloud(conn)
    for s in scopes:
        err = authz.validate_scope_shape(s, valid_accounts)
        if err:
            conn.close()
            raise HTTPException(status_code=400, detail=f"Invalid scope: {err}")

    cursor = conn.cursor()
    inserted_ids = []
    for s in scopes:
        cursor.execute(
            "INSERT INTO group_policies "
            "(group_id, cloud, account_ref_id, regions, resource_groups, resource_types, resource_ids, granted_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                group_id, s["cloud"], s.get("account_ref_id"),
                json.dumps(s["regions"]) if s.get("regions") else None,
                json.dumps(s["resource_groups"]) if s.get("resource_groups") else None,
                json.dumps(s["resource_types"]) if s.get("resource_types") else None,
                json.dumps(s["resource_ids"]) if s.get("resource_ids") else None,
                current_user["id"],
            ),
        )
        inserted_ids.append(cursor.lastrowid)
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(
        current_user["username"], "Group policy granted",
        f"{g['name']}: +{len(inserted_ids)} scope grant(s)",
    )
    return {"status": "updated", "group_id": group_id, "policy_ids": inserted_ids}


@router.delete("/policies/{policy_id}")
def delete_group_policy(policy_id: int, current_user: dict = Depends(require_role("admin"))):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT gp.id, gp.group_id, og.name AS group_name FROM group_policies gp "
        "JOIN org_groups og ON og.id = gp.group_id WHERE gp.id = %s",
        (policy_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Group policy not found")

    cursor = conn.cursor()
    cursor.execute("DELETE FROM group_policies WHERE id = %s", (policy_id,))
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(current_user["username"], "Group policy revoked", f"{row['group_name']}: policy #{policy_id} removed")
    return {"status": "revoked", "policy_id": policy_id}


@router.post("/{group_id}/members")
def add_group_members(group_id: int, payload: dict = Body(...), current_user: dict = Depends(require_role("admin"))):
    user_ids = payload.get("user_ids") or []
    if not user_ids:
        raise HTTPException(status_code=400, detail="user_ids required")

    conn = get_connection()
    g = authz.get_group(conn, group_id)
    if not g:
        conn.close()
        raise HTTPException(status_code=404, detail="Group not found")

    cursor = conn.cursor(dictionary=True)
    placeholders = ",".join(["%s"] * len(user_ids))
    cursor.execute(f"SELECT id FROM users WHERE id IN ({placeholders})", tuple(user_ids))
    existing_ids = {r["id"] for r in cursor.fetchall()}
    cursor.close()
    missing = set(user_ids) - existing_ids
    if missing:
        conn.close()
        raise HTTPException(status_code=404, detail=f"User id(s) not found: {sorted(missing)}")

    cursor = conn.cursor()
    added, already = [], []
    for uid in user_ids:
        try:
            cursor.execute(
                "INSERT INTO user_group_memberships (user_id, group_id, assigned_by) VALUES (%s, %s, %s)",
                (uid, group_id, current_user["id"]),
            )
            added.append(uid)
        except Exception as e:
            if "Duplicate" in str(e) or "1062" in str(e):
                already.append(uid)
            else:
                conn.rollback()
                cursor.close()
                conn.close()
                raise HTTPException(status_code=500, detail=str(e))
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(
        current_user["username"], "Group membership added",
        f"{g['name']}: +{len(added)} user(s)" + (f", {len(already)} already member" if already else ""),
    )
    return {"status": "updated", "group_id": group_id, "added": added, "already_member": already}


@router.delete("/{group_id}/members/{user_id}")
def remove_group_member(group_id: int, user_id: int, current_user: dict = Depends(require_role("admin"))):
    conn = get_connection()
    g = authz.get_group(conn, group_id)
    if not g:
        conn.close()
        raise HTTPException(status_code=404, detail="Group not found")

    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM user_group_memberships WHERE group_id = %s AND user_id = %s",
        (group_id, user_id),
    )
    removed = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()

    if not removed:
        raise HTTPException(status_code=404, detail="User is not a member of this group")

    _write_audit(current_user["username"], "Group membership removed", f"{g['name']}: user #{user_id} removed")
    return {"status": "removed", "group_id": group_id, "user_id": user_id}
'''

NEW_FILES = [
    ("db/migrations/013_org_group_rbac.sql", MIGRATION_SQL),
    ("app/api/admin/groups.py", GROUPS_PY),
]

# authorization.py already exists (Phase 1) — full rewrite, not a
# fresh file. old_anchor is checked in preflight() to confirm the
# Phase 1 version is what's actually on disk before it gets replaced.
FULL_REWRITES = [
    (
        "app/auth/authorization.py",
        'def scope_within(requested_scopes: list, actor_scope) -> bool:',
        AUTHORIZATION_PY,
    ),
]

PATCHES = [
    (
        "app/main.py",
        [
            (
                "from app.api.admin.users    import router as admin_users_router\n",
                "from app.api.admin.users    import router as admin_users_router\n"
                "from app.api.admin.groups   import router as admin_groups_router\n",
            ),
            (
                "app.include_router(admin_users_router)\n",
                "app.include_router(admin_users_router)\n"
                "app.include_router(admin_groups_router)\n",
            ),
        ],
    ),
]



# ─────────────────────────────────────────────────────────────────────────
# Preflight / apply / validate — same pattern as apply_phase1_authorization_service.py
# ─────────────────────────────────────────────────────────────────────────

def preflight():
    print("=== Pre-flight: checking prerequisites and anchors ===")
    problems = []

    access_scopes_migration = REPO_ROOT / "db" / "migrations" / "011_access_scopes.sql"
    authz_path = REPO_ROOT / "app" / "auth" / "authorization.py"
    if not authz_path.exists():
        problems.append(
            "MISSING: app/auth/authorization.py — Phase 1 (RBAC scopes) does not "
            "appear to be applied yet. Run apply_access_scopes_migration.py and "
            "apply_phase1_authorization_service.py first."
        )
    if not access_scopes_migration.exists():
        problems.append(f"MISSING: {access_scopes_migration.relative_to(REPO_ROOT)}")

    if problems:
        print("\n".join(problems))
        raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")

    for rel_path, content in NEW_FILES:
        full_path = REPO_ROOT / rel_path
        if full_path.exists():
            print(f"  (already exists, will skip creating) {rel_path}")
        else:
            print(f"  OK  {rel_path}: will be created")

    for rel_path, old_anchor, _new in FULL_REWRITES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        if old_anchor not in text:
            problems.append(f"{rel_path}: expected anchor not found")
        else:
            print(f"  OK  {rel_path}: ready for full rewrite")

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        for old, _new in replacements:
            count = text.count(old)
            if count == 0:
                problems.append(f"{rel_path}: anchor not found (0 matches) — {old[:60]!r}")
            elif count > 1:
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1")
            else:
                print(f"  OK  {rel_path}: anchor matched exactly once")

    if problems:
        print("\n".join(problems))

        def _already(rel, new_text):
            p = REPO_ROOT / rel
            return p.exists() and new_text in p.read_text(encoding="utf-8")

        already_applied = (
            all(_already(rel, new) for rel, _anchor, new in FULL_REWRITES)
            and all(_already(rel, content) for rel, content in NEW_FILES)
            and all(_already(rel, new) for rel, repls in PATCHES for _old, new in repls)
        )
        if already_applied:
            print("\nAll target text already present — code changes appear already applied.")
            print("Will still check/apply the DB migration below.")
        else:
            raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")
    print("Pre-flight OK.\n")


def apply_all(dry_run: bool):
    changed_files = []
    report = []

    for rel_path, content in NEW_FILES:
        full_path = REPO_ROOT / rel_path
        if full_path.exists():
            continue
        if dry_run:
            report.append(f"[DRY RUN] would create: {rel_path}")
        else:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            report.append(f"CREATED: {rel_path}")
            changed_files.append(full_path)

    for rel_path, old_anchor, new_content in FULL_REWRITES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        if old_anchor not in text:
            report.append(f"SKIPPED (already rewritten): {rel_path}")
            continue
        if dry_run:
            report.append(f"[DRY RUN] would fully rewrite: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(new_content, encoding="utf-8")
            report.append(f"REWROTE: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        original_text = text
        for old, new in replacements:
            if new in text:
                continue  # already patched
            if old not in text:
                raise PatchError(f"{rel_path}: expected anchor vanished mid-patch — aborting")
            text = text.replace(old, new, 1)

        if text == original_text:
            continue

        if dry_run:
            report.append(f"[DRY RUN] would patch: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(text, encoding="utf-8")
            report.append(f"PATCHED: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)

    for line in report:
        print(line)

    return changed_files


def validate_python_syntax(changed_files):
    print("\n=== Validating Python syntax (py_compile) ===")
    for f in changed_files:
        if f.suffix != ".py":
            continue
        try:
            py_compile.compile(str(f), doraise=True)
            print(f"  OK  {f.relative_to(REPO_ROOT)}")
        except py_compile.PyCompileError as e:
            raise PatchError(f"SYNTAX ERROR after patching {f}: {e}")


# ─────────────────────────────────────────────────────────────────────────
# DB migration — same connect/backup/apply/verify pattern as
# apply_access_scopes_migration.py, folded into this one script so a
# single run does code + schema together.
# ─────────────────────────────────────────────────────────────────────────

def _db_connect():
    import mysql.connector
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME,
    )


def _table_exists(conn, table_name: str) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        (DB_NAME, table_name),
    )
    count = cursor.fetchone()[0]
    cursor.close()
    return count > 0


def _take_db_backup(dry_run: bool):
    DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = DB_BACKUP_DIR / f"pre_org_group_rbac_{timestamp}.sql"

    if dry_run:
        print(f"[DRY RUN] would back up {DB_NAME} to {backup_path}")
        return None

    cmd = ["mysqldump", f"--host={DB_HOST}", f"--port={DB_PORT}", f"--user={DB_USER}"]
    if DB_PASSWORD:
        cmd.append(f"--password={DB_PASSWORD}")
    cmd += ["--default-character-set=utf8mb4", DB_NAME]

    try:
        with open(backup_path, "w", encoding="utf-8") as f:
            result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            raise PatchError(f"mysqldump failed: {result.stderr}")
        print(f"DB backup written: {backup_path}")
        return backup_path
    except FileNotFoundError:
        print("WARNING: 'mysqldump' not found on PATH — skipping backup. This "
              "migration is additive-only (three new tables, zero ALTERs on "
              "existing tables), so risk is low, but proceed with that in mind.")
        return None


def _apply_db_migration(conn, dry_run: bool):
    code_lines = [
        line for line in MIGRATION_SQL.splitlines()
        if not line.strip().startswith("--")
    ]
    sql = "\n".join(code_lines)
    statements = [s.strip() for s in sql.split(";") if s.strip()]

    if dry_run:
        print(f"[DRY RUN] would execute {len(statements)} statement(s) from 013_org_group_rbac.sql")
        return

    cursor = conn.cursor()
    for stmt in statements:
        cursor.execute(stmt)
    conn.commit()
    cursor.close()
    print("Applied db/migrations/013_org_group_rbac.sql")


def _verify_db(conn):
    expected = {
        "org_groups": {"id", "name", "level", "parent_group_id", "description",
                        "created_by", "created_at", "updated_at"},
        "group_policies": {"id", "group_id", "cloud", "account_ref_id", "regions",
                            "resource_groups", "resource_types", "resource_ids",
                            "granted_by", "created_at", "updated_at"},
        "user_group_memberships": {"id", "user_id", "group_id", "assigned_by", "assigned_at"},
    }
    for table, expected_columns in expected.items():
        cursor = conn.cursor()
        cursor.execute(f"DESCRIBE {table}")
        rows = cursor.fetchall()
        cursor.close()
        actual_columns = {row[0] for row in rows}
        missing = expected_columns - actual_columns
        if missing:
            raise PatchError(f"{table} is missing expected columns: {missing}")
        print(f"Verified: {table} has all {len(expected_columns)} expected columns.")


def run_db_migration(dry_run: bool, skip_backup: bool):
    # Note: this reads the SQL from the in-memory MIGRATION_SQL constant
    # (identical to what apply_all() writes to db/migrations/013_org_group_rbac.sql),
    # not from disk — so this works correctly even in --dry-run, where
    # apply_all() intentionally writes nothing.
    try:
        import mysql.connector
    except ImportError:
        raise PatchError(
            "mysql-connector-python is not installed. Install it (pip install "
            "mysql-connector-python) or re-run with --skip-db and apply "
            "db/migrations/013_org_group_rbac.sql yourself."
        )

    print("\n=== DB migration: org_groups / group_policies / user_group_memberships ===")
    try:
        conn = _db_connect()
    except mysql.connector.Error as e:
        raise PatchError(f"could not connect to database ({DB_HOST}:{DB_PORT}/{DB_NAME}): {e}")

    try:
        if _table_exists(conn, "org_groups"):
            print("org_groups already exists — DB migration already applied. Nothing to do.")
            _verify_db(conn)
            return
        if not skip_backup:
            _take_db_backup(dry_run)
        _apply_db_migration(conn, dry_run)
        if not dry_run:
            _verify_db(conn)
            print("\n=== DB migration done. ===")
        else:
            print("\n=== DB dry run complete. ===")
    except mysql.connector.Error as e:
        raise PatchError(str(e))
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, touch nothing")
    parser.add_argument("--skip-db", action="store_true", help="Apply code changes only, skip the DB migration")
    parser.add_argument("--skip-db-backup", action="store_true", help="Skip the mysqldump backup before migrating")
    args = parser.parse_args()

    try:
        preflight()
        changed = apply_all(args.dry_run)
        if not args.dry_run:
            validate_python_syntax(changed)
            print(f"\n=== Code step done. {len(changed)} file(s) touched. ===")

        if not args.skip_db:
            run_db_migration(args.dry_run, args.skip_db_backup)
        else:
            print("\n--skip-db passed: not touching the database. Apply "
                  "db/migrations/013_org_group_rbac.sql manually when ready.")

        if not args.dry_run:
            print("\nNext steps:")
            print("  1. Full uvicorn restart (not --reload).")
            print("  2. As an admin, create the hierarchy, e.g.:")
            print('       POST /api/groups {"name":"APAC","level":"L1"}')
            print('       POST /api/groups {"name":"India-NOC","level":"L2","parent_group_id":<L1 id>}')
            print('       POST /api/groups {"name":"L3-OnCall","level":"L3","parent_group_id":<L2 id>}')
            print("  3. Attach an account+region-specific policy to a group:")
            print('       POST /api/groups/{id}/policies')
            print('       {"scopes":[{"cloud":"aws","account_ref_id":3,"regions":["ap-south-1"]}]}')
            print("  4. Add users to a group:")
            print('       POST /api/groups/{id}/members {"user_ids":[7,9]}')
            print("     They immediately inherit that group's policy plus every")
            print("     ancestor group's policy — verify via:")
            print('       GET /api/groups/users/{user_id}/groups')
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
