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
import copy
import json
import logging
import threading
import time
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
MAX_RESOURCES_PER_COMPONENT = 100

# The public endpoint is unauthenticated and costs 1 + 2*N queries on a
# pooled connection per hit; without a cache a trivial request flood
# drains the 10-connection pool (-> 500s on login). Per-process TTL
# cache; each uvicorn worker keeps its own copy.
PUBLIC_CACHE_TTL_SECONDS = 30
_public_cache = {"expires": 0.0, "payload": None}
_public_cache_lock = threading.Lock()


def _invalidate_public_cache() -> None:
    with _public_cache_lock:
        _public_cache["expires"] = 0.0
        _public_cache["payload"] = None


# ── ADMIN (authenticated) ───────────────────────────────────────────

def _validate_resource_ids(cursor, account_id: int, resource_ids) -> list:
    """Tenant isolation: every mapped resource_id must be a resource of
    the component's own account. Otherwise a status_page.manage holder
    scoped to account A could publish (unauthenticated!) the live alert
    status of account B's resources."""
    if not isinstance(resource_ids, list) or not resource_ids:
        raise HTTPException(status_code=400, detail="resource_ids must be a non-empty array")
    if len(resource_ids) > MAX_RESOURCES_PER_COMPONENT:
        raise HTTPException(status_code=400, detail=f"at most {MAX_RESOURCES_PER_COMPONENT} resource_ids per component")
    if not all(isinstance(r, str) and r for r in resource_ids):
        raise HTTPException(status_code=400, detail="resource_ids must be non-empty strings")
    unique_ids = list(dict.fromkeys(resource_ids))
    placeholders = ", ".join(["%s"] * len(unique_ids))
    cursor.execute(f"""
        SELECT DISTINCT resource_id FROM resources
        WHERE aws_account_id = %s AND resource_id IN ({placeholders})
    """, (account_id, *unique_ids))
    found = {row["resource_id"] if isinstance(row, dict) else row[0] for row in cursor.fetchall()}
    missing = [r for r in unique_ids if r not in found]
    if missing:
        raise HTTPException(status_code=400, detail="Some resource_ids do not belong to this account")
    return unique_ids


def _validate_name(raw) -> str:
    name = raw.strip() if isinstance(raw, str) else ""
    if not name or len(name) > 100:
        raise HTTPException(status_code=400, detail="name is required (max 100 characters)")
    return name

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
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="aws_account_id must be an integer")
    _require_account_access(account_id, current_user)

    name = _validate_name(payload.get("name"))
    try:
        display_order = int(payload.get("display_order", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="display_order must be an integer")

    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        resource_ids = _validate_resource_ids(cursor, account_id, payload.get("resource_ids"))
        cursor.execute("""
            INSERT INTO status_page_components
                (aws_account_id, name, resource_ids, display_order, enabled, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            account_id, name, json.dumps(resource_ids),
            display_order, bool(payload.get("enabled", True)),
            int(current_user["id"]),
        ))
        conn.commit()
        _invalidate_public_cache()
        return {"status": "created", "id": cursor.lastrowid}
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        logger.exception("create status page component failed")
        raise HTTPException(status_code=400, detail="Could not create component")
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
            updates["name"] = _validate_name(payload["name"])
        if "resource_ids" in payload:
            updates["resource_ids"] = json.dumps(
                _validate_resource_ids(cursor, row["aws_account_id"], payload["resource_ids"])
            )
        if "display_order" in payload:
            try:
                updates["display_order"] = int(payload["display_order"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="display_order must be an integer")
        if "enabled" in payload:
            updates["enabled"] = bool(payload["enabled"])
        if not updates:
            raise HTTPException(status_code=400, detail="No editable fields provided")

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cursor.execute(f"UPDATE status_page_components SET {set_clause} WHERE id = %s",
                        (*updates.values(), component_id))
        conn.commit()
        _invalidate_public_cache()
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
        _invalidate_public_cache()
        return {"status": "deleted"}
    finally:
        cursor.close(); conn.close()


# ── PUBLIC (no auth) ─────────────────────────────────────────────────

def _component_status(cursor, resource_ids: list, account_id: int = None) -> str:
    """Public status of one component. Uses the canonical FIRING state
    (app/alert_rules.py): stale, acknowledged, muted and maintenance-window
    alerts no longer turn a PUBLIC page red -- previously any status='active'
    row did, including alerts for a resource under planned maintenance and
    fake volume alerts, and matching was by resource_id alone (no account)."""
    if not resource_ids:
        return "operational"
    from app import alert_rules
    placeholders = ", ".join(["%s"] * len(resource_ids))
    acct_sql = " AND a.aws_account_id = %s" if account_id is not None else ""
    params = list(resource_ids) + ([account_id] if account_id is not None else [])
    cursor.execute(f"""
        SELECT DISTINCT UPPER(a.severity) AS severity
        {alert_rules.alert_base_from()}
        WHERE a.resource_id IN ({placeholders}){acct_sql}
          AND {alert_rules.firing_where()}
          AND {alert_rules.base_where()}
    """, tuple(params))
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
    now = time.monotonic()
    with _public_cache_lock:
        if _public_cache["payload"] is not None and now < _public_cache["expires"]:
            return copy.deepcopy(_public_cache["payload"])
    payload = _build_public_status_page()
    with _public_cache_lock:
        _public_cache["payload"] = payload
        _public_cache["expires"] = time.monotonic() + PUBLIC_CACHE_TTL_SECONDS
    return copy.deepcopy(payload)


def _build_public_status_page() -> dict:
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT c.id, c.aws_account_id, c.name, c.resource_ids
            FROM status_page_components c
            JOIN aws_accounts acc ON acc.id = c.aws_account_id AND acc.status = 'active'
            WHERE c.enabled = 1 ORDER BY c.display_order, c.name
        """)
        components_raw = cursor.fetchall()

        components = []
        overall = "operational"
        lookback = datetime.utcnow() - timedelta(hours=RECENT_EVENTS_LOOKBACK_HOURS)
        recent_events = []

        for c in components_raw:
            try:
                resource_ids = json.loads(c["resource_ids"]) or []
            except (TypeError, ValueError):
                logger.warning("status page component %s has malformed resource_ids -- skipped", c["id"])
                continue
            resource_ids = [r for r in resource_ids if isinstance(r, str)][:MAX_RESOURCES_PER_COMPONENT]
            status = _component_status(cursor, resource_ids, c["aws_account_id"])
            components.append({"name": c["name"], "status": status})
            if status == "outage":
                overall = "outage"
            elif status == "degraded" and overall != "outage":
                overall = "degraded"

            if resource_ids:
                placeholders = ", ".join(["%s"] * len(resource_ids))
                cursor.execute(f"""
                    SELECT a.severity, a.triggered_at, a.resolved_at
                    FROM alerts a
                    WHERE a.aws_account_id = %s
                      AND a.resource_id IN ({placeholders})
                      AND a.metric_name != 'multivariate_anomaly'
                      AND a.silenced = 0
                      AND (a.status = 'active' OR a.resolved_at >= %s)
                      AND COALESCE(a.resolution_reason, '') NOT IN
                          ('duplicate', 'placeholder_threshold', 'threshold_disabled',
                           'resource_gone', 'account_inactive', 'bulk_clear')
                    ORDER BY a.triggered_at DESC
                    LIMIT 5
                """, (c["aws_account_id"], *resource_ids, lookback))
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
