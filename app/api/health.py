# app/api/health.py
"""
Health endpoints for CloudOps itself (audit C5: the monitoring product did not
monitor itself, and /health, /status served the SPA HTML with 200, which defeats
load-balancer health checks).

  GET /api/health/live    public, no I/O. Process is up. Use for liveness.
  GET /api/health/ready   public, one `SELECT 1`. 200 when the DB answers, else 503.
                          Body is deliberately minimal (no versions, hosts or counts).
  GET /api/health/detail  requires operations.view (admin always passes). DB latency and
                          collector freshness per the accounts this deployment monitors.

Collector freshness is read from aws_accounts.last_synced_at, which the metrics
runner stamps after each account sync. An account is "stale" when that stamp is older
than STALE_AFTER_SECONDS (3 x the 15-minute low tier). With no active accounts the
collector status is "idle", not "stale".
"""
import os
import time

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.db import get_connection
from app.auth.permissions import require_permission

router = APIRouter(prefix="/api/health", tags=["health"])

STALE_AFTER_SECONDS = 45 * 60
VERSION = "0.3.0"


@router.get("/live")
def live():
    return {"status": "ok"}


def _db_ping():
    """-> (ok, latency_ms). Never raises."""
    started = time.monotonic()
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT 1")
                cur.fetchone()
            finally:
                cur.close()
        finally:
            conn.close()
        return True, round((time.monotonic() - started) * 1000, 1)
    except Exception:
        return False, None


@router.get("/ready")
def ready():
    ok, _ = _db_ping()
    if ok:
        return {"status": "ok"}
    return JSONResponse(status_code=503, content={"status": "unavailable"})


def _collector_freshness():
    """-> dict with per-fleet freshness, or {"error": ...} if the query fails."""
    try:
        conn = get_connection()
        try:
            cur = conn.cursor(dictionary=True)
            try:
                cur.execute("""
                    SELECT COUNT(*) AS active_accounts,
                           SUM(last_synced_at IS NULL) AS never_synced,
                           MIN(TIMESTAMPDIFF(SECOND, last_synced_at, NOW())) AS newest_age_s,
                           MAX(TIMESTAMPDIFF(SECOND, last_synced_at, NOW())) AS oldest_age_s,
                           SUM(TIMESTAMPDIFF(SECOND, last_synced_at, NOW()) > %s) AS stale_accounts
                    FROM aws_accounts WHERE status = 'active'
                """, (STALE_AFTER_SECONDS,))
                row = cur.fetchone() or {}
            finally:
                cur.close()
        finally:
            conn.close()
    except Exception as exc:
        return {"error": type(exc).__name__}

    def _i(v):
        return int(v) if v is not None else None

    active = _i(row.get("active_accounts")) or 0
    never = _i(row.get("never_synced")) or 0
    stale = (_i(row.get("stale_accounts")) or 0) + never
    if active == 0:
        status = "idle"
    elif stale:
        status = "stale"
    else:
        status = "ok"
    return {
        "status": status,
        "active_accounts": active,
        "stale_accounts": stale,
        "newest_sync_age_seconds": _i(row.get("newest_age_s")),
        "oldest_sync_age_seconds": _i(row.get("oldest_age_s")),
        "stale_after_seconds": STALE_AFTER_SECONDS,
    }


@router.get("/detail")
def detail(current_user: dict = Depends(require_permission("operations.view"))):
    ok, latency = _db_ping()
    collector = _collector_freshness() if ok else {"status": "unknown"}
    overall = "ok"
    if not ok:
        overall = "down"
    elif collector.get("status") in ("stale", None) or "error" in collector:
        overall = "degraded"
    return {
        "status": overall,
        "version": VERSION,
        "database": {"ok": ok, "latency_ms": latency},
        "collector": collector,
        "llm_summaries_enabled": os.getenv("LLM_SUMMARY_ENABLED", "false").lower() == "true",
    }
