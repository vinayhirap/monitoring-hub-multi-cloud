# app/api/maintenance.py
"""
CRUD for maintenance_windows (db/migrations/036_maintenance_windows.sql).
Read gated on maintenance.view, write gated on maintenance.manage --
same split as app/api/synthetic.py. Actual silencing is applied by the
background job in app/collector/maintenance.py, not by this API --
creating a window here just declares intent; the next "critical" tier
scheduler cycle (within ~2 min of starts_at) is what actually flips
alerts.silenced.
"""
import logging
from fastapi import APIRouter, Body, HTTPException, Depends
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/maintenance-windows", tags=["Maintenance Windows"])


def _require_account_access(account_id: int, current_user: dict) -> None:
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")


@router.get("")
def list_windows(current_user: dict = Depends(require_permission("maintenance.view"))):
    """Includes is_active (computed from starts_at/ends_at vs NOW())
    so the UI doesn't need to do its own clock comparison."""
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT w.*, acc.account_name,
                   (w.starts_at <= NOW() AND w.ends_at >= NOW()) AS is_active
            FROM maintenance_windows w
            JOIN aws_accounts acc ON acc.id = w.aws_account_id
            ORDER BY w.starts_at DESC
        """)
        rows = cursor.fetchall()
        if accessible is not None:
            rows = [r for r in rows if r["aws_account_id"] in accessible]
        return rows
    finally:
        cursor.close(); conn.close()


@router.post("")
def create_window(payload: dict = Body(...), current_user: dict = Depends(require_permission("maintenance.manage"))):
    account_id = payload.get("aws_account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="aws_account_id is required")
    _require_account_access(int(account_id), current_user)

    resource_id = (payload.get("resource_id") or "").strip()
    if not resource_id:
        raise HTTPException(status_code=400, detail="resource_id is required")

    reason = (payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")

    starts_at = payload.get("starts_at")
    ends_at = payload.get("ends_at")
    if not starts_at or not ends_at:
        raise HTTPException(status_code=400, detail="starts_at and ends_at are required (ISO datetime)")

    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        # Confirm the resource actually belongs to this account -- a
        # typo'd resource_id would otherwise silently create a
        # maintenance window that matches nothing, ever.
        cursor.execute("SELECT 1 FROM resources WHERE resource_id = %s AND aws_account_id = %s",
                        (resource_id, int(account_id)))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="resource_id not found in this account")

        cursor.execute("""
            INSERT INTO maintenance_windows
                (aws_account_id, resource_id, reason, starts_at, ends_at, silence_downstream, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            int(account_id), resource_id, reason, starts_at, ends_at,
            bool(payload.get("silence_downstream", True)), int(current_user["id"]),
        ))
        conn.commit()
        return {"status": "created", "id": cursor.lastrowid}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close(); conn.close()


@router.patch("/{window_id}")
def update_window(window_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("maintenance.manage"))):
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT aws_account_id FROM maintenance_windows WHERE id = %s", (window_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Maintenance window not found")
        _require_account_access(row["aws_account_id"], current_user)

        editable = ("reason", "starts_at", "ends_at", "silence_downstream")
        updates = {k: v for k, v in payload.items() if k in editable}
        if not updates:
            raise HTTPException(status_code=400, detail="No editable fields provided")

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cursor.execute(f"UPDATE maintenance_windows SET {set_clause} WHERE id = %s", (*updates.values(), window_id))
        conn.commit()
        return {"status": "updated"}
    finally:
        cursor.close(); conn.close()


@router.delete("/{window_id}")
def delete_window(window_id: int, current_user: dict = Depends(require_permission("maintenance.manage"))):
    """Deleting an active window doesn't leave alerts stuck silenced --
    the next sync_maintenance_silencing() cycle (within ~2 min) sees
    the window is gone and un-silences everything it was covering."""
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT aws_account_id FROM maintenance_windows WHERE id = %s", (window_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Maintenance window not found")
        _require_account_access(row["aws_account_id"], current_user)

        cursor.execute("DELETE FROM maintenance_windows WHERE id = %s", (window_id,))
        conn.commit()
        return {"status": "deleted"}
    finally:
        cursor.close(); conn.close()
