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
from app.collector.synthetic import (
    validate_target, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/synthetic-checks", tags=["Synthetic Monitoring"])

_VALID_CHECK_TYPES = ("http", "tcp", "dns")
_MAX_INTERVAL_SECONDS = 86400


def _int_field(payload: dict, key: str, default, lo: int, hi: int):
    """Parses an optional int field with bounds -> HTTP 400 on bad input
    (previously int() raised straight through as a 500)."""
    raw = payload.get(key, default)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{key} must be an integer")
    if not (lo <= value <= hi):
        raise HTTPException(status_code=400, detail=f"{key} must be between {lo} and {hi}")
    return value


def _validate_common(payload: dict) -> dict:
    """Validates/normalises every bounded field present in payload."""
    out = {}
    if "timeout_seconds" in payload:
        out["timeout_seconds"] = _int_field(payload, "timeout_seconds", 10, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
    if "interval_seconds" in payload:
        # Below 60 the 2-min critical-tier scheduler cadence can't
        # honor the interval anyway -- reject rather than silently
        # running slower than configured.
        out["interval_seconds"] = _int_field(payload, "interval_seconds", 300, 60, _MAX_INTERVAL_SECONDS)
    if "consecutive_failure_threshold" in payload:
        out["consecutive_failure_threshold"] = _int_field(payload, "consecutive_failure_threshold", 2, 1, 100)
    if "expected_status_code" in payload:
        out["expected_status_code"] = _int_field(payload, "expected_status_code", None, 100, 599)
    if "expected_keyword" in payload:
        kw = payload.get("expected_keyword")
        if kw is not None and (not isinstance(kw, str) or len(kw) > 255):
            raise HTTPException(status_code=400, detail="expected_keyword must be a string of at most 255 characters")
        out["expected_keyword"] = kw or None
    if "environment" in payload:
        env = payload.get("environment")
        if not isinstance(env, str) or not env.strip() or len(env) > 50:
            raise HTTPException(status_code=400, detail="environment must be a non-empty string of at most 50 characters")
        out["environment"] = env.strip()
    if "name" in payload:
        name = (payload.get("name") or "").strip() if isinstance(payload.get("name"), str) else ""
        if not name or len(name) > 255:
            raise HTTPException(status_code=400, detail="name is required (max 255 characters)")
        out["name"] = name
    if "enabled" in payload:
        out["enabled"] = bool(payload.get("enabled"))
    return out


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


def _get_check_type(check_id: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT check_type FROM synthetic_checks WHERE id = %s", (check_id,))
        row = cur.fetchone()
        return row["check_type"] if row else None
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
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="aws_account_id must be an integer")
    _require_account_access(account_id, current_user)

    check_type = payload.get("check_type", "http")
    if check_type not in _VALID_CHECK_TYPES:
        raise HTTPException(status_code=400, detail=f"check_type must be one of {_VALID_CHECK_TYPES}")

    fields = _validate_common({
        "name": payload.get("name"),
        "timeout_seconds": payload.get("timeout_seconds", 10),
        "interval_seconds": payload.get("interval_seconds", 300),
        "consecutive_failure_threshold": payload.get("consecutive_failure_threshold", 2),
        "expected_status_code": payload.get("expected_status_code"),
        "expected_keyword": payload.get("expected_keyword"),
        "environment": payload.get("environment", "prod"),
        "enabled": payload.get("enabled", True),
    })

    target = (payload.get("target") or "").strip() if isinstance(payload.get("target"), str) else ""
    try:
        validate_target(check_type, target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO synthetic_checks
                (aws_account_id, name, check_type, target, expected_status_code,
                 expected_keyword, timeout_seconds, interval_seconds,
                 consecutive_failure_threshold, environment, enabled, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            int(account_id), fields["name"], check_type, target,
            fields["expected_status_code"], fields["expected_keyword"],
            fields["timeout_seconds"], fields["interval_seconds"],
            fields["consecutive_failure_threshold"],
            fields["environment"], fields["enabled"],
            int(current_user["id"]),
        ))
        conn.commit()
        return {"status": "created", "id": cur.lastrowid}
    except Exception:
        conn.rollback()
        logger.exception("create synthetic check failed")
        raise HTTPException(status_code=400, detail="Could not create synthetic check")
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
    raw = {k: v for k, v in payload.items() if k in editable_fields}
    if not raw:
        raise HTTPException(status_code=400, detail="No editable fields provided")
    updates = _validate_common(raw)
    if "target" in raw:
        # SSRF: a PATCH must pass the same target validation as create.
        check_type = _get_check_type(check_id)
        target = raw["target"].strip() if isinstance(raw["target"], str) else ""
        try:
            validate_target(check_type, target)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        updates["target"] = target

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
            UPDATE alerts SET status = 'resolved', resolved_at = UTC_TIMESTAMP(), last_seen_at = UTC_TIMESTAMP(),
                              resolution_reason = 'check_deleted', resolved_by = %s
            WHERE aws_account_id = %s AND resource_id = %s AND metric_name = 'synthetic_uptime'
              AND status IN ('active', 'acknowledged')
        """, (current_user["username"], account_id, f"synthetic-{check_id}"))
        cur.execute("DELETE FROM synthetic_checks WHERE id = %s", (check_id,))
        conn.commit()
        return {"status": "deleted"}
    finally:
        cur.close(); conn.close()
