# app/api/notifications.py
"""
Notification channels (audit C9). Admin-only by default via notifications.manage.

  GET    /api/notifications/channels          list (targets masked: URLs show host only)
  POST   /api/notifications/channels          create
  PUT    /api/notifications/channels/{id}     update (omit `target` to keep the stored secret)
  DELETE /api/notifications/channels/{id}
  POST   /api/notifications/channels/{id}/test   send one test message now, report the real outcome
  GET    /api/notifications/log               last 100 delivery attempts

A channel's URL is a secret (a Slack/Teams incoming webhook is a bearer credential), so it is accepted
on write, never returned, and never written to the audit log.
"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from app.audit import write_audit
from app.auth.permissions import require_permission
from app.db import get_connection
from app.notifications import sender

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/notifications", tags=["Notifications"])



def _clean_events(value) -> str:
    if value is None:
        return "opened,escalated"
    items = value if isinstance(value, list) else str(value).split(",")
    items = [i.strip() for i in items if i and i.strip()]
    if not items or any(i not in sender.VALID_EVENTS for i in items):
        raise HTTPException(status_code=400, detail=f"events must be a non-empty subset of {list(sender.VALID_EVENTS)}")
    return ",".join(dict.fromkeys(items))


def _public(row: dict) -> dict:
    return {
        "id": row["id"], "name": row["name"], "type": row["type"],
        "target_preview": sender.mask_target(row["type"], row["target"]),
        "min_severity": row["min_severity"], "events": (row["events"] or "").split(","),
        "aws_account_id": row["aws_account_id"], "enabled": bool(row["enabled"]),
        "created_by": row.get("created_by"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def _validate_common(payload: dict, partial: bool = False) -> dict:
    out = {}
    if not partial or "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name or len(name) > 100:
            raise HTTPException(status_code=400, detail="name is required (max 100 characters)")
        out["name"] = name
    if not partial or "min_severity" in payload:
        sev = (payload.get("min_severity") or "CRITICAL").upper()
        if sev not in ("WARNING", "CRITICAL"):
            raise HTTPException(status_code=400, detail="min_severity must be WARNING or CRITICAL")
        out["min_severity"] = sev
    if not partial or "events" in payload:
        out["events"] = _clean_events(payload.get("events"))
    if "aws_account_id" in payload:
        v = payload.get("aws_account_id")
        out["aws_account_id"] = int(v) if v not in (None, "", 0) else None
    if "enabled" in payload:
        out["enabled"] = 1 if payload.get("enabled") else 0
    return out


def _fetch(cur, channel_id: int) -> dict:
    cur.execute("SELECT * FROM notification_channels WHERE id = %s", (channel_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Channel not found")
    return row


@router.get("/channels")
def list_channels(current_user: dict = Depends(require_permission("notifications.manage"))):
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM notification_channels ORDER BY name")
        return [_public(r) for r in cur.fetchall()]
    finally:
        conn.close()


@router.post("/channels")
def create_channel(request: Request, payload: dict = Body(...),
                   current_user: dict = Depends(require_permission("notifications.manage"))):
    fields = _validate_common(payload)
    ctype = (payload.get("type") or "").lower()
    try:
        fields["target"] = sender.validate_channel(ctype, payload.get("target"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    fields["type"] = ctype
    fields.setdefault("enabled", 1)
    fields["created_by"] = current_user["username"]
    cols = ", ".join(fields)
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id FROM notification_channels WHERE name = %s", (fields["name"],))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="A channel with that name already exists")
        cur.execute(f"INSERT INTO notification_channels ({cols}) VALUES ({', '.join(['%s'] * len(fields))})",
                    list(fields.values()))
        conn.commit()
        new_id = cur.lastrowid
        row = _fetch(cur, new_id)
    finally:
        conn.close()
    write_audit(current_user["username"], "Notification channel created",
                f"{fields['type']} channel '{fields['name']}'", role=current_user.get("role"), request=request)
    return _public(row)


@router.put("/channels/{channel_id}")
def update_channel(channel_id: int, request: Request, payload: dict = Body(...),
                   current_user: dict = Depends(require_permission("notifications.manage"))):
    fields = _validate_common(payload, partial=True)
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        row = _fetch(cur, channel_id)
        if payload.get("target"):              # omitted/empty target keeps the stored secret
            try:
                fields["target"] = sender.validate_channel(row["type"], payload["target"])
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        if not fields:
            return _public(row)
        if "name" in fields and fields["name"] != row["name"]:
            cur.execute("SELECT id FROM notification_channels WHERE name = %s AND id <> %s", (fields["name"], channel_id))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="A channel with that name already exists")
        sets = ", ".join(f"{k} = %s" for k in fields)
        cur.execute(f"UPDATE notification_channels SET {sets} WHERE id = %s", [*fields.values(), channel_id])
        conn.commit()
        row = _fetch(cur, channel_id)
    finally:
        conn.close()
    write_audit(current_user["username"], "Notification channel updated",
                f"channel '{row['name']}' (fields: {', '.join(sorted(k for k in fields if k != 'target')) or 'target'})",
                role=current_user.get("role"), request=request)
    return _public(row)


@router.delete("/channels/{channel_id}")
def delete_channel(channel_id: int, request: Request,
                   current_user: dict = Depends(require_permission("notifications.manage"))):
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        row = _fetch(cur, channel_id)
        cur.execute("DELETE FROM notification_channels WHERE id = %s", (channel_id,))
        conn.commit()
    finally:
        conn.close()
    write_audit(current_user["username"], "Notification channel deleted",
                f"channel '{row['name']}'", role=current_user.get("role"), request=request)
    return {"status": "deleted"}


@router.post("/channels/{channel_id}/test")
def test_channel(channel_id: int, request: Request,
                 current_user: dict = Depends(require_permission("notifications.manage"))):
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        row = _fetch(cur, channel_id)
    finally:
        conn.close()
    try:
        sender.send_test(row)
        ok, detail = True, "Test message delivered"
    except Exception as exc:
        ok, detail = False, sender._short_error(exc)
    write_audit(current_user["username"], "Notification channel tested",
                f"channel '{row['name']}': {'ok' if ok else 'failed'}", role=current_user.get("role"), request=request)
    return {"ok": ok, "detail": detail}


@router.get("/log")
def delivery_log(current_user: dict = Depends(require_permission("notifications.manage"))):
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, channel_name, alert_id, event, status, detail, created_at "
                    "FROM notification_log ORDER BY id DESC LIMIT 100")
        rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        r["created_at"] = r["created_at"].isoformat() if r.get("created_at") else None
    return rows
