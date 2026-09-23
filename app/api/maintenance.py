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
import datetime
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


# AUDIT(b06): maintenance_windows.starts_at/ends_at are naive DATETIMEs
# compared against the DB clock by app/collector/maintenance.py. The UI
# used to post raw <input type="datetime-local"> values (browser-local
# wall-clock, no offset), so an IST operator's 14:00-15:00 window was
# stored as 14:00 and only became active at 14:00 UTC (19:30 IST) --
# silencing ran 5h30m late and pages went out during the real
# maintenance. All times are now normalised to naive UTC here; an
# offset-less value is interpreted as UTC (the UI now always sends one).
_MAX_WINDOW = datetime.timedelta(days=31)


def _parse_utc(value, field: str) -> datetime.datetime:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"{field} must be an ISO-8601 datetime")
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be an ISO-8601 datetime")
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _validate_range(starts_at: datetime.datetime, ends_at: datetime.datetime) -> None:
    if ends_at <= starts_at:
        raise HTTPException(status_code=400, detail="ends_at must be after starts_at")
    if ends_at - starts_at > _MAX_WINDOW:
        raise HTTPException(status_code=400, detail="A maintenance window may not exceed 31 days")


def _iso_z(value):
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


@router.get("")
def list_windows(current_user: dict = Depends(require_permission("maintenance.view"))):
    """Includes is_active (computed from starts_at/ends_at vs NOW())
    so the UI doesn't need to do its own clock comparison."""
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT w.*, acc.account_name,
                   (w.starts_at <= UTC_TIMESTAMP() AND w.ends_at >= UTC_TIMESTAMP()) AS is_active
            FROM maintenance_windows w
            JOIN aws_accounts acc ON acc.id = w.aws_account_id
            ORDER BY w.starts_at DESC
        """)
        rows = cursor.fetchall()
        if accessible is not None:
            rows = [r for r in rows if r["aws_account_id"] in accessible]
        for r in rows:
            r["is_active"] = bool(r.get("is_active"))
            for f in ("starts_at", "ends_at", "created_at"):
                r[f] = _iso_z(r.get(f))
        return rows
    finally:
        cursor.close(); conn.close()


@router.post("")
def create_window(payload: dict = Body(...), current_user: dict = Depends(require_permission("maintenance.manage"))):
    account_id = payload.get("aws_account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="aws_account_id is required")
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="aws_account_id must be an integer")
    _require_account_access(account_id, current_user)

    resource_id = (payload.get("resource_id") or "").strip()
    if not resource_id:
        raise HTTPException(status_code=400, detail="resource_id is required")

    reason = (payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")

    if not payload.get("starts_at") or not payload.get("ends_at"):
        raise HTTPException(status_code=400, detail="starts_at and ends_at are required (ISO datetime)")
    starts_at = _parse_utc(payload.get("starts_at"), "starts_at")
    ends_at = _parse_utc(payload.get("ends_at"), "ends_at")
    _validate_range(starts_at, ends_at)

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
    except Exception:
        conn.rollback()
        # AUDIT(b06): never echo raw DB exception text to the client.
        logger.exception("Failed to create maintenance window")
        raise HTTPException(status_code=400, detail="Could not create maintenance window")
    finally:
        cursor.close(); conn.close()


@router.patch("/{window_id}")
def update_window(window_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("maintenance.manage"))):
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT aws_account_id, starts_at, ends_at FROM maintenance_windows WHERE id = %s",
                       (window_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Maintenance window not found")
        _require_account_access(row["aws_account_id"], current_user)

        editable = ("reason", "starts_at", "ends_at", "silence_downstream")
        updates = {k: v for k, v in payload.items() if k in editable}
        if not updates:
            raise HTTPException(status_code=400, detail="No editable fields provided")
        # AUDIT(b06): same UTC normalisation + range check as create; the
        # old code wrote whatever strings it was given straight to SQL.
        if "starts_at" in updates:
            updates["starts_at"] = _parse_utc(updates["starts_at"], "starts_at")
        if "ends_at" in updates:
            updates["ends_at"] = _parse_utc(updates["ends_at"], "ends_at")
        if "starts_at" in updates or "ends_at" in updates:
            _validate_range(updates.get("starts_at", row["starts_at"]),
                            updates.get("ends_at", row["ends_at"]))
        if "reason" in updates:
            updates["reason"] = (str(updates["reason"] or "")).strip()
            if not updates["reason"]:
                raise HTTPException(status_code=400, detail="reason cannot be empty")
        if "silence_downstream" in updates:
            updates["silence_downstream"] = bool(updates["silence_downstream"])

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
