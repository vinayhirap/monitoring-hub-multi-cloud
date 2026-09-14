# app/api/synthetic.py
"""
CRUD + results for synthetic_checks (db/migrations/031_synthetic_monitoring.sql).
Read gated on synthetic.view, write gated on synthetic.manage -- same
read/write permission split as topology.py (see
024_topology_manage_permission.sql's reasoning). Every endpoint is
scoped to the caller's accessible accounts via
get_accessible_account_ids(), same pattern as app/api/alerts.py.
"""
import logging
from fastapi import APIRouter, Body, HTTPException, Depends, Query
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/synthetic-checks", tags=["Synthetic Monitoring"])

_VALID_CHECK_TYPES = ("http", "tcp", "dns")


def _require_account_access(account_id: int, current_user: dict) -> None:
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")


def _get_check_account_id(check_id: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT aws_account_id FROM synthetic_checks WHERE id = %s", (check_id,))
        row = cur.fetchone()
        return row["aws_account_id"] if row else None
    finally:
        cur.close(); conn.close()


@router.get("")
def list_checks(current_user: dict = Depends(require_permission("synthetic.view"))):
    """
    Includes a lightweight uptime_pct_24h computed inline from
    synthetic_check_results -- avoids a second round trip from the
    frontend's list view for the single number people actually look at
    first (Pingdom/UptimeRobot's own list views lead with the same
    number for the same reason).
    """
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT c.*, acc.account_name,
                   (SELECT ROUND(100 * AVG(r.success), 1)
                    FROM synthetic_check_results r
                    WHERE r.check_id = c.id
                      AND r.checked_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)) AS uptime_pct_24h
            FROM synthetic_checks c
            JOIN aws_accounts acc ON acc.id = c.aws_account_id
            ORDER BY c.name
        """)
        rows = cur.fetchall()
        if accessible is not None:
            rows = [r for r in rows if r["aws_account_id"] in accessible]
        return rows
    finally:
        cur.close(); conn.close()


@router.get("/{check_id}/results")
def get_check_results(
    check_id: int,
    hours: int = Query(24, ge=1, le=720),
    current_user: dict = Depends(require_permission("synthetic.view")),
):
    account_id = _get_check_account_id(check_id)
    if account_id is None:
        raise HTTPException(status_code=404, detail="Check not found")
    _require_account_access(account_id, current_user)

    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT checked_at, success, response_time_ms, status_code, error_message
            FROM synthetic_check_results
            WHERE check_id = %s AND checked_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)
            ORDER BY checked_at ASC
        """, (check_id, hours))
        results = cur.fetchall()
        uptime_pct = round(100 * sum(r["success"] for r in results) / len(results), 2) if results else None
        return {"check_id": check_id, "window_hours": hours, "uptime_pct": uptime_pct, "results": results}
    finally:
        cur.close(); conn.close()


@router.post("")
def create_check(payload: dict = Body(...), current_user: dict = Depends(require_permission("synthetic.manage"))):
    account_id = payload.get("aws_account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="aws_account_id is required")
    _require_account_access(int(account_id), current_user)

    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    check_type = payload.get("check_type", "http")
    if check_type not in _VALID_CHECK_TYPES:
        raise HTTPException(status_code=400, detail=f"check_type must be one of {_VALID_CHECK_TYPES}")

    target = (payload.get("target") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="target is required")
    if check_type == "http" and not (target.startswith("http://") or target.startswith("https://")):
        raise HTTPException(status_code=400, detail="http checks need a full URL (http:// or https://)")

    interval_seconds = int(payload.get("interval_seconds", 300))
    if interval_seconds < 60:
        # Below this, the 2-min critical-tier scheduler cadence
        # (see scheduler.py's run_loop) can't honor the interval
        # anyway -- reject rather than silently running slower than
        # configured.
        raise HTTPException(status_code=400, detail="interval_seconds must be at least 60")

    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO synthetic_checks
                (aws_account_id, name, check_type, target, expected_status_code,
                 expected_keyword, timeout_seconds, interval_seconds,
                 consecutive_failure_threshold, environment, enabled, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            int(account_id), name, check_type, target,
            payload.get("expected_status_code"), payload.get("expected_keyword"),
            int(payload.get("timeout_seconds", 10)), interval_seconds,
            int(payload.get("consecutive_failure_threshold", 2)),
            payload.get("environment", "prod"),
            bool(payload.get("enabled", True)),
            int(current_user["id"]),
        ))
        conn.commit()
        return {"status": "created", "id": cur.lastrowid}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close(); conn.close()


@router.patch("/{check_id}")
def update_check(check_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("synthetic.manage"))):
    account_id = _get_check_account_id(check_id)
    if account_id is None:
        raise HTTPException(status_code=404, detail="Check not found")
    _require_account_access(account_id, current_user)

    editable_fields = (
        "name", "target", "expected_status_code", "expected_keyword",
        "timeout_seconds", "interval_seconds", "consecutive_failure_threshold",
        "environment", "enabled",
    )
    updates = {k: v for k, v in payload.items() if k in editable_fields}
    if not updates:
        raise HTTPException(status_code=400, detail="No editable fields provided")
    if "interval_seconds" in updates and int(updates["interval_seconds"]) < 60:
        raise HTTPException(status_code=400, detail="interval_seconds must be at least 60")

    set_clause = ", ".join(f"{k} = %s" for k in updates)
    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute(
            f"UPDATE synthetic_checks SET {set_clause} WHERE id = %s",
            (*updates.values(), check_id),
        )
        conn.commit()
        return {"status": "updated"}
    finally:
        cur.close(); conn.close()


@router.delete("/{check_id}")
def delete_check(check_id: int, current_user: dict = Depends(require_permission("synthetic.manage"))):
    account_id = _get_check_account_id(check_id)
    if account_id is None:
        raise HTTPException(status_code=404, detail="Check not found")
    _require_account_access(account_id, current_user)

    conn = get_connection(); cur = conn.cursor()
    try:
        # Resolve any still-active alert for this check's synthetic
        # resource first -- deleting the check shouldn't leave a
        # permanently-open alert with no configuration behind it.
        cur.execute("""
            UPDATE alerts SET status = 'resolved', resolved_at = NOW()
            WHERE resource_id = %s AND metric_name = 'synthetic_uptime' AND status = 'active'
        """, (f"synthetic-{check_id}",))
        cur.execute("DELETE FROM synthetic_checks WHERE id = %s", (check_id,))
        conn.commit()
        return {"status": "deleted"}
    finally:
        cur.close(); conn.close()
