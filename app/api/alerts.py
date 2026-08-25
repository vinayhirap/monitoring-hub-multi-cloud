# app/api/alerts.py
from typing import Optional
import datetime
import time
import logging
from fastapi import APIRouter, HTTPException, Depends
from app.db import get_connection
from app.auth.deps import get_current_user
from app.aws.federation import (
    build_federated_console_url,
    resource_console_destination,
    NoConsoleCredentialsError,
)
from app.ws.publisher import publish_alert_resolved

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
def get_alerts():
    now = time.time()
    if _alerts_cache["data"] is not None and now - _alerts_cache["ts"] < _CACHE_TTL:
        return _alerts_cache["data"]
    rows = _fetch_alerts_from_db()
    _alerts_cache["data"] = rows
    _alerts_cache["ts"]   = now
    return rows


# ── GET open/active only ──────────────────────────────────────
@router.get("/open")
def open_alerts():
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
def get_console_url(alert_id: int, user: dict = Depends(get_current_user)):
    """
    Returns a federated sign-in URL that opens THIS alert's resource in
    THIS alert's AWS account — regardless of which account the operator's
    browser currently happens to be signed into.
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            a.resource_id                          AS resource,
            r.resource_type                        AS resource_type,
            r.name                                  AS resource_name,
            COALESCE(a.region, acc.default_region) AS region,
            acc.account_id                         AS aws_account_id,
            acc.role_arn,
            acc.external_id
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

    destination = resource_console_destination(
        row.get("resource_type"), row["resource"], row["region"],
        resource_name=row.get("resource_name"),
    )

    try:
        url = build_federated_console_url(
            row.get("role_arn"), row.get("external_id"), destination,
            target_account_id=row.get("aws_account_id"),
            requested_by=user["username"],
            service=row.get("resource_type"), resource_id=row["resource"],
            region=row["region"], resource_name=row.get("resource_name"),
        )
    except NoConsoleCredentialsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Failed to build federated console URL for alert %s", alert_id)
        raise HTTPException(status_code=502, detail="Could not generate AWS console link")

    return {"url": url, "account_id": row["aws_account_id"]}


# ── ACK ───────────────────────────────────────────────────────
@router.post("/{alert_id}/ack")
@router.patch("/{alert_id}/ack")
def ack_alert(alert_id: int):
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
    return {"status": "acknowledged"}


# ── RESOLVE ───────────────────────────────────────────────────
@router.post("/{alert_id}/resolve")
@router.patch("/{alert_id}/resolve")
def resolve_alert(alert_id: int):
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

    try:
        publish_alert_resolved(alert_id=alert_id, account_id=row["account_id"] if row else None)
    except Exception as e:
        logger.warning(f"Resolve publish failed: {e}")

    return {"status": "resolved", "alert_id": alert_id}


# ── MUTE ──────────────────────────────────────────────────────
@router.post("/{alert_id}/mute")
def mute_alert(alert_id: int, minutes: int = 30):
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
    return {"status": "muted", "minutes": minutes}


# ── CLEAR ─────────────────────────────────────────────────────
@router.delete("/clear")
def clear_alerts():
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM alerts WHERE resolved_at IS NULL AND acked = 0")
    conn.commit()
    affected = cur.rowcount
    cur.close()
    conn.close()
    _invalidate_cache()
    return {"status": "cleared", "count": affected}