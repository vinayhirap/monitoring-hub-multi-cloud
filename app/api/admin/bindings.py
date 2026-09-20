# app/api/admin/bindings.py
"""
app/api/admin/bindings.py

Phase 2 of the RBAC audit plan, RBAC-administration slice: the last
piece of "who guards the guards". Covers:

  - role_bindings   -- (principal, role, scope) grants. rbac.binding.*
  - permission_overrides -- explicit deny (rarely allow) exceptions.
    rbac.override.manage only; no separate view code exists in the
    catalog (041 never defined rbac.override.view), and the schema
    comment for this table says overrides are "kept deliberately
    narrow ... only admins may write one" -- bundling read into the
    same manage code keeps that narrowness rather than inventing a
    wider view surface the audit never asked for.
  - access_reviews  -- attestation log ("Priya's prod access was
    reviewed on 12 Aug, decision: retain"). Pure record-keeping: this
    endpoint does NOT itself revoke a binding on a 'revoke' decision --
    an administrator who decides to revoke still calls
    DELETE /api/rbac/bindings/{id} explicitly. Auto-revoking as a side
    effect of writing a log entry is exactly the kind of hidden
    mutation that makes an audit trail untrustworthy.
  - GET /api/rbac/explain -- exposes app.auth.rbac.explain() so "why
    does this user have (or not have) this access" is self-service
    instead of a database read, same value 041's migration comment
    named as the point of finishing the v2 cutover.

Privilege-escalation gate: creating a binding additionally requires
rbac.can_grant() to return True -- holding rbac.binding.manage is
necessary but not sufficient. An Editor with binding.manage granted at
a dev-only scope still cannot mint anything on prod; only someone
whose OWN role+scope covers what they're handing out can grant it,
exactly matching AWS IAM's "you cannot grant a permission you don't
have" pattern for policy attachment.
"""
from fastapi import APIRouter, Body, HTTPException, Depends, Query
from app.db import get_connection
from app.auth.deps import get_current_user
from app.auth.permissions import require_permission
from app.auth import rbac
from app.audit import write_audit as _write_audit
import datetime

router = APIRouter(prefix="/api/rbac", tags=["RBAC Administration"])


def _serialize(obj):
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    return obj


def _principal_exists(conn, principal_type: str, principal_id: int) -> bool:
    cursor = conn.cursor()
    if principal_type == "user":
        cursor.execute("SELECT id FROM users WHERE id = %s", (principal_id,))
    else:
        cursor.execute("SELECT id FROM org_groups WHERE id = %s", (principal_id,))
    row = cursor.fetchone()
    cursor.close()
    return row is not None


# ─────────────────────────────────────────────────────────────────────
# Role bindings
# ─────────────────────────────────────────────────────────────────────

@router.get("/bindings")
def list_bindings(principal_type: str = Query(None), principal_id: int = Query(None),
                   current_user: dict = Depends(require_permission("rbac.binding.view"))):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        q = ("SELECT b.*, r.role_key, r.name AS role_name, s.label AS scope_label, "
             "u.username AS granted_by_username "
             "FROM role_bindings b "
             "JOIN roles r ON r.id = b.role_id "
             "JOIN rbac_scopes s ON s.id = b.scope_id "
             "JOIN users u ON u.id = b.granted_by")
        conditions, params = [], []
        if principal_type:
            conditions.append("b.principal_type = %s")
            params.append(principal_type)
        if principal_id:
            conditions.append("b.principal_id = %s")
            params.append(principal_id)
        if conditions:
            q += " WHERE " + " AND ".join(conditions)
        q += " ORDER BY b.created_at DESC"
        cursor.execute(q, tuple(params))
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()
    return [_serialize(r) for r in rows]


@router.post("/bindings")
def create_binding(payload: dict = Body(...), current_user: dict = Depends(require_permission("rbac.binding.manage"))):
    principal_type = payload.get("principal_type")
    principal_id = payload.get("principal_id")
    role_id = payload.get("role_id")
    scope_id = payload.get("scope_id")
    reason = (payload.get("reason") or "").strip() or None
    expires_at = payload.get("expires_at")  # ISO string or None

    if principal_type not in ("user", "group"):
        raise HTTPException(status_code=400, detail="principal_type must be 'user' or 'group'")
    if not isinstance(principal_id, int) or not isinstance(role_id, int) or not isinstance(scope_id, int):
        raise HTTPException(status_code=400, detail="principal_id, role_id, and scope_id are required integers")

    conn = get_connection()
    try:
        if not _principal_exists(conn, principal_type, principal_id):
            raise HTTPException(status_code=404, detail=f"No {principal_type} with id {principal_id}")

        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, role_key, role_rank, name FROM roles WHERE id = %s", (role_id,))
        role_row = cursor.fetchone()
        if not role_row:
            cursor.close()
            raise HTTPException(status_code=404, detail="Role not found")

        cursor.execute("SELECT * FROM rbac_scopes WHERE id = %s", (scope_id,))
        scope_row = cursor.fetchone()
        cursor.close()
        if not scope_row:
            raise HTTPException(status_code=404, detail="Scope not found")

        target_scope = rbac._scope_from_row(scope_row)

        # Admins bypass the escalation gate the same way they bypass
        # has_permission()'s table lookup (app.auth.permissions) --
        # an admin binding gap must never be able to lock the org's
        # own administrators out of granting access, which would be a
        # far worse failure mode than the gate needing an explicit
        # admin carve-out.
        if current_user["role"] != "admin" and not rbac.can_grant(current_user, role_row["role_key"], target_scope):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"You do not hold '{role_row['role_key']}' (or higher) at a scope that covers "
                    f"the one you're trying to grant -- you cannot delegate access you don't have."
                ),
            )

        if reason is None and role_row["role_rank"] >= 30:
            raise HTTPException(status_code=400, detail="A reason is required when granting an admin-rank role")

        parsed_expiry = None
        if expires_at:
            try:
                parsed_expiry = datetime.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=400, detail="expires_at must be an ISO 8601 timestamp")

        cursor2 = conn.cursor(dictionary=True)
        cursor2.execute(
            "SELECT id FROM role_bindings WHERE principal_type=%s AND principal_id=%s AND role_id=%s AND scope_id=%s",
            (principal_type, principal_id, role_id, scope_id),
        )
        if cursor2.fetchone():
            cursor2.close()
            raise HTTPException(status_code=409, detail="This exact (principal, role, scope) binding already exists")

        cursor2.execute(
            "INSERT INTO role_bindings (principal_type, principal_id, role_id, scope_id, granted_by, reason, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (principal_type, principal_id, role_id, scope_id, current_user["id"], reason, parsed_expiry),
        )
        binding_id = cursor2.lastrowid
        conn.commit()
        cursor2.close()
    finally:
        conn.close()

    rbac.invalidate_principal(principal_id if principal_type == "user" else None)
    _write_audit(
        actor=current_user["username"], action="RBAC binding granted",
        detail=f"{principal_type} #{principal_id}: role '{role_row['role_key']}' at scope '{scope_row.get('label') or scope_id}'"
               + (f" -- {reason}" if reason else ""),
        role=current_user["role"].upper(),
    )
    return {"status": "created", "id": binding_id}


@router.delete("/bindings/{binding_id}")
def revoke_binding(binding_id: int, current_user: dict = Depends(require_permission("rbac.binding.manage"))):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT b.*, r.role_key FROM role_bindings b JOIN roles r ON r.id = b.role_id WHERE b.id = %s",
            (binding_id,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            raise HTTPException(status_code=404, detail="Binding not found")

        cursor2 = conn.cursor()
        cursor2.execute("DELETE FROM role_bindings WHERE id = %s", (binding_id,))
        conn.commit()
        cursor.close()
        cursor2.close()
    finally:
        conn.close()

    rbac.invalidate_principal(row["principal_id"] if row["principal_type"] == "user" else None)
    _write_audit(actor=current_user["username"], action="RBAC binding revoked",
                 detail=f"{row['principal_type']} #{row['principal_id']}: role '{row['role_key']}' removed",
                 role=current_user["role"].upper())
    return {"status": "revoked", "id": binding_id}


# ─────────────────────────────────────────────────────────────────────
# Deny overrides
# ─────────────────────────────────────────────────────────────────────

@router.get("/overrides")
def list_overrides(current_user: dict = Depends(require_permission("rbac.override.manage"))):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT o.*, p.code AS permission_code, s.label AS scope_label, u.username AS granted_by_username "
            "FROM permission_overrides o "
            "JOIN permissions p ON p.id = o.permission_id "
            "LEFT JOIN rbac_scopes s ON s.id = o.scope_id "
            "JOIN users u ON u.id = o.granted_by "
            "ORDER BY o.created_at DESC"
        )
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()
    return [_serialize(r) for r in rows]


@router.post("/overrides")
def create_override(payload: dict = Body(...), current_user: dict = Depends(require_permission("rbac.override.manage"))):
    principal_type = payload.get("principal_type")
    principal_id = payload.get("principal_id")
    permission_code = (payload.get("permission_code") or "").strip()
    scope_id = payload.get("scope_id")  # None = applies everywhere
    effect = (payload.get("effect") or "deny").strip()
    reason = (payload.get("reason") or "").strip() or None

    if principal_type not in ("user", "group"):
        raise HTTPException(status_code=400, detail="principal_type must be 'user' or 'group'")
    if not isinstance(principal_id, int) or not permission_code:
        raise HTTPException(status_code=400, detail="principal_id and permission_code are required")
    if effect not in ("allow", "deny"):
        raise HTTPException(status_code=400, detail="effect must be 'allow' or 'deny'")
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required for an override -- this bypasses the normal grant path")

    conn = get_connection()
    try:
        if not _principal_exists(conn, principal_type, principal_id):
            raise HTTPException(status_code=404, detail=f"No {principal_type} with id {principal_id}")

        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id FROM permissions WHERE code = %s", (permission_code,))
        perm = cursor.fetchone()
        if not perm:
            cursor.close()
            raise HTTPException(status_code=400, detail=f"Unknown permission code: {permission_code}")

        if scope_id is not None:
            cursor.execute("SELECT id FROM rbac_scopes WHERE id = %s", (scope_id,))
            if not cursor.fetchone():
                cursor.close()
                raise HTTPException(status_code=404, detail="Scope not found")
        cursor.close()

        cursor2 = conn.cursor()
        cursor2.execute(
            "INSERT INTO permission_overrides (principal_type, principal_id, permission_id, scope_id, effect, reason, granted_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (principal_type, principal_id, perm["id"], scope_id, effect, reason, current_user["id"]),
        )
        override_id = cursor2.lastrowid
        conn.commit()
        cursor2.close()
    finally:
        conn.close()

    rbac.invalidate_principal(principal_id if principal_type == "user" else None)
    _write_audit(actor=current_user["username"], action="RBAC override created",
                 detail=f"{effect} {permission_code} for {principal_type} #{principal_id} -- {reason}",
                 role=current_user["role"].upper())
    return {"status": "created", "id": override_id}


@router.delete("/overrides/{override_id}")
def delete_override(override_id: int, current_user: dict = Depends(require_permission("rbac.override.manage"))):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM permission_overrides WHERE id = %s", (override_id,))
        row = cursor.fetchone()
        if not row:
            cursor.close()
            raise HTTPException(status_code=404, detail="Override not found")
        cursor2 = conn.cursor()
        cursor2.execute("DELETE FROM permission_overrides WHERE id = %s", (override_id,))
        conn.commit()
        cursor.close()
        cursor2.close()
    finally:
        conn.close()

    rbac.invalidate_principal(row["principal_id"] if row["principal_type"] == "user" else None)
    _write_audit(actor=current_user["username"], action="RBAC override removed",
                 detail=f"override #{override_id}", role=current_user["role"].upper())
    return {"status": "deleted", "id": override_id}


# ─────────────────────────────────────────────────────────────────────
# Access reviews (attestation log -- does not itself mutate bindings)
# ─────────────────────────────────────────────────────────────────────

@router.get("/reviews")
def list_reviews(principal_id: int = Query(None), current_user: dict = Depends(require_permission("rbac.review.conduct"))):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        if principal_id:
            cursor.execute(
                "SELECT ar.*, u.username AS reviewed_by_username FROM access_reviews ar "
                "JOIN users u ON u.id = ar.reviewed_by WHERE ar.principal_id = %s ORDER BY ar.reviewed_at DESC",
                (principal_id,),
            )
        else:
            cursor.execute(
                "SELECT ar.*, u.username AS reviewed_by_username FROM access_reviews ar "
                "JOIN users u ON u.id = ar.reviewed_by ORDER BY ar.reviewed_at DESC LIMIT 500"
            )
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()
    return [_serialize(r) for r in rows]


@router.post("/reviews")
def record_review(payload: dict = Body(...), current_user: dict = Depends(require_permission("rbac.review.conduct"))):
    principal_type = payload.get("principal_type")
    principal_id = payload.get("principal_id")
    binding_id = payload.get("binding_id")  # optional -- reviewing a specific binding
    decision = (payload.get("decision") or "").strip()
    notes = (payload.get("notes") or "").strip() or None

    if principal_type not in ("user", "group"):
        raise HTTPException(status_code=400, detail="principal_type must be 'user' or 'group'")
    if not isinstance(principal_id, int):
        raise HTTPException(status_code=400, detail="principal_id is required")
    if decision not in ("retain", "revoke", "modify"):
        raise HTTPException(status_code=400, detail="decision must be 'retain', 'revoke', or 'modify'")

    conn = get_connection()
    try:
        if not _principal_exists(conn, principal_type, principal_id):
            raise HTTPException(status_code=404, detail=f"No {principal_type} with id {principal_id}")
        if binding_id is not None:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM role_bindings WHERE id = %s", (binding_id,))
            if not cursor.fetchone():
                cursor.close()
                raise HTTPException(status_code=404, detail="Binding not found")
            cursor.close()

        cursor2 = conn.cursor()
        cursor2.execute(
            "INSERT INTO access_reviews (binding_id, principal_type, principal_id, reviewed_by, decision, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (binding_id, principal_type, principal_id, current_user["id"], decision, notes),
        )
        review_id = cursor2.lastrowid
        conn.commit()
        cursor2.close()
    finally:
        conn.close()

    _write_audit(
        actor=current_user["username"], action="Access review recorded",
        detail=f"{principal_type} #{principal_id}: {decision}" + (f" -- {notes}" if notes else ""),
        role=current_user["role"].upper(),
    )
    return {"status": "recorded", "id": review_id}


# ─────────────────────────────────────────────────────────────────────
# Self-service explain
# ─────────────────────────────────────────────────────────────────────

@router.get("/explain")
def explain_access(
    permission: str = Query(...),
    user_id: int = Query(None),
    cloud: str = Query(None),
    account_id: int = Query(None),
    region: str = Query(None),
    service: str = Query(None),
    resource_id: str = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """
    Why does (or doesn't) a user have this permission at this target.
    A user may always explain their OWN access; explaining someone
    else's requires rbac.binding.view -- this is a read of another
    principal's security posture, not neutral information.
    """
    if user_id is None or user_id == current_user["id"]:
        target_user = current_user
    else:
        from app.auth.permissions import has_permission
        if not has_permission(current_user, "rbac.binding.view"):
            raise HTTPException(status_code=403, detail="Missing permission: rbac.binding.view")
        conn = get_connection()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT id, username, role FROM users WHERE id = %s", (user_id,))
            target_user = cursor.fetchone()
            cursor.close()
        finally:
            conn.close()
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")

    target = rbac.Target(cloud=cloud, account_id=account_id, region=region, service=service, resource_id=resource_id)
    return rbac.explain(target_user, permission, target)
