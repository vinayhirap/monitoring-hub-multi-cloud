# app/api/security.py
"""
Read-only API for security_findings (db/migrations/035_security_findings.sql,
app/collector/cspm.py). Findings are entirely system-generated (like
incidents -- see app/api/incidents.py's own docstring for the same
reasoning) -- no create/edit/delete surface, gated on security.view
only.
"""
import logging
from fastapi import APIRouter, Depends, Query
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/security-findings", tags=["Security"])


@router.get("")
def list_findings(
    status: str = Query("open", pattern="^(open|resolved|all)$"),
    severity: str = Query(None, pattern="^(HIGH|MEDIUM|LOW)$"),
    current_user: dict = Depends(require_permission("security.view")),
):
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        where = ["1=1"]
        params = []
        if status != "all":
            where.append("f.status = %s")
            params.append(status)
        if severity:
            where.append("f.severity = %s")
            params.append(severity)
        if accessible is not None:
            if not accessible:
                return []
            placeholders = ", ".join(["%s"] * len(accessible))
            where.append(f"f.aws_account_id IN ({placeholders})")
            params.extend(accessible)

        cursor.execute(f"""
            SELECT f.*, acc.account_name
            FROM security_findings f
            JOIN aws_accounts acc ON acc.id = f.aws_account_id
            WHERE {' AND '.join(where)}
            ORDER BY FIELD(f.severity, 'HIGH', 'MEDIUM', 'LOW'), f.last_seen_at DESC
        """, tuple(params))
        return cursor.fetchall()
    finally:
        cursor.close(); conn.close()


@router.get("/summary")
def findings_summary(current_user: dict = Depends(require_permission("security.view"))):
    """
    Per-account open-finding counts by severity -- the "posture score"
    view for a fleet overview page, same shape as
    app/api/incidents.py's fleet-wide health summary.
    """
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        where = ["f.status = 'open'"]
        params = []
        if accessible is not None:
            if not accessible:
                return []
            placeholders = ", ".join(["%s"] * len(accessible))
            where.append(f"f.aws_account_id IN ({placeholders})")
            params.extend(accessible)

        cursor.execute(f"""
            SELECT acc.id AS account_id, acc.account_name,
                   SUM(f.severity = 'HIGH')   AS high_count,
                   SUM(f.severity = 'MEDIUM') AS medium_count,
                   SUM(f.severity = 'LOW')    AS low_count,
                   COUNT(*) AS total_open
            FROM security_findings f
            JOIN aws_accounts acc ON acc.id = f.aws_account_id
            WHERE {' AND '.join(where)}
            GROUP BY acc.id, acc.account_name
            ORDER BY high_count DESC, total_open DESC
        """, tuple(params))
        return cursor.fetchall()
    finally:
        cursor.close(); conn.close()
