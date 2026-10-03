# app/api/audit_logs.py
"""
Audit log API — reads ONLY from the database.
No hardcoded data anywhere.
Every action in the system writes here automatically.
"""
from fastapi import APIRouter, Query, Depends
from app.db import get_connection
from app.auth.permissions import require_permission
from app.utils.time_json import to_utc_iso
import datetime
import json

router = APIRouter(prefix="/api", tags=["Audit Logs"])


def _parse_payload(payload):
    """Safely parse payload — handles both string JSON and dict."""
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except Exception:
            return {"raw": payload}
    return {}


def _serialize_row(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, (datetime.datetime, datetime.date)):
            out[k] = to_utc_iso(v)
        elif k == "payload":
            out[k] = _parse_payload(v)
        else:
            out[k] = v
    return out


@router.get("/audit-logs")
def get_audit_logs(
    limit:  int = Query(200, ge=1, le=1000),
    actor:  str = Query(None),
    action: str = Query(None),
    current_user: dict = Depends(require_permission("audit.view")),
):
    """
    Fetch audit logs from DB.
    Optional filters: actor, action (partial match).
    """
    # ip_address/user_agent/request_id: stored by write_audit() but never returned before, so the
    # Compliance page's IP handling had nothing to show (audit E8).
    cols   = "id, actor, action, payload, ip_address, user_agent, request_id, created_at"
    query  = "SELECT {cols} FROM audit_logs WHERE 1=1"
    params = []

    if actor:
        query += " AND actor LIKE %s"
        params.append(f"%{actor}%")
    if action:
        query += " AND action LIKE %s"
        params.append(f"%{action}%")

    query += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)

    # AUDIT(b06): connection is now released on error too (pool is 10).
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        try:
            cursor.execute(query.format(cols=cols), params)
        except Exception as exc:
            if getattr(exc, "errno", None) != 1054:      # unknown column: migration 078 not applied yet
                raise
            cursor.execute(query.format(cols="id, actor, action, payload, created_at"), params)
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    return [_serialize_row(r) for r in rows]

