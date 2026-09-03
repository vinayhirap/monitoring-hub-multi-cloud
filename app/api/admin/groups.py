# app/api/admin/groups.py
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
