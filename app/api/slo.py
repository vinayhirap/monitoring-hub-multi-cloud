# app/api/slo.py
"""
CRUD + on-demand error-budget computation for slo_definitions
(db/migrations/034_slo_error_budget.sql -- see its docstring for the
two measurement modes and why this is computed on demand rather than
materialized). Read gated on slo.view, write gated on slo.manage --
same split as app/api/synthetic.py.
"""
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Body, HTTPException, Depends
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/slo", tags=["SLO"])


def _require_account_access(account_id: int, current_user: dict) -> None:
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")


_MAX_WINDOW_DAYS = 365


def _validate_target_pct(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="target_pct must be a number")
    if not (0 < value <= 100):
        raise HTTPException(status_code=400, detail="target_pct must be between 0 and 100")
    return value


def _validate_window_days(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="window_days must be an integer")
    if not (1 <= value <= _MAX_WINDOW_DAYS):
        raise HTTPException(status_code=400, detail=f"window_days must be between 1 and {_MAX_WINDOW_DAYS}")
    return value


def _compute_status(cursor, slo: dict) -> dict:
    """
    Returns {uptime_pct, budget_consumed_pct, budget_remaining_pct,
    window_start, status} for one SLO, or a dict with status='no_data'
    if there's nothing to compute from yet (brand new SLO, or a
    synthetic check that hasn't run in this window).

    status is one of: 'ok' (< 75% of budget used), 'warning' (75-100%),
    'breached' (>= 100% -- the SLO's promise has been broken this
    window). Thresholds match common SRE practice (75% is the
    conventional "start paying attention" line -- same idea as this
    app's own alert warning/critical split elsewhere).
    """
    window_minutes = slo["window_days"] * 24 * 60
    window_start = datetime.utcnow() - timedelta(days=slo["window_days"])
    allowed_bad_minutes = window_minutes * (1 - float(slo["target_pct"]) / 100)

    if slo["synthetic_check_id"]:
        cursor.execute("""
            SELECT COUNT(*) AS total, SUM(r.success) AS successful,
                   MAX(c.interval_seconds) AS interval_seconds
            FROM synthetic_check_results r
            JOIN synthetic_checks c ON c.id = r.check_id AND c.aws_account_id = %s
            WHERE r.check_id = %s AND r.checked_at >= %s
        """, (slo["aws_account_id"], slo["synthetic_check_id"], window_start))
        row = cursor.fetchone()
        total = row["total"] or 0
        if total == 0:
            return {"status": "no_data", "uptime_pct": None, "budget_consumed_pct": None,
                    "budget_remaining_pct": None, "window_start": str(window_start)}
        successful = row["successful"] or 0
        uptime_pct = round(100 * successful / total, 3)
        # F23: each failed probe stands for one check interval of
        # downtime. The old formula extrapolated the failure RATIO over
        # the whole window, so a check created 1 day ago with 1% failures
        # was charged as if it had failed 1% of all 30 days (10x budget on
        # a 99.9% SLO -> 'breached' when only ~33% was really consumed).
        interval_minutes = float(row.get("interval_seconds") or 300) / 60
        failed = max(0, int(total) - int(successful))
        actual_bad_minutes = min(window_minutes, failed * interval_minutes)
    else:
        metric_clause = "AND a.metric_name = %s" if slo["metric_name"] else ""
        params = [window_start, slo["resource_id"], slo["aws_account_id"]]
        if slo["metric_name"]:
            params.append(slo["metric_name"])
        params.append(window_start)
        cursor.execute(f"""
            SELECT SUM(
                TIMESTAMPDIFF(
                    SECOND,
                    GREATEST(a.triggered_at, %s),
                    LEAST(COALESCE(a.resolved_at, NOW()), NOW())
                )
            ) AS bad_seconds
            FROM alerts a
            WHERE a.resource_id = %s
              AND a.aws_account_id = %s
              AND UPPER(a.severity) = 'CRITICAL'
              -- downtime that was NOT real must not burn error budget:
              -- planned maintenance (silenced), and alerts the system closed
              -- because they were never genuine (placeholder/duplicate/...)
              AND a.silenced = 0
              AND COALESCE(a.resolution_reason, '') NOT IN
                  ('duplicate', 'placeholder_threshold', 'threshold_disabled', 'bulk_clear')
              {metric_clause}
              AND COALESCE(a.resolved_at, NOW()) >= %s
              AND a.triggered_at <= NOW()
        """, tuple(params))
        row = cursor.fetchone()
        bad_seconds = row["bad_seconds"] or 0
        # NOTE: overlapping CRITICAL alerts on the same resource/metric
        # window would double-count here -- an accepted approximation,
        # same "probable, not exact" honesty as every other derived
        # metric in this app (see rca.py's module docstring). Rare in
        # practice since alert_evaluator.py only keeps one active
        # alert per (resource, metric) at a time (group_key dedup).
        actual_bad_minutes = bad_seconds / 60
        uptime_pct = round(100 * (1 - actual_bad_minutes / window_minutes), 3) if window_minutes else None

    if allowed_bad_minutes > 0:
        budget_consumed_pct = round(100 * actual_bad_minutes / allowed_bad_minutes, 1)
    else:
        # target_pct = 100: zero budget. Any bad minute is a breach;
        # this previously fell through to status 'no_data' even while
        # the service was down.
        budget_consumed_pct = 0.0 if actual_bad_minutes <= 0 else 100.0
    budget_remaining_pct = round(100 - budget_consumed_pct, 1)

    if budget_consumed_pct >= 100:
        status = "breached"
    elif budget_consumed_pct >= 75:
        status = "warning"
    else:
        status = "ok"

    return {
        "status": status,
        "uptime_pct": uptime_pct,
        "budget_consumed_pct": budget_consumed_pct,
        "budget_remaining_pct": budget_remaining_pct,
        "window_start": str(window_start),
    }


@router.get("")
def list_slos(current_user: dict = Depends(require_permission("slo.view"))):
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT s.*, acc.account_name,
                   sc.name AS synthetic_check_name,
                   (SELECT r.name FROM resources r
                    WHERE r.resource_id = s.resource_id
                      AND r.aws_account_id = s.aws_account_id
                    LIMIT 1) AS resource_name
            FROM slo_definitions s
            JOIN aws_accounts acc ON acc.id = s.aws_account_id
            LEFT JOIN synthetic_checks sc ON sc.id = s.synthetic_check_id
                                           AND sc.aws_account_id = s.aws_account_id
            ORDER BY s.name
        """)
        rows = cursor.fetchall()
        if accessible is not None:
            rows = [r for r in rows if r["aws_account_id"] in accessible]
        for row in rows:
            row.update(_compute_status(cursor, row))
        return rows
    finally:
        cursor.close(); conn.close()


@router.post("")
def create_slo(payload: dict = Body(...), current_user: dict = Depends(require_permission("slo.manage"))):
    account_id = payload.get("aws_account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="aws_account_id is required")
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="aws_account_id must be an integer")
    _require_account_access(account_id, current_user)

    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    synthetic_check_id = payload.get("synthetic_check_id")
    resource_id = payload.get("resource_id")
    if bool(synthetic_check_id) == bool(resource_id):
        raise HTTPException(
            status_code=400,
            detail="Exactly one of synthetic_check_id or resource_id must be set (not both, not neither)",
        )

    target_pct = _validate_target_pct(payload.get("target_pct", 99.9))
    window_days = _validate_window_days(payload.get("window_days", 30))

    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        # Tenant isolation: the referenced check/resource must belong
        # to the SLO's own account, otherwise a user scoped to account A
        # could read account B's uptime/alert history through the SLO.
        if synthetic_check_id:
            cursor.execute("SELECT aws_account_id FROM synthetic_checks WHERE id = %s", (synthetic_check_id,))
            ref = cursor.fetchone()
        else:
            cursor.execute(
                "SELECT aws_account_id FROM resources WHERE resource_id = %s AND aws_account_id = %s LIMIT 1",
                (resource_id, account_id),
            )
            ref = cursor.fetchone()
        if not ref or ref["aws_account_id"] != account_id:
            raise HTTPException(status_code=400, detail="Referenced synthetic check / resource not found in this account")

        cursor.execute("""
            INSERT INTO slo_definitions
                (aws_account_id, name, synthetic_check_id, resource_id, metric_name,
                 target_pct, window_days, enabled, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            account_id, name, synthetic_check_id, resource_id,
            payload.get("metric_name"), target_pct,
            window_days, bool(payload.get("enabled", True)),
            int(current_user["id"]),
        ))
        conn.commit()
        return {"status": "created", "id": cursor.lastrowid}
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        logger.exception("create SLO failed")
        raise HTTPException(status_code=400, detail="Could not create SLO")
    finally:
        cursor.close(); conn.close()


@router.patch("/{slo_id}")
def update_slo(slo_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("slo.manage"))):
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT aws_account_id FROM slo_definitions WHERE id = %s", (slo_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="SLO not found")
        _require_account_access(row["aws_account_id"], current_user)

        editable = ("name", "target_pct", "window_days", "enabled")
        updates = {k: v for k, v in payload.items() if k in editable}
        if not updates:
            raise HTTPException(status_code=400, detail="No editable fields provided")
        if "target_pct" in updates:
            updates["target_pct"] = _validate_target_pct(updates["target_pct"])
        if "window_days" in updates:
            updates["window_days"] = _validate_window_days(updates["window_days"])
        if "name" in updates:
            name = updates["name"].strip() if isinstance(updates["name"], str) else ""
            if not name or len(name) > 255:
                raise HTTPException(status_code=400, detail="name is required (max 255 characters)")
            updates["name"] = name
        if "enabled" in updates:
            updates["enabled"] = bool(updates["enabled"])

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        cursor.execute(f"UPDATE slo_definitions SET {set_clause} WHERE id = %s", (*updates.values(), slo_id))
        conn.commit()
        return {"status": "updated"}
    finally:
        cursor.close(); conn.close()


@router.delete("/{slo_id}")
def delete_slo(slo_id: int, current_user: dict = Depends(require_permission("slo.manage"))):
    conn = get_connection(); cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT aws_account_id FROM slo_definitions WHERE id = %s", (slo_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="SLO not found")
        _require_account_access(row["aws_account_id"], current_user)

        cursor.execute("DELETE FROM slo_definitions WHERE id = %s", (slo_id,))
        conn.commit()
        return {"status": "deleted"}
    finally:
        cursor.close(); conn.close()
