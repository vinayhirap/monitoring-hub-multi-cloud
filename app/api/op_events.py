# app/api/op_events.py
"""
Read endpoint for op_events (db/migrations/022_op_events_table.sql,
roadmap phase 5, 2026-09-13). Gated on operations.view -- same
permission as other operational-visibility surfaces (see
db/migrations/015_permissions_rbac.sql) -- not scoped per-account like
alerts/resources, since these are collector/discovery-cycle events, not
per-resource data; an L1 operator without operations.view simply won't
see this endpoint at all (permission-gated, not row-filtered).
"""
import datetime
from fastapi import APIRouter, Query, Depends
from app.db import get_connection
from app.auth.permissions import require_permission

router = APIRouter(prefix="/api/op-events", tags=["Operational Events"])


@router.get("")
def list_op_events(
    event_type: str = Query(None),
    severity: str = Query(None),
    account_id: int = Query(None),
    limit: int = Query(100, le=500),
    current_user: dict = Depends(require_permission("operations.view")),
):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        clauses, params = [], []
        if event_type:
            clauses.append("event_type = %s"); params.append(event_type)
        if severity:
            clauses.append("severity = %s"); params.append(severity)
        if account_id:
            clauses.append("aws_account_id = %s"); params.append(account_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        cur.execute(f"""
            SELECT e.*, acc.account_name
            FROM op_events e
            LEFT JOIN aws_accounts acc ON acc.id = e.aws_account_id
            {where}
            ORDER BY e.created_at DESC
            LIMIT %s
        """, params)
        rows = cur.fetchall()
        for r in rows:
            if r.get("created_at") and isinstance(r["created_at"], datetime.datetime):
                r["created_at"] = r["created_at"].strftime("%Y-%m-%dT%H:%M:%SZ")
        return rows
    finally:
        cur.close(); conn.close()
