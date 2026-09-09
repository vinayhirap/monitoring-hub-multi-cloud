# app/api/settings.py
from fastapi import APIRouter, Body, Query, Depends
from app.db import get_connection
from app.auth.permissions import require_permission
from app.threshold_defaults import DEFAULT_THRESHOLDS, FALLBACK_THRESHOLD
import datetime, json, logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["Settings"])

# metric_catalog.service ("alb", "nlb") is the correct catalog/display
# value and is NOT changed by this map -- but
# app/collector/discovery/runner.py stores ALL Elastic Load Balancing v2
# resources (both ALB and NLB; this codebase doesn't distinguish them at
# discovery time) under resources.resource_type = "elb" uniformly.
# alert_evaluator.py's core scheduled evaluation JOINs
# thresholds.resource_type directly against resources.resource_type with
# no service-name fallback (unlike check_and_write_alerts() /
# app/aws/collector_direct.py, which already has its own separate
# LOCAL_RESOURCE_TYPE map for this same translation -- see Phase 5,
# apply_check_thresholds_local_metrics.py). Without this normalization,
# ALB/NLB thresholds are silently unevaluable by the scheduled evaluator
# forever, no matter what value they're set to. See
# apply_fix_alb_nlb_threshold_resource_type.py for the full story.
_THRESHOLD_RESOURCE_TYPE_ALIASES = {"alb": "elb", "nlb": "elb"}


def _normalize_threshold_resource_type(value):
    return _THRESHOLD_RESOURCE_TYPE_ALIASES.get(value, value)


def _ser(obj):
    if isinstance(obj, (datetime.datetime, datetime.date)): return obj.isoformat()
    if isinstance(obj, dict):  return {k: _ser(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [_ser(i) for i in obj]
    return obj


@router.get("/thresholds")
def get_thresholds(account_id: int = Query(3), current_user: dict = Depends(require_permission("alerts.view"))):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT
            t.id, t.aws_account_id, t.resource_type, t.metric_id,
            t.warning_value, t.critical_value, t.comparison,
            t.evaluation_period, t.enabled, t.created_at,
            mc.metric_name, mc.service, mc.namespace, mc.statistic, mc.unit
        FROM thresholds t
        LEFT JOIN metric_catalog mc ON t.metric_id = mc.id
        WHERE t.aws_account_id = %s
        ORDER BY mc.service, mc.metric_name
    """, (account_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [_ser(r) for r in rows]


@router.post("/thresholds")
def upsert_threshold(payload: dict = Body(...), current_user: dict = Depends(require_permission("alerts.configure"))):
    account_id     = int(payload.get("account_id", 3))
    metric_id      = payload["metric_id"]
    resource_type  = _normalize_threshold_resource_type(payload.get("resource_type", "ec2"))
    warning_value  = float(payload["warning_value"])
    critical_value = float(payload["critical_value"])
    comparison     = payload.get("comparison", ">")
    eval_period    = int(payload.get("evaluation_period", 5))
    enabled        = int(payload.get("enabled", 1))

    conn = get_connection(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO thresholds
          (aws_account_id, resource_type, metric_id, warning_value,
           critical_value, comparison, evaluation_period, enabled)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          resource_type     = VALUES(resource_type),
          warning_value     = VALUES(warning_value),
          critical_value    = VALUES(critical_value),
          comparison        = VALUES(comparison),
          evaluation_period = VALUES(evaluation_period),
          enabled           = VALUES(enabled)
    """, (account_id, resource_type, metric_id, warning_value,
          critical_value, comparison, eval_period, enabled))
    conn.commit(); new_id = cur.lastrowid; cur.close(); conn.close()

    _write_audit("admin", "Threshold updated",
                 f"account={account_id} metric_id={metric_id} warn={warning_value} crit={critical_value}")
    return {"status": "saved", "id": new_id}


@router.patch("/thresholds/{threshold_id}/toggle")
def toggle_threshold(threshold_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("alerts.configure"))):
    enabled = int(payload.get("enabled", 1))
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE thresholds SET enabled=%s WHERE id=%s", (enabled, threshold_id))
    conn.commit(); cur.close(); conn.close()
    return {"status": "updated", "enabled": enabled}


@router.post("/thresholds/seed")
def seed_default_thresholds(account_id: int = Query(3), current_user: dict = Depends(require_permission("alerts.configure"))):
    # Only seed thresholds for metrics actually enabled in "Metrics to
    # Monitor" for this account (account_metric_selections). Previously this
    # pulled from the entire metric_catalog regardless of selection, so the
    # Metric Thresholds section could show/create rows for metrics that
    # weren't even being collected for this account.
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT mc.* FROM metric_catalog mc
        JOIN account_metric_selections ams ON ams.metric_id = mc.id
        WHERE ams.aws_account_id = %s AND ams.enabled = 1 AND mc.metric_name != ''
    """, (account_id,))
    metrics = cur.fetchall()
    inserted = 0
    for m in metrics:
        warn, crit, comp = DEFAULT_THRESHOLDS.get(m["metric_name"], FALLBACK_THRESHOLD)
        try:
            cur.execute("""
                INSERT IGNORE INTO thresholds
                  (aws_account_id, resource_type, metric_id,
                   warning_value, critical_value, comparison, evaluation_period, enabled)
                VALUES (%s,%s,%s,%s,%s,%s,5,1)
            """, (account_id, _normalize_threshold_resource_type(m["service"]), m["id"], warn, crit, comp))
            inserted += cur.rowcount
        except Exception as e:
            logger.warning(f"Seed skip {m['metric_name']}: {e}")
    conn.commit(); cur.close(); conn.close()
    return {"status": "seeded", "inserted": inserted}


@router.get("/check")
def check_thresholds(account_id: int = Query(3), current_user: dict = Depends(require_permission("alerts.view"))):
    from app.aws.collector_direct import check_and_write_alerts

    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT t.*, mc.metric_name, mc.namespace, mc.statistic, mc.service
        FROM thresholds t
        JOIN metric_catalog mc ON t.metric_id = mc.id
        WHERE t.aws_account_id = %s AND t.enabled = 1
    """, (account_id,))
    thresholds = cur.fetchall()
    cur.execute("SELECT default_region FROM aws_accounts WHERE id=%s", (account_id,))
    acc = cur.fetchone(); cur.close(); conn.close()
    region = (acc or {}).get("default_region", "")

    try:
        breaches = check_and_write_alerts(account_id, region, [_ser(t) for t in thresholds])
        return {"breaches": breaches, "checked": len(thresholds), "region": region, "written_to_db": len(breaches)}
    except Exception as e:
        logger.error(f"Check error: {e}")
        return {"breaches": [], "error": str(e)}


def _write_audit(actor, action, detail):
    try:
        conn = get_connection(); cur = conn.cursor()
        cur.execute(
            "INSERT INTO audit_logs (actor, action, payload) VALUES (%s,%s,%s)",
            (actor, action, json.dumps({"detail": detail, "role": "ADMIN"}))
        )
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.warning(f"Audit: {e}")