# app/api/audit_logs.py
"""
Audit log API — reads ONLY from the database.
No hardcoded data anywhere.
Every action in the system writes here automatically.
"""
from fastapi import APIRouter, Query, Depends
from app.db import get_connection
from app.auth.permissions import require_permission
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
            out[k] = v.isoformat()
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
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)

    query  = "SELECT id, actor, action, payload, created_at FROM audit_logs WHERE 1=1"
    params = []

    if actor:
        query += " AND actor LIKE %s"
        params.append(f"%{actor}%")
    if action:
        query += " AND action LIKE %s"
        params.append(f"%{action}%")

    query += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)

    cursor.execute(query, params)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    return [_serialize_row(r) for r in rows]

