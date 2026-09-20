# app/api/op_events.py
"""
Read endpoint for op_events (db/migrations/022_op_events_table.sql,
roadmap phase 5, 2026-09-13). Gated on operations.view -- same
permission as other operational-visibility surfaces (see
db/migrations/015_permissions_rbac.sql).

SECURITY: editor holds operations.view (015's seed list), and this
endpoint joins in account_name and accepts an arbitrary account_id
filter -- so despite the "not scoped per-account like alerts/
resources" reasoning this file used to have, an editor scoped to
exactly one client account could browse discovery/collector/alert-
eval failure events (including account_name) for every OTHER client
too, just by omitting the filter or passing a different account_id.
In a multi-client setup that's a real cross-tenant leak of one
client's operational health/error detail to someone only engaged on
another's account, even though it's read-only and lower severity than
the escalation_policies IDOR (no write path here).

Now enforced the same way as 15 other endpoint files: an event with
aws_account_id IS NULL is a system-wide/collector-level event (not
tied to any client, e.g. "the whole collector cycle failed") and stays
visible to everyone with operations.view; an event tied to a specific
account is only visible to admin or someone whose access_scopes
actually cover that account.
"""
import datetime
from fastapi import APIRouter, HTTPException, Query, Depends
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids

router = APIRouter(prefix="/api/op-events", tags=["Operational Events"])


@router.get("")
def list_op_events(
    event_type: str = Query(None),
    severity: str = Query(None),
    account_id: int = Query(None),
    limit: int = Query(100, le=500),
    current_user: dict = Depends(require_permission("operations.view")),
):
    accessible = None if current_user["role"] == "admin" else get_accessible_account_ids(current_user)
    if account_id is not None and accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")

    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        clauses, params = [], []
        if event_type:
            clauses.append("event_type = %s"); params.append(event_type)
        if severity:
            clauses.append("severity = %s"); params.append(severity)
        if account_id:
            clauses.append("aws_account_id = %s"); params.append(account_id)
        elif accessible is not None:
            # No specific account requested and the caller isn't
            # unrestricted -- show only system-wide events (NULL) plus
            # whatever accounts they actually have access to. An empty
            # accessible set still must render as "no accounts", so
            # this still needs an explicit clause rather than skipping
            # the filter.
            placeholders = ",".join(["%s"] * len(accessible)) if accessible else "NULL"
            clauses.append(f"(aws_account_id IS NULL OR aws_account_id IN ({placeholders}))")
            params += sorted(accessible)
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
