# app/api/admin/rbac_scopes.py
"""
app/api/admin/rbac_scopes.py

Phase 2 of the RBAC audit plan, RBAC-administration slice: CRUD for
`rbac_scopes` (db/migrations/040) -- the reusable (cloud, account,
region, service, resource-group, resource-id, tag) boundary that a
role_binding (app/api/admin/bindings.py) points at. Scopes are created
here independently of any one binding so the same scope ("prod-aws /
ap-south-1 / ec2+rds") can be reused across multiple bindings without
retyping it, per 040's own design rationale.

Every JSON-array field (regions/services/resource_groups/resource_ids)
is validated as a list of strings if present; tag_selector as a dict
of string -> string|list[string]. All are optional -- omitting a
dimension leaves it NULL (unrestricted at that dimension), exactly as
rbac.py's Scope.covers() expects.
"""
from fastapi import APIRouter, Body, HTTPException, Depends
from app.db import get_connection
from app.auth.permissions import require_permission
from app.audit import write_audit as _write_audit
import json
import datetime

router = APIRouter(prefix="/api/rbac", tags=["RBAC Administration"])

_VALID_CLOUDS = ("aws", "azure", "gcp")
_LIST_FIELDS = ("regions", "services", "resource_groups", "resource_ids")


def _serialize(obj):
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    return obj


def _parse_json_col(value):
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _validate_scope_payload(payload: dict) -> dict:
    cloud = payload.get("cloud")
    if cloud is not None and cloud not in _VALID_CLOUDS:
        raise HTTPException(status_code=400, detail=f"cloud must be one of {_VALID_CLOUDS} or omitted for unrestricted")

    account_ref_id = payload.get("account_ref_id")
    if account_ref_id is not None and not isinstance(account_ref_id, int):
        raise HTTPException(status_code=400, detail="account_ref_id must be an integer account id")

    clean = {"label": (payload.get("label") or "").strip() or None, "cloud": cloud, "account_ref_id": account_ref_id}

    for field in _LIST_FIELDS:
        val = payload.get(field)
        if val is None:
            clean[field] = None
        elif isinstance(val, list) and all(isinstance(v, str) for v in val):
            clean[field] = val or None
        else:
            raise HTTPException(status_code=400, detail=f"{field} must be a list of strings or omitted")

    tag_selector = payload.get("tag_selector")
    if tag_selector is None:
        clean["tag_selector"] = None
    elif isinstance(tag_selector, dict):
        clean["tag_selector"] = tag_selector or None
    else:
        raise HTTPException(status_code=400, detail="tag_selector must be an object of tag -> value(s) or omitted")

    return clean


def _row_out(row: dict) -> dict:
    for f in _LIST_FIELDS + ("tag_selector",):
        row[f] = _parse_json_col(row.get(f))
    return row


@router.get("/service-catalog")
def list_service_catalog(cloud: str = None, current_user: dict = Depends(require_permission("rbac.scope.view"))):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        if cloud:
            cursor.execute(
                "SELECT cloud, service_key, display_name, category FROM rbac_service_catalog "
                "WHERE cloud = %s ORDER BY category, display_name", (cloud,),
            )
        else:
            cursor.execute(
                "SELECT cloud, service_key, display_name, category FROM rbac_service_catalog "
                "ORDER BY cloud, category, display_name"
            )
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()
    return rows


@router.get("/scopes")
def list_scopes(current_user: dict = Depends(require_permission("rbac.scope.view"))):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT s.*, a.account_name FROM rbac_scopes s "
            "LEFT JOIN aws_accounts a ON a.id = s.account_ref_id "
            "ORDER BY s.created_at DESC"
        )
        rows = [_row_out(r) for r in cursor.fetchall()]
        cursor.close()
    finally:
        conn.close()
    return [_serialize(r) for r in rows]


@router.post("/scopes")
def create_scope(payload: dict = Body(...), current_user: dict = Depends(require_permission("rbac.scope.manage"))):
    clean = _validate_scope_payload(payload)

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        if clean["account_ref_id"] is not None:
            cursor.execute("SELECT id, provider FROM aws_accounts WHERE id = %s", (clean["account_ref_id"],))
            acc = cursor.fetchone()
            if not acc:
                cursor.close()
                raise HTTPException(status_code=400, detail="account_ref_id does not match any onboarded account")
            if clean["cloud"] and acc["provider"] != clean["cloud"]:
                cursor.close()
                raise HTTPException(status_code=400, detail=f"account_ref_id belongs to provider '{acc['provider']}', not '{clean['cloud']}'")

        cursor2 = conn.cursor()
        cursor2.execute(
            "INSERT INTO rbac_scopes (label, cloud, account_ref_id, regions, services, "
            "resource_groups, resource_ids, tag_selector, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                clean["label"], clean["cloud"], clean["account_ref_id"],
                json.dumps(clean["regions"]) if clean["regions"] is not None else None,
                json.dumps(clean["services"]) if clean["services"] is not None else None,
                json.dumps(clean["resource_groups"]) if clean["resource_groups"] is not None else None,
                json.dumps(clean["resource_ids"]) if clean["resource_ids"] is not None else None,
                json.dumps(clean["tag_selector"]) if clean["tag_selector"] is not None else None,
                current_user["id"],
            ),
        )
        scope_id = cursor2.lastrowid
        conn.commit()
        cursor.close()
        cursor2.close()
    finally:
        conn.close()

    _write_audit(actor=current_user["username"], action="RBAC scope created",
                 detail=clean["label"] or f"scope #{scope_id}", role=current_user["role"].upper())
    return {"status": "created", "id": scope_id}


@router.patch("/scopes/{scope_id}")
def update_scope(scope_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("rbac.scope.manage"))):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id FROM rbac_scopes WHERE id = %s", (scope_id,))
        if not cursor.fetchone():
            cursor.close()
            raise HTTPException(status_code=404, detail="Scope not found")

        clean = _validate_scope_payload(payload)
        cursor2 = conn.cursor()
        cursor2.execute(
            "UPDATE rbac_scopes SET label=%s, cloud=%s, account_ref_id=%s, regions=%s, services=%s, "
            "resource_groups=%s, resource_ids=%s, tag_selector=%s WHERE id=%s",
            (
                clean["label"], clean["cloud"], clean["account_ref_id"],
                json.dumps(clean["regions"]) if clean["regions"] is not None else None,
                json.dumps(clean["services"]) if clean["services"] is not None else None,
                json.dumps(clean["resource_groups"]) if clean["resource_groups"] is not None else None,
                json.dumps(clean["resource_ids"]) if clean["resource_ids"] is not None else None,
                json.dumps(clean["tag_selector"]) if clean["tag_selector"] is not None else None,
                scope_id,
            ),
        )
        conn.commit()
        cursor.close()
        cursor2.close()
    finally:
        conn.close()

    from app.auth import rbac as _rbac
    _rbac.invalidate_principal(None)
    _write_audit(actor=current_user["username"], action="RBAC scope updated",
                 detail=f"scope #{scope_id}", role=current_user["role"].upper())
    return {"status": "updated", "id": scope_id}


@router.delete("/scopes/{scope_id}")
def delete_scope(scope_id: int, current_user: dict = Depends(require_permission("rbac.scope.manage"))):
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT COUNT(*) AS n FROM role_bindings WHERE scope_id = %s", (scope_id,))
        in_use = cursor.fetchone()["n"]
        if in_use:
            cursor.close()
            raise HTTPException(status_code=409, detail=f"Scope is used by {in_use} role binding(s) -- revoke those first")

        cursor2 = conn.cursor()
        cursor2.execute("DELETE FROM rbac_scopes WHERE id = %s", (scope_id,))
        if cursor2.rowcount == 0:
            cursor.close()
            cursor2.close()
            raise HTTPException(status_code=404, detail="Scope not found")
        conn.commit()
        cursor.close()
        cursor2.close()
    finally:
        conn.close()

    _write_audit(actor=current_user["username"], action="RBAC scope deleted",
                 detail=f"scope #{scope_id}", role=current_user["role"].upper())
    return {"status": "deleted", "id": scope_id}
