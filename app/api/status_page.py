# app/api/status_page.py
"""
Public status page (db/migrations/037_status_page.sql -- see its
module docstring for the full design and the "never leak internal
details" constraint). Two halves in this file:

  ADMIN (authenticated, status_page.manage): CRUD for which
  resource_ids map to which public-facing component name.

  PUBLIC (no auth at all -- see main.py's router registration, same
  pattern as app/api/sso.py/webhooks.py): GET /api/status-page. Every
  response field is built fresh from component NAMES and computed
  statuses only -- resource_ids, account names, alert internals never
  appear in this endpoint's output. If you're editing this file, treat
  that boundary as load-bearing: it's the only thing standing between
  "public status page" and "unauthenticated internal-infrastructure
  disclosure".
"""
import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, HTTPException, Depends
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids

logger = logging.getLogger(__name__)

admin_router = APIRouter(prefix="/api/status-page/components", tags=["Status Page (admin)"])
public_router = APIRouter(prefix="/api/status-page", tags=["Status Page (public)"])

RECENT_EVENTS_LOOKBACK_HOURS = 24
MAX_RECENT_EVENTS = 10


# ── ADMIN (authenticated) ───────────────────────────────────────────

def _require_account_access(account_id: int, current_user: dict) -> None:
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")


@admin_router.get("")
def list_components(current_user: dict = Depends(require_permission("status_page.manage"))):
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT c.*, acc.account_name
            FROM status_page_components c
            JOIN aws_accounts acc ON acc.id = c.aws_account_id
            ORDER BY c.display_order, c.name
        """)
        rows = cursor.fetchall()
        if accessible is not None:
            rows = [r for r in rows if r["aws_account_id"] in accessible]
        for row in rows:
            row["resource_ids"] = json.loads(row["resource_ids"])
        return rows
    finally:
        cursor.close(); conn.close()


@admin_router.post("")
def create_component(payload: dict = Body(...), current_user: dict = Depends(require_permission("status_page.manage"))):
    account_id = payload.get("aws_account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="aws_account_id is required")
    _require_account_access(int(account_id), current_user)

    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    resource_ids = payload.get("resource_ids")
    if not resource_ids or not isinstance(resource_ids, list):
        raise HTTPException(status_code=400, detail="resource_ids must be a non-empty array")

    conn = get_connection(); cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO status_page_components
                (aws_account_id, name, resource_ids, display_order, enabled, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            int(account_id), name, json.dumps(resource_ids),
            int(payload.get("display_order", 0)), bool(payload.get("enabled", True)),
            int(current_user["id"]),
        ))
        conn.commit()
        return {"status": "created", "id": cursor.lastrowid}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close(); conn.close()


@admin_router.patch("/{component_id}")
def update_component(component_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("status_page.manage"))):
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT aws_account_id FROM status_page_components WHERE id = %s", (component_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Component not found")
        _require_account_access(row["aws_account_id"], current_user)

        updates = {}
        if "name" in payload:
            updates["name"] = payload["name"]
        if "resource_ids" in payload:
            if not isinstance(payload["resource_ids"], list):
                raise HTTPException(status_code=400, detail="resource_ids must be an array")
            updates["resource_ids"] = json.dumps(payload["resource_ids"])
        if "display_order" in payload:
            updates["display_order"] = payload["display_order"]
        if "enabled" in payload:
            updates["enabled"] = payload["enabled"]
        if not updates:
            raise HTTPException(status_code=400, detail="No editable fields provided")

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cursor.execute(f"UPDATE status_page_components SET {set_clause} WHERE id = %s",
                        (*updates.values(), component_id))
        conn.commit()
        return {"status": "updated"}
    finally:
        cursor.close(); conn.close()


@admin_router.delete("/{component_id}")
def delete_component(component_id: int, current_user: dict = Depends(require_permission("status_page.manage"))):
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT aws_account_id FROM status_page_components WHERE id = %s", (component_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Component not found")
        _require_account_access(row["aws_account_id"], current_user)

        cursor.execute("DELETE FROM status_page_components WHERE id = %s", (component_id,))
        conn.commit()
        return {"status": "deleted"}
    finally:
        cursor.close(); conn.close()


# ── PUBLIC (no auth) ─────────────────────────────────────────────────

def _component_status(cursor, resource_ids: list) -> str:
    if not resource_ids:
        return "operational"
    placeholders = ", ".join(["%s"] * len(resource_ids))
    cursor.execute(f"""
        SELECT severity FROM alerts
        WHERE resource_id IN ({placeholders}) AND status = 'active'
          AND metric_name != 'multivariate_anomaly'
    """, tuple(resource_ids))
    severities = {row["severity"] for row in cursor.fetchall()}
    if "CRITICAL" in severities:
        return "outage"
    if "WARNING" in severities:
        return "degraded"
    return "operational"


@public_router.get("")
def public_status_page():
    """
    NO AUTHENTICATION -- see module docstring's sanitization boundary.
    Returns only: component names, computed statuses, and a short
    recent-events list built the same sanitized way. Never exposes
    resource_ids, account names, alert IDs, or any other internal
    identifier.
    """
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, name, resource_ids FROM status_page_components
            WHERE enabled = 1 ORDER BY display_order, name
        """)
        components_raw = cursor.fetchall()

        components = []
        overall = "operational"
        lookback = datetime.utcnow() - timedelta(hours=RECENT_EVENTS_LOOKBACK_HOURS)
        recent_events = []

        for c in components_raw:
            resource_ids = json.loads(c["resource_ids"])
            status = _component_status(cursor, resource_ids)
            components.append({"name": c["name"], "status": status})
            if status == "outage":
                overall = "outage"
            elif status == "degraded" and overall != "outage":
                overall = "degraded"

            if resource_ids:
                placeholders = ", ".join(["%s"] * len(resource_ids))
                cursor.execute(f"""
                    SELECT severity, triggered_at, resolved_at
                    FROM alerts
                    WHERE resource_id IN ({placeholders})
                      AND metric_name != 'multivariate_anomaly'
                      AND (status = 'active' OR resolved_at >= %s)
                      AND triggered_at >= %s
                    ORDER BY triggered_at DESC
                    LIMIT 5
                """, (*resource_ids, lookback, lookback))
                for row in cursor.fetchall():
                    recent_events.append({
                        "component": c["name"],
                        "status": "outage" if row["severity"] == "CRITICAL" else "degraded",
                        # +"Z" is load-bearing, not decorative: triggered_at/
                        # resolved_at come out of MySQL as naive datetimes
                        # (this DB's NOW() is confirmed plain UTC with no
                        # offset -- see the audit that added this fix), and
                        # str(naive_datetime) produces "2026-09-16 06:46:33"
                        # with no timezone marker at all. Browsers parse a
                        # timestamp with no 'Z'/offset as LOCAL time per the
                        # ES2015+ Date-parsing spec -- so without this, every
                        # viewer's browser silently mis-parsed a UTC instant
                        # as if it were already their own local time, no
                        # matter what timezone selector they had (see
                        # StatusPagePublic.jsx's now-fixed toLocaleString()
                        # calls, and TimezoneContext.jsx's formatInTz, which
                        # both assume a real, unambiguous instant on input).
                        "started_at": str(row["triggered_at"]) + "Z",
                        "resolved_at": (str(row["resolved_at"]) + "Z") if row["resolved_at"] else None,
                    })

        recent_events.sort(key=lambda e: e["started_at"], reverse=True)

        return {
            "overall_status": overall,
            "components": components,
            "recent_events": recent_events[:MAX_RECENT_EVENTS],
            # Same "no offset = browser treats it as local time" issue as
            # started_at/resolved_at above -- isoformat() alone omits the
            # 'Z' even though datetime.utcnow() genuinely is UTC.
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }
    finally:
        cursor.close(); conn.close()
