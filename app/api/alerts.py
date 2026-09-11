# app/api/alerts.py
from typing import Optional
import datetime
import time
import logging
from fastapi import APIRouter, HTTPException, Depends
from app.db import get_connection
from app.auth.deps import get_current_user, require_role
from app.auth.permissions import require_permission
from app.aws.federation import NoConsoleCredentialsError
from app.ws.publisher import publish_alert_resolved
from app.api.live_data import invalidate_accounts_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts", tags=["Alerts"])

# Simple in-process cache — alerts list doesn't change sub-second
_alerts_cache: dict = {"data": None, "ts": 0}
_CACHE_TTL = 15  # seconds — short enough for near-realtime, avoids hammering DB

# An active alert whose last_seen_at hasn't been touched in this long has
# stopped getting fresh metric data -- surfaced to the UI as "stale / no
# data" so it's not mistaken for a live, just-reconfirmed breach. It is
# NOT auto-resolved (see 008_revert_falsely_resolved_alerts.sql) -- this
# is display-only, the operator decides whether to resolve it.
_STALE_AFTER_MINUTES = 20


def _invalidate_cache():
    _alerts_cache["data"] = None
    _alerts_cache["ts"]   = 0

def _fetch_alerts_from_db():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            a.id,
            a.resource_id                          AS resource,
            COALESCE(a.region, acc.default_region) AS region,
            a.metric_name,
            a.severity,
            a.status,
            a.current_value,
            a.threshold,
            a.value,
            CONVERT_TZ(a.triggered_at, @@session.time_zone, '+00:00') AS triggered_at,
            CONVERT_TZ(a.resolved_at,  @@session.time_zone, '+00:00') AS resolved_at,
            CONVERT_TZ(a.last_seen_at, @@session.time_zone, '+00:00') AS last_seen_at,
            (a.status = 'active'
             AND a.last_seen_at IS NOT NULL
             AND a.last_seen_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL {stale} MINUTE)
            ) AS stale,
            a.acked,
            a.muted_until,
            a.environment,
            r.resource_type                        AS service,
            COALESCE(r.name, a.resource_id)        AS resource_name,
            acc.account_name,
            acc.id                                 AS account_id
        FROM alerts a
        JOIN resources r      ON r.resource_id = a.resource_id
        JOIN aws_accounts acc ON acc.id = r.aws_account_id
                               AND acc.status = 'active'
        ORDER BY a.triggered_at DESC
        LIMIT 200
    """.format(stale=_STALE_AFTER_MINUTES))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    for r in rows:
        for field in ("triggered_at", "resolved_at", "last_seen_at"):
            if r.get(field) and isinstance(r[field], datetime.datetime):
                r[field] = r[field].strftime("%Y-%m-%dT%H:%M:%SZ")
            elif r.get(field) and isinstance(r[field], str) and not r[field].endswith("Z"):
                r[field] = r[field].rstrip("+00:00").rstrip(" UTC") + "Z"
        r["stale"] = bool(r.get("stale"))

    return rows


# ── GET all alerts (cached) ───────────────────────────────────
@router.get("")
def get_alerts(current_user: dict = Depends(require_permission("alerts.view"))):
    now = time.time()
    if _alerts_cache["data"] is not None and now - _alerts_cache["ts"] < _CACHE_TTL:
        return _alerts_cache["data"]
    rows = _fetch_alerts_from_db()
    _alerts_cache["data"] = rows
    _alerts_cache["ts"]   = now
    return rows


# ── GET open/active only ──────────────────────────────────────
@router.get("/open")
def open_alerts(current_user: dict = Depends(require_permission("alerts.view"))):
    """
    Returns only unresolved alerts — used by Overview alert strip + api.js getAlerts().
    Also cached. Invalidated on ack/resolve.
    """
    now = time.time()
    # Reuse full cache if available, filter client-side to avoid second DB call
    if _alerts_cache["data"] is not None and now - _alerts_cache["ts"] < _CACHE_TTL:
        return [a for a in _alerts_cache["data"] if a.get("status") != "resolved"]

    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            a.id,
            a.resource_id                          AS resource,
            a.metric_name,
            a.severity,
            a.status,
            a.current_value,
            a.threshold,
            a.value,
            CONVERT_TZ(a.triggered_at, @@session.time_zone, '+00:00') AS triggered_at,
            CONVERT_TZ(a.resolved_at,  @@session.time_zone, '+00:00') AS resolved_at,
            CONVERT_TZ(a.last_seen_at, @@session.time_zone, '+00:00') AS last_seen_at,
            (a.status = 'active'
             AND a.last_seen_at IS NOT NULL
             AND a.last_seen_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL {stale} MINUTE)
            ) AS stale,
            a.acked,
            a.environment,
            r.resource_type                        AS service,
            COALESCE(r.name, a.resource_id)        AS resource_name,
            acc.account_name,
            acc.id                                 AS account_id,
            COALESCE(a.region, acc.default_region) AS region
        FROM alerts a
        JOIN resources r      ON r.resource_id = a.resource_id
        JOIN aws_accounts acc ON acc.id = r.aws_account_id
                               AND acc.status = 'active'
        WHERE a.resolved_at IS NULL
            ORDER BY
            FIELD(a.severity, 'CRITICAL', 'WARNING', 'INFO'),
            a.triggered_at DESC
        LIMIT 100
    """.format(stale=_STALE_AFTER_MINUTES))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    for r in rows:
        for field in ("triggered_at", "resolved_at", "last_seen_at"):
            if r.get(field) and isinstance(r[field], datetime.datetime):
                r[field] = r[field].strftime("%Y-%m-%dT%H:%M:%SZ")
            elif r.get(field) and isinstance(r[field], str) and not r[field].endswith("Z"):
                r[field] = r[field].rstrip("+00:00").rstrip(" UTC") + "Z"
        r["stale"] = bool(r.get("stale"))

    return rows


# ── AWS CONSOLE DEEP-LINK (account-correct) ────────────────────
@router.get("/{alert_id}/console-url")
def get_console_url(alert_id: int, user: dict = Depends(require_permission("alerts.view"))):
    """
    Returns a console deep link that opens THIS alert's resource in THIS
    alert's account -- regardless of which account/cloud the operator's
    browser currently happens to be signed into.

    Dispatches through the provider layer (get_provider().get_console_url)
    the same way app/api/admin/accounts.py's sibling endpoint already
    does -- this one was the one place that migration was never finished,
    which meant no Azure/GCP alert could ever produce a working console
    link (AWS's federation helpers were being called directly regardless
    of the alert's actual account provider).
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            a.resource_id                          AS resource,
            r.resource_type                        AS resource_type,
            r.name                                  AS resource_name,
            COALESCE(a.region, acc.default_region) AS region,
            acc.*
        FROM alerts a
        JOIN resources r      ON r.resource_id = a.resource_id
        JOIN aws_accounts acc ON acc.id = r.aws_account_id
        WHERE a.id = %s
    """, (alert_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")

    try:
        from app.providers.registry import get_provider
        provider = get_provider(row.get("provider") or "aws")
        url = provider.get_console_url(
            row, row["resource"], row["region"],
            service=row.get("resource_type"), resource_name=row.get("resource_name"),
            requested_by=user["username"],
        )
    except NoConsoleCredentialsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Failed to build console URL for alert %s", alert_id)
        raise HTTPException(status_code=502, detail="Could not generate console link")

    return {"url": url, "account_id": row["account_id"]}


# ── ACK ───────────────────────────────────────────────────────
@router.post("/{alert_id}/ack")
@router.patch("/{alert_id}/ack")
def ack_alert(alert_id: int, current_user: dict = Depends(require_permission("operations.execute"))):
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE alerts SET acked = 1, status = 'acknowledged' WHERE id = %s",
        (alert_id,)
    )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Alert not found")
    conn.commit()
    cursor.close()
    conn.close()
    _invalidate_cache()
    invalidate_accounts_cache()
    return {"status": "acknowledged"}


# ── RESOLVE ───────────────────────────────────────────────────
@router.post("/{alert_id}/resolve")
@router.patch("/{alert_id}/resolve")
def resolve_alert(alert_id: int, current_user: dict = Depends(require_permission("operations.execute"))):
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "UPDATE alerts SET resolved_at = UTC_TIMESTAMP(), last_seen_at = UTC_TIMESTAMP(), "
        "status = 'resolved' WHERE id = %s",
        (alert_id,)
    )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Alert not found")
    conn.commit()

    cursor.execute("""
        SELECT acc.id AS account_id
        FROM alerts a
        JOIN resources r      ON r.resource_id = a.resource_id
        JOIN aws_accounts acc ON acc.id = r.aws_account_id
        WHERE a.id = %s
    """, (alert_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    _invalidate_cache()
    invalidate_accounts_cache()

    try:
        publish_alert_resolved(alert_id=alert_id, account_id=row["account_id"] if row else None)
    except Exception as e:
        logger.warning(f"Resolve publish failed: {e}")

    return {"status": "resolved", "alert_id": alert_id}


# ── MUTE ──────────────────────────────────────────────────────
@router.post("/{alert_id}/mute")
def mute_alert(alert_id: int, minutes: int = 30, current_user: dict = Depends(require_permission("operations.execute"))):
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE alerts SET muted_until = DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s MINUTE) WHERE id = %s",
        (minutes, alert_id)
    )
    conn.commit()
    cursor.close()
    conn.close()
    _invalidate_cache()
    invalidate_accounts_cache()
    return {"status": "muted", "minutes": minutes}


# ── CLEAR ─────────────────────────────────────────────────────
@router.delete("/clear")
def clear_alerts(current_user: dict = Depends(require_role("admin"))):
    # Admin-only: bulk-deletes every unresolved/unacked alert with no
    # undo. No existing permission code covers a bulk-destructive action
    # like this (operations.execute covers acting on ONE alert), so this
    # is intentionally locked tighter than the single-alert actions above.
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM alerts WHERE resolved_at IS NULL AND acked = 0")
    conn.commit()
    affected = cur.rowcount
    cur.close()
    conn.close()
    _invalidate_cache()
    invalidate_accounts_cache()
    return {"status": "cleared", "count": affected}