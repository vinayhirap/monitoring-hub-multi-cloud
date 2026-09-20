# app/api/admin/roles.py
"""
app/api/admin/roles.py

Phase 2 of the RBAC audit plan, RBAC-administration slice: CRUD for
`roles` and `role_permissions_v2` (db/migrations/040/041). Referenced
by name in both of those migrations' comments ("app/api/admin/
roles.py writes both while the legacy path is alive") but never
actually created until now -- the audit's own headline finding was
that the system which would control who can change permissions had
no permission controlling it, because no endpoint existed to enforce
one against.

Two separate write actions, two separate permission codes, matching
the catalog's own descriptions:
  - roles.create/update/delete -- a role's identity (name, description).
    Custom roles only; the three builtin roles (admin/editor/viewer)
    cannot be renamed or removed (is_builtin=1), matching the
    constraint 040 already put in the schema and comments.
  - permissions.manage -- a role's PERMISSION SET. Deliberately
    separate: an org may want "can rename a role" and "can decide
    what a role is allowed to do" to be different people. This also
    covers editing a BUILTIN role's permissions (e.g. adding a code to
    what "editor" grants), which 040's comment says is intentionally
    still allowed ("their permission set CAN still be edited, same as
    today").

Legacy mirror: role_permissions (the ENUM-keyed v1 table) is what
app.auth.permissions.require_permission() actually reads today, since
Phase 3 (the v2 cutover) hasn't happened yet. Editing a BUILTIN role's
permission set here writes BOTH role_permissions_v2 AND the legacy
role_permissions row, so the change has real, immediate effect instead
of silently only mattering once Phase 3 ships. Custom roles have no
legacy equivalent (role_permissions.role is a 3-value ENUM) -- they
only exist in v2 and have zero effect until Phase 3's cutover gives
require_permission_v2() a caller.
"""
from fastapi import APIRouter, Body, HTTPException, Depends
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth import rbac
from app.audit import write_audit as _write_audit
import datetime

router = APIRouter(prefix="/api/rbac/roles", tags=["RBAC Administration"])

_BUILTIN_KEYS = ("admin", "editor", "viewer")


def _serialize(obj):
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    return obj


def _fetch_role(conn, role_id: int):
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM roles WHERE id = %s", (role_id,))
    row = cursor.fetchone()
    cursor.close()
    return row


def _role_permission_codes(conn, role_id: int) -> list:
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT p.code FROM role_permissions_v2 rpv "
        "JOIN permissions p ON p.id = rpv.permission_id "
        "WHERE rpv.role_id = %s ORDER BY p.code",
        (role_id,),
    )
    codes = [r["code"] for r in cursor.fetchall()]
    cursor.close()
    return codes


@router.get("")
def list_roles(current_user: dict = Depends(require_permission("roles.view"))):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM roles ORDER BY role_rank DESC, name ASC")
        roles = cursor.fetchall()
        cursor.close()
        for r in roles:
            r["permissions"] = _role_permission_codes(conn, r["id"])
    finally:
        conn.close()
    return [_serialize(r) for r in roles]


@router.post("")
def create_role(payload: dict = Body(...), current_user: dict = Depends(require_permission("roles.create"))):
    name = (payload.get("name") or "").strip()
    role_key = (payload.get("role_key") or "").strip().lower()
    description = (payload.get("description") or "").strip() or None
    codes = payload.get("permissions") or []

    if not name or not role_key:
        raise HTTPException(status_code=400, detail="name and role_key are required")
    if role_key in _BUILTIN_KEYS:
        raise HTTPException(status_code=409, detail=f"'{role_key}' is a builtin role key and cannot be reused")
    if not all(isinstance(c, str) for c in codes):
        raise HTTPException(status_code=400, detail="permissions must be a list of permission codes")

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id FROM roles WHERE role_key = %s", (role_key,))
        if cursor.fetchone():
            cursor.close()
            raise HTTPException(status_code=409, detail=f"Role key '{role_key}' already exists")

        # Custom roles always rank 0 -- they can never out-rank a
        # builtin role, matching 040's comment on role_rank. Not
        # settable by the caller.
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO roles (role_key, name, description, is_builtin, role_rank, created_by) "
            "VALUES (%s, %s, %s, 0, 0, %s)",
            (role_key, name, description, current_user["id"]),
        )
        role_id = cursor.lastrowid

        if codes:
            cursor.execute(
                "SELECT id, code FROM permissions WHERE code IN (%s)" % ",".join(["%s"] * len(codes)),
                tuple(codes),
            )
            found = {r[0]: r[1] for r in cursor.fetchall()}
            unknown = set(codes) - set(found.values())
            if unknown:
                conn.rollback()
                cursor.close()
                raise HTTPException(status_code=400, detail=f"Unknown permission code(s): {', '.join(sorted(unknown))}")
            for pid in found:
                cursor.execute(
                    "INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id) VALUES (%s, %s)",
                    (role_id, pid),
                )
        conn.commit()
        cursor.close()
    finally:
        conn.close()

    _write_audit(actor=current_user["username"], action="Role created",
                 detail=f"{name} ({role_key}), {len(codes)} permission(s)",
                 role=current_user["role"].upper())
    return {"status": "created", "id": role_id, "role_key": role_key}


@router.patch("/{role_id}")
def update_role(role_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("roles.update"))):
    conn = get_connection()
    try:
        role = _fetch_role(conn, role_id)
        if not role:
            raise HTTPException(status_code=404, detail="Role not found")
        if role["is_builtin"]:
            raise HTTPException(status_code=403, detail="Builtin roles cannot be renamed or re-described")

        name = payload.get("name")
        description = payload.get("description")
        if name is not None and not name.strip():
            raise HTTPException(status_code=400, detail="name cannot be blank")

        cursor = conn.cursor()
        cursor.execute(
            "UPDATE roles SET name = COALESCE(%s, name), description = %s WHERE id = %s",
            (name.strip() if name is not None else None, description, role_id),
        )
        conn.commit()
        cursor.close()
    finally:
        conn.close()

    rbac.invalidate_principal(None)
    _write_audit(actor=current_user["username"], action="Role updated",
                 detail=f"role #{role_id}", role=current_user["role"].upper())
    return {"status": "updated", "id": role_id}


@router.put("/{role_id}/permissions")
def set_role_permissions(role_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("permissions.manage"))):
    """Full-replace the permission set for one role (builtin or custom)."""
    codes = payload.get("permissions")
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
        raise HTTPException(status_code=400, detail="permissions must be a list of permission codes")

    conn = get_connection()
    try:
        role = _fetch_role(conn, role_id)
        if not role:
            raise HTTPException(status_code=404, detail="Role not found")

        cursor = conn.cursor(dictionary=True)
        if codes:
            cursor.execute(
                "SELECT id, code FROM permissions WHERE code IN (%s)" % ",".join(["%s"] * len(codes)),
                tuple(codes),
            )
            found_rows = cursor.fetchall()
            found_codes = {r["code"] for r in found_rows}
            unknown = set(codes) - found_codes
            if unknown:
                cursor.close()
                raise HTTPException(status_code=400, detail=f"Unknown permission code(s): {', '.join(sorted(unknown))}")
            permission_ids = [r["id"] for r in found_rows]
        else:
            permission_ids = []

        cursor2 = conn.cursor()
        cursor2.execute("DELETE FROM role_permissions_v2 WHERE role_id = %s", (role_id,))
        for pid in permission_ids:
            cursor2.execute(
                "INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id) VALUES (%s, %s)",
                (role_id, pid),
            )

        # Legacy mirror -- only meaningful for the 3 builtin keys,
        # since role_permissions.role is a 3-value ENUM and
        # require_permission() (v1, still the live enforcement path
        # pre-Phase-3) only ever reads that table.
        if role["role_key"] in _BUILTIN_KEYS:
            cursor2.execute("DELETE FROM role_permissions WHERE role = %s", (role["role_key"],))
            for pid in permission_ids:
                cursor2.execute(
                    "INSERT IGNORE INTO role_permissions (role, permission_id) VALUES (%s, %s)",
                    (role["role_key"], pid),
                )
        conn.commit()
        cursor.close()
        cursor2.close()
    finally:
        conn.close()

    rbac.invalidate_principal(None)
    _write_audit(actor=current_user["username"], action="Role permissions changed",
                 detail=f"role '{role['role_key']}' -> {len(codes)} permission(s)",
                 role=current_user["role"].upper())
    return {"status": "updated", "id": role_id, "permissions": sorted(codes)}


@router.delete("/{role_id}")
def delete_role(role_id: int, current_user: dict = Depends(require_permission("roles.delete"))):
    conn = get_connection()
    try:
        role = _fetch_role(conn, role_id)
        if not role:
            raise HTTPException(status_code=404, detail="Role not found")
        if role["is_builtin"]:
            raise HTTPException(status_code=403, detail="Builtin roles cannot be deleted")

        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT COUNT(*) AS n FROM role_bindings WHERE role_id = %s", (role_id,))
        in_use = cursor.fetchone()["n"]
        if in_use:
            cursor.close()
            raise HTTPException(
                status_code=409,
                detail=f"Role is bound to {in_use} principal(s) at a scope -- revoke those bindings first",
            )

        cursor2 = conn.cursor()
        cursor2.execute("DELETE FROM roles WHERE id = %s", (role_id,))
        conn.commit()
        cursor.close()
        cursor2.close()
    finally:
        conn.close()

    _write_audit(actor=current_user["username"], action="Role deleted",
                 detail=f"role #{role_id} ({role['role_key']})", role=current_user["role"].upper())
    return {"status": "deleted", "id": role_id}
