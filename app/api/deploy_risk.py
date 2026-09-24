# app/api/deploy_risk.py
"""
Deploy-risk correlation, READ half (2026-09-14). Ingestion side is
app/api/webhooks.py's POST /api/webhooks/deploy; per-alert narrative
correlation is app/collector/rca.py's _gather_deployment_signal(). This
is the fleet-wide VIEW: "of our recent deploys, which ones were
actually followed by trouble" -- the same question
app/collector/threshold_tuning.py's docstring calls out as the thing a
real production diagnosis needs to answer quickly.
"""
import logging
from fastapi import APIRouter, Depends, Query
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids
from app.collector.rca import DEPLOY_LOOKBACK_MINUTES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/deploy-risk", tags=["Deploy Risk"])

# One follow-up query runs per deployment (N+1); bound N so a chatty CI
# pipeline over a 90-day window can't turn one GET into thousands of
# queries on a pooled connection.
MAX_DEPLOYMENTS = 500


@router.get("")
def list_deploy_risk(
    days: int = Query(7, ge=1, le=90),
    current_user: dict = Depends(require_permission("deploy_risk.view")),
):
    """
    Every 'deployment' op_event in the last `days` days, with a count
    of alerts that started within DEPLOY_LOOKBACK_MINUTES afterward on
    the same account (excluding multivariate_anomaly, same exclusion
    app/api/alerts.py's end-user list uses) -- classified:
      clean  -- 0 alerts followed
      watch  -- 1 non-critical alert followed
      risky  -- 2+ alerts, or any CRITICAL alert, followed
    This is a correlation, not a proof of causation -- same "probable,
    not confirmed" framing as every other RCA signal in this app (see
    rca.py's module docstring).
    """
    accessible = get_accessible_account_ids(current_user)
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        where = ["e.event_type = 'deployment'", "e.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"]
        params = [days]
        if accessible is not None:
            if not accessible:
                return []
            placeholders = ", ".join(["%s"] * len(accessible))
            where.append(f"e.aws_account_id IN ({placeholders})")
            params.extend(accessible)

        cursor.execute(f"""
            SELECT e.id, e.aws_account_id, acc.account_name, e.resource_id,
                   e.message, e.detail, e.created_at
            FROM op_events e
            JOIN aws_accounts acc ON acc.id = e.aws_account_id AND acc.status = 'active'
            WHERE {' AND '.join(where)}
            ORDER BY e.created_at DESC
            LIMIT %s
        """, (*params, MAX_DEPLOYMENTS))
        deployments = cursor.fetchall()

        results = []
        for d in deployments:
            cursor.execute("""
                SELECT a.id, a.severity, a.metric_name, a.resource_id
                FROM alerts a
                WHERE a.aws_account_id = %s
                  AND a.metric_name != 'multivariate_anomaly'
                  -- same "not real trouble" exclusions as the SLO budget
                  -- (app/api/slo.py): maintenance-silenced alerts and ones
                  -- the system closed as never genuine.
                  AND a.silenced = 0
                  AND COALESCE(a.resolution_reason, '') NOT IN
                      ('duplicate', 'placeholder_threshold', 'threshold_disabled', 'bulk_clear')
                  AND a.triggered_at BETWEEN %s AND DATE_ADD(%s, INTERVAL %s MINUTE)
                ORDER BY a.triggered_at ASC
            """, (d["aws_account_id"], d["created_at"], d["created_at"], DEPLOY_LOOKBACK_MINUTES))
            followed_by = cursor.fetchall()

            has_critical = any(str(a["severity"] or "").upper() == "CRITICAL" for a in followed_by)
            if not followed_by:
                risk = "clean"
            elif has_critical or len(followed_by) >= 2:
                risk = "risky"
            else:
                risk = "watch"

            results.append({
                **d,
                "risk": risk,
                "alert_count": len(followed_by),
                "alerts": followed_by,
            })

        return results
    finally:
        cursor.close()
        conn.close()
