# app/api/settings.py
from fastapi import APIRouter, Body, Query, Depends
from app.db import get_connection
from app.auth.permissions import require_permission
from app.threshold_defaults import DEFAULT_THRESHOLDS, FALLBACK_THRESHOLD, normalize_threshold_resource_type
import datetime, json, logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["Settings"])

# ALB/NLB resource_type normalization now lives in app/threshold_defaults.py
# (normalize_threshold_resource_type) so every place that writes
# thresholds.resource_type -- this file's two call sites AND
# app/api/metric_catalog.py's separate _sync_thresholds_for_selection(),
# which the original fix here missed entirely -- shares one definition
# instead of drifting copies. See
# apply_fix_threshold_resource_type_everywhere.py for why this moved.

def _metrics_with_data_for_account(account_id: int) -> set:
    """
    {(resource_type, metric_name_lower), ...} -- every (resource_type,
    metric_name) combination that has AT LEAST ONE row in the `metrics`
    last-value cache for a resource belonging to this account.
    metric_name is lowercased here because metric_catalog.metric_name
    stores CloudWatch-style names ("CPUUtilization") while
    app/collector/metrics/runner.py's write_metric() writes its own
    lowercase db_metric_name convention ("cpuutilization") into `metrics`
    -- comparing them as plain Python strings without normalizing case
    would incorrectly treat every AWS metric as having no data, since
    the two sides never match by construction. (SQL comparisons
    elsewhere in this app, e.g. alert_evaluator.py's JOIN, happen to work
    despite this because MySQL's default collation is case-insensitive;
    this is a plain Python set membership check, which is not.) Callers
    must also .lower() the metric_name they're checking against this set.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT r.resource_type, m.metric_name
        FROM metrics m
        JOIN resources r ON r.id = m.resource_id
        WHERE r.aws_account_id = %s
    """, (account_id,))
    pairs = {(resource_type, metric_name.lower()) for resource_type, metric_name in cur.fetchall()}
    cur.close(); conn.close()
    return pairs


def _ser(obj):
    if isinstance(obj, (datetime.datetime, datetime.date)): return obj.isoformat()
    if isinstance(obj, dict):  return {k: _ser(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [_ser(i) for i in obj]
    return obj


@router.get("/thresholds")
def get_thresholds(
    account_id: int = Query(3),
    include_no_data: bool = Query(
        False,
        description="If false (default), thresholds for metrics that have "
                    "never produced any data for this account are hidden -- "
                    "not deleted, just excluded from this response. Pass "
                    "true to see everything, e.g. for debugging why a "
                    "metric never collects.",
    ),
    current_user: dict = Depends(require_permission("alerts.view")),
):
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

    has_data_pairs = _metrics_with_data_for_account(account_id)
    no_data_count = 0
    out = []
    for r in rows:
        has_data = (r["resource_type"], (r["metric_name"] or "").lower()) in has_data_pairs
        r["has_data"] = has_data
        if not has_data:
            no_data_count += 1
            if not include_no_data:
                continue
        out.append(r)

    return {"thresholds": [_ser(r) for r in out], "hidden_no_data_count": no_data_count}


@router.post("/thresholds")
def upsert_threshold(payload: dict = Body(...), current_user: dict = Depends(require_permission("alerts.configure"))):
    account_id     = int(payload.get("account_id", 3))
    metric_id      = payload["metric_id"]
    resource_type  = normalize_threshold_resource_type(payload.get("resource_type", "ec2"))
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
            """, (account_id, normalize_threshold_resource_type(m["service"]), m["id"], warn, crit, comp))
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