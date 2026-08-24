#!/usr/bin/env python3
"""
apply_phase1_authorization_service.py — Phase 1, sub-stage 2: the
actual scope-enforcement engine.

PREREQUISITE — run this FIRST if you haven't already:
    python apply_access_scopes_migration.py
This patch's code queries the access_scopes table; without it, any
editor/viewer authorization check will fail with a real DB error
(admin is unaffected — admin always short-circuits before touching the
table).

WHAT THIS ADDS:
  - app/auth/authorization.py (new): the centralized AuthorizationService
    the spec explicitly asked for. Every scope/role decision — who can
    manage whom, what's in a user's effective scope, whether a
    requested scope grant is within the actor's own — routes through
    this one module. get_effective_scope / get_accessible_account_ids /
    get_accessible_regions_for_account / can_manage_role / scope_within
    / validate_scope_shape / serialize_scope.

  - app/api/admin/users.py (rewritten): role gate loosened from
    admin-only to admin-OR-editor, with editor's actual capabilities
    bounded server-side on every call (never trusting what the client
    claims about its own permissions):
      * create_user: editor can only create role=viewer, and only with
        scopes that are a subset of the editor's own effective scope
        (validated via authz.scope_within — this is the actual
        privilege-escalation gate from the spec). If scope validation
        fails, the just-created user row is rolled back — no orphaned
        accounts.
      * list_users: editor sees only viewers they can actually manage
        (computed live, not cached).
      * delete_user / GET+POST /{id}/access / DELETE /access/{id}:
        gated by _user_manageable_by(), which re-checks LIVE on every
        call — if an admin later grants a viewer something outside an
        editor's scope, that editor immediately loses management
        rights over them, rather than that being a stale one-time check.
      * update_role stays admin-only by design (changing what role
        someone holds is a hierarchy change, not a scope delegation —
        documented in the code).
      * Also fixes _hash_password(): the old version only caught
        ImportError, but the actual passlib+bcrypt incompatibility
        (same one fixed in Phase 0's security.py) raises ValueError —
        meaning create_user() was silently 500ing before this fix,
        unrelated to anything this patch adds.

  - app/api/auth.py: GET /api/auth/me now includes the caller's
    effective scope (serialized), per the spec's suggested shape
    ({role, permissions, scopes}) — lets the frontend build scope-aware
    UI without a second round trip.

TESTED (not just syntax-checked): the full attack matrix from the
spec, run against a real MariaDB instance with real FK constraints and
real FastAPI TestClient routes — 21/21 checks passed, including:
editor creates viewer within scope (allow) / outside scope, region,
account, or cloud (deny) / editor creates editor or admin (deny) /
viewer creates anything (deny) / editor expands own scope via a viewer
grant (deny) / raw malicious payload bypassing frontend hiding entirely
(deny) / IDOR — editor guessing another editor's viewer's user ID
directly for delete/read/write (deny, all three) / admin unrestricted /
list_users correctly scoped per editor / rejected grants leave zero
orphaned user rows (verified via direct DB query, not just HTTP status).

WHAT THIS DOES NOT DO YET (next sub-stage):
  - Scope-aware FILTERING of actual data (alerts, resources, metrics,
    dashboards) — this stage is user/access MANAGEMENT only. A viewer
    scoped to one account can currently still see all alerts via
    GET /api/alerts, because that endpoint doesn't consult
    get_accessible_account_ids() yet. That's the next sub-stage.
  - Frontend — nothing here touches the UI yet.

Usage:
    python apply_phase1_authorization_service.py --dry-run
    python apply_phase1_authorization_service.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-phase1-authorization-service"


class PatchError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────
# New file: app/auth/authorization.py
# ─────────────────────────────────────────────────────────────────────────
AUTHORIZATION_PY = r'''"""
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
'''

# ─────────────────────────────────────────────────────────────────────────
# Full rewrite: app/api/admin/users.py
# ─────────────────────────────────────────────────────────────────────────
USERS_PY_OLD_ANCHOR = 'def delete_user(user_id: int, current_user: dict = Depends(require_role("admin"))):'
USERS_PY_NEW = r'''# app/api/admin/users.py
from fastapi import APIRouter, HTTPException, Body, Depends
from app.db import get_connection
from app.auth.deps import require_role
from app.auth import authorization as authz
import bcrypt
import datetime
import json

router = APIRouter(prefix="/api/users", tags=["Users"])


def _hash_password(password: str) -> str:
    # Raw bcrypt, not passlib \u2014 passlib's bcrypt backend detection is
    # broken with the installed bcrypt version here (confirmed: raises
    # ValueError, NOT the ImportError the old code only caught for \u2014
    # meaning create_user() was silently 500ing before this fix).
    return bcrypt.hashpw(password[:72].encode(), bcrypt.gensalt()).decode()


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
        conn = get_connection()
        cursor = conn.cursor()
        payload = json.dumps({"detail": detail, "role": role})
        cursor.execute(
            "INSERT INTO audit_logs (actor, action, payload) VALUES (%s, %s, %s)",
            (actor, action, payload)
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


def _fetch_user(conn, user_id: int):
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, role FROM users WHERE id = %s", (user_id,))
    row = cursor.fetchone()
    cursor.close()
    return row


def _user_manageable_by(actor: dict, target_user: dict) -> bool:
    """
    Can `actor` manage (view/modify access for, delete) `target_user`?
    Admin: yes, always (self-protection for delete/role-change is
    enforced separately at the route level).
    Editor: only if target is a viewer AND every one of the target's
    CURRENT scope grants is already within the editor's own effective
    scope. Re-checked live on every call \u2014 if an admin later grants
    that viewer something outside this editor's scope, the editor
    immediately loses the ability to manage them, rather than that
    being a one-time check that goes stale.
    """
    if actor["role"] == "admin":
        return True
    if actor["role"] != "editor" or target_user["role"] != "viewer":
        return False

    actor_scope = authz.get_effective_scope(actor)
    target_scope = authz.get_effective_scope(target_user)
    if target_scope == authz.FULL_ACCESS:
        return False  # shouldn't happen for a viewer, but never trust it
    if not target_scope:
        return True  # a viewer with zero scope is trivially "within" anything

    target_as_dicts = [
        {
            "cloud": g.cloud, "account_ref_id": g.account_ref_id,
            "regions": g.regions, "resource_groups": g.resource_groups,
            "resource_types": g.resource_types, "resource_ids": g.resource_ids,
        }
        for g in target_scope
    ]
    return authz.scope_within(target_as_dicts, actor_scope)


def _validate_and_insert_scopes(conn, user_id: int, scopes: list, actor: dict, actor_scope) -> list:
    """
    Validates each requested scope dict (structure, referential
    integrity against real accounts, and \u2014 for non-admin actors \u2014
    containment within the actor's own effective scope), then inserts
    them. Raises HTTPException on the first problem; nothing is
    inserted if any scope in the batch is invalid (all-or-nothing).
    """
    if not scopes:
        return []

    valid_accounts = _account_ids_by_cloud(conn)
    for s in scopes:
        err = authz.validate_scope_shape(s, valid_accounts)
        if err:
            raise HTTPException(status_code=400, detail=f"Invalid scope: {err}")

    if actor["role"] != "admin":
        if not authz.scope_within(scopes, actor_scope):
            raise HTTPException(
                status_code=403,
                detail="Cannot grant access outside your own assigned scope",
            )

    cursor = conn.cursor()
    inserted_ids = []
    for s in scopes:
        cursor.execute(
            "INSERT INTO access_scopes "
            "(user_id, cloud, account_ref_id, regions, resource_groups, resource_types, resource_ids, granted_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                user_id, s["cloud"], s.get("account_ref_id"),
                json.dumps(s["regions"]) if s.get("regions") else None,
                json.dumps(s["resource_groups"]) if s.get("resource_groups") else None,
                json.dumps(s["resource_types"]) if s.get("resource_types") else None,
                json.dumps(s["resource_ids"]) if s.get("resource_ids") else None,
                actor["id"],
            ),
        )
        inserted_ids.append(cursor.lastrowid)
    conn.commit()
    cursor.close()
    return inserted_ids


# Every endpoint below requires an authenticated admin OR editor.
# Fine-grained bounds (what an editor may see/create/delete) are
# enforced inside each function via authz.can_manage_role /
# _user_manageable_by / authz.scope_within \u2014 never by trusting
# anything the client sent about its own permissions.


@router.get("")
def list_users(current_user: dict = Depends(require_role("admin", "editor"))):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, role, created_at FROM users ORDER BY created_at ASC")
    rows = cursor.fetchall()

    if current_user["role"] == "admin":
        cursor.close()
        conn.close()
        return [_serialize(r) for r in rows]

    # Editor: only viewers they can actually manage.
    visible = [r for r in rows if _user_manageable_by(current_user, r)]
    cursor.close()
    conn.close()
    return [_serialize(r) for r in visible]


@router.post("")
def create_user(payload: dict = Body(...), current_user: dict = Depends(require_role("admin", "editor"))):
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()
    role     = (payload.get("role") or "viewer").strip().lower()
    scopes   = payload.get("scopes") or []

    if not username:
        raise HTTPException(status_code=400, detail="username required")
    if not password or len(password) < 6:
        raise HTTPException(status_code=400, detail="password min 6 characters")
    if role not in ["admin", "editor", "viewer"]:
        raise HTTPException(status_code=400, detail="role must be admin, editor, or viewer")

    if not authz.can_manage_role(current_user, role):
        raise HTTPException(
            status_code=403,
            detail="Editors may only create viewer accounts" if current_user["role"] == "editor"
            else "Insufficient permissions to assign this role",
        )

    if current_user["role"] == "editor" and not scopes:
        raise HTTPException(
            status_code=400,
            detail="Editors must specify at least one scope when creating a viewer "
                   "(a viewer with no scope has no purpose and is refused rather than silently created)",
        )

    pw_hash = _hash_password(password)
    conn    = get_connection()
    cursor  = conn.cursor()

    try:
        cursor.execute(
            "INSERT INTO users (username, password, role) VALUES (%s, %s, %s)",
            (username, pw_hash, role)
        )
        conn.commit()
        new_id = cursor.lastrowid
    except Exception as e:
        if "Duplicate" in str(e) or "1062" in str(e):
            raise HTTPException(status_code=409, detail=f"User '{username}' already exists")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()

    actor_scope = authz.get_effective_scope(current_user)
    try:
        _validate_and_insert_scopes(conn, new_id, scopes, current_user, actor_scope)
    except HTTPException:
        # Roll back the just-created user rather than leave an
        # orphaned account with no valid scope.
        cleanup = conn.cursor()
        cleanup.execute("DELETE FROM users WHERE id = %s", (new_id,))
        conn.commit()
        cleanup.close()
        conn.close()
        raise
    conn.close()

    _write_audit(
        actor=current_user["username"], action="User created",
        detail=f"{username} added as {role.upper()} with {len(scopes)} scope grant(s)",
    )
    return {"status": "created", "id": new_id, "username": username, "role": role, "scopes_granted": len(scopes)}


@router.patch("/{user_id}/role")
def update_role(user_id: int, payload: dict = Body(...), current_user: dict = Depends(require_role("admin"))):
    # Role changes stay admin-only by design: an editor's authority is
    # to manage VIEWER accounts within their scope, not to change what
    # role anyone holds (including promoting a viewer they manage into
    # an editor, which would be a role-hierarchy change, not a scope
    # delegation).
    new_role = (payload.get("role") or "").strip().lower()

    if new_role not in ["admin", "editor", "viewer"]:
        raise HTTPException(status_code=400, detail="role must be admin, editor, or viewer")
    if current_user["id"] == user_id:
        raise HTTPException(status_code=403, detail="Cannot change your own role")

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT username FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()
    if not user:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    cursor.execute("UPDATE users SET role = %s WHERE id = %s", (new_role, user_id))
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(actor=current_user["username"], action="Role changed", detail=f"{user['username']} \u2192 {new_role.upper()}")
    return {"status": "updated", "id": user_id, "role": new_role}


@router.get("/{user_id}/access")
def get_user_access(user_id: int, current_user: dict = Depends(require_role("admin", "editor"))):
    conn = get_connection()
    target = _fetch_user(conn, user_id)
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    if not _user_manageable_by(current_user, target):
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have access to this user's scope")
    conn.close()
    return authz.serialize_scope(target)


@router.post("/{user_id}/access")
def add_user_access(user_id: int, payload: dict = Body(...), current_user: dict = Depends(require_role("admin", "editor"))):
    scopes = payload.get("scopes") or []
    if not scopes:
        raise HTTPException(status_code=400, detail="scopes required")

    conn = get_connection()
    target = _fetch_user(conn, user_id)
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    if not _user_manageable_by(current_user, target):
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have access to manage this user's scope")

    actor_scope = authz.get_effective_scope(current_user)
    inserted_ids = _validate_and_insert_scopes(conn, user_id, scopes, current_user, actor_scope)
    conn.close()

    _write_audit(
        actor=current_user["username"], action="Access granted",
        detail=f"{target['username']}: +{len(inserted_ids)} scope grant(s)",
    )
    return {"status": "updated", "user_id": user_id, "scope_ids": inserted_ids}


@router.delete("/access/{scope_id}")
def revoke_access_scope(scope_id: int, current_user: dict = Depends(require_role("admin", "editor"))):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT s.id, s.user_id, u.username, u.role FROM access_scopes s "
        "JOIN users u ON u.id = s.user_id WHERE s.id = %s",
        (scope_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Scope grant not found")

    target = {"id": row["user_id"], "username": row["username"], "role": row["role"]}
    if not _user_manageable_by(current_user, target):
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have access to manage this user's scope")

    cursor = conn.cursor()
    cursor.execute("DELETE FROM access_scopes WHERE id = %s", (scope_id,))
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(actor=current_user["username"], action="Access revoked", detail=f"{target['username']}: scope #{scope_id} removed")
    return {"status": "revoked", "scope_id": scope_id}


@router.delete("/{user_id}")
def delete_user(user_id: int, current_user: dict = Depends(require_role("admin", "editor"))):
    if current_user["id"] == user_id:
        raise HTTPException(status_code=403, detail="Cannot delete your own account")

    conn = get_connection()
    target = _fetch_user(conn, user_id)
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    if not _user_manageable_by(current_user, target):
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have access to delete this user")

    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))  # access_scopes rows cascade via FK
    conn.commit()
    cursor.close()
    conn.close()

    _write_audit(actor=current_user["username"], action="User deleted", detail=f"{target['username']} removed")
    return {"status": "deleted", "id": user_id, "username": target["username"]}
'''

FULL_REWRITES = [
    ("app/api/admin/users.py", USERS_PY_OLD_ANCHOR, USERS_PY_NEW),
]

NEW_FILES = [
    ("app/auth/authorization.py", AUTHORIZATION_PY),
]

# ─────────────────────────────────────────────────────────────────────────
# Anchor patch: app/api/auth.py — /me includes effective scope
# ─────────────────────────────────────────────────────────────────────────
PATCHES = []

AUTH_ME_OLD = '''@router.get("/me")
def me(current_user: dict = Depends(get_current_user)):
    return current_user'''

AUTH_ME_NEW = '''@router.get("/me")
def me(current_user: dict = Depends(get_current_user)):
    from app.auth import authorization as authz
    return {**current_user, "scope": authz.serialize_scope(current_user)}'''

PATCHES.append(("app/api/auth.py", [(AUTH_ME_OLD, AUTH_ME_NEW)]))


def preflight():
    print("=== Pre-flight: verifying anchors match exactly ===")
    problems = []

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

    for rel_path, _content in NEW_FILES:
        full_path = REPO_ROOT / rel_path
        if full_path.exists():
            print(f"  (already exists, will skip creating) {rel_path}")
        else:
            print(f"  OK  {rel_path}: will be created")

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        for old, _new in replacements:
            count = text.count(old)
            if count == 0:
                problems.append(f"{rel_path}: anchor not found (0 matches)")
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
            print("\nAll target text already present — patch appears to be already applied. Nothing to do.")
            sys.exit(0)
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

    for rel_path, _old_anchor, new_content in FULL_REWRITES:
        full_path = REPO_ROOT / rel_path
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        preflight()
        changed = apply_all(args.dry_run)
        if not args.dry_run:
            validate_python_syntax(changed)
            print(f"\n=== Done. {len(changed)} file(s) touched. ===")
            print("\nIMPORTANT — if you have not already, run the schema migration FIRST:")
            print("  python apply_access_scopes_migration.py")
            print("(admin-role requests work fine either way; editor/viewer requests need the")
            print("access_scopes table to exist, or they'll hit a real DB error, not a graceful 403.)")
            print("\nThen: full uvicorn restart (not --reload).")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
