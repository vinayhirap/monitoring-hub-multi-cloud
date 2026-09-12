# app/api/settings.py
from fastapi import APIRouter, Body, Query, Depends, HTTPException
from app.db import get_connection
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids
from app.threshold_defaults import DEFAULT_THRESHOLDS, FALLBACK_THRESHOLD, normalize_threshold_resource_type, resolve_db_metric_name
import datetime, json, logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["Settings"])

# How old a `metrics` row can be before _metrics_with_data_for_account()
# stops counting it as "this metric has data" -- deliberately generous.
#
# CORRECTED (apply_fix_stale_cutoff_too_aggressive.py): originally set to
# 60 minutes, which caused a real regression -- confirmed live: ALB's
# HTTPCode_Target_5XX_Count disappeared from Metric Thresholds within
# hours of shipping, despite being correctly, actively collected. Root
# cause: this is a Sum-type, EVENT-DRIVEN CloudWatch metric -- AWS only
# publishes a datapoint for it when a 5xx error actually happens.  Zero
# 5xx errors for an hour is a GOOD sign (a healthy load balancer), not
# evidence the collector stopped, but a 60-minute cutoff couldn't tell
# the difference between "genuinely abandoned metric" (the BurstBalance
# case this was built for) and "actively collected, currently just has
# nothing to report" (this case). 60 minutes is far too short a window
# for any event/error-count metric on a quiet-but-healthy resource.
#
# Widened to match metric_history's OWN existing retention window (7
# days, see prune_metric_history() in app/collector/metrics_writer.py)
# instead of picking a new arbitrary number -- this app already treats
# 7 days as "how long data stays relevant" elsewhere, so reusing it here
# is a principled choice, not a guess. A metric permanently dropped from
# collection (like BurstBalance) will reliably exceed even a 7-day
# window eventually, since nothing will EVER refresh it again -- while
# an event metric would need to go a full week with zero occurrences to
# be wrongly hidden, a much rarer, more defensible edge case than an
# hour.
_STALE_DATA_CUTOFF_MINUTES = 7 * 24 * 60  # 10080 -- 7 days

# ALB/NLB resource_type normalization now lives in app/threshold_defaults.py
# (normalize_threshold_resource_type) so every place that writes
# thresholds.resource_type -- this file's two call sites AND
# app/api/metric_catalog.py's separate _sync_thresholds_for_selection(),
# which the original fix here missed entirely -- shares one definition
# instead of drifting copies. See
# apply_fix_threshold_resource_type_everywhere.py for why this moved.

# ALB/NLB share resources.resource_type='elb' (see
# app/collector/discovery/runner.py -- this app doesn't distinguish load
# balancer type at discovery time) and commonly share metric_catalog
# metric names (e.g. HealthyHostCount). The only way to tell them apart
# is the target-group/load-balancer ARN pattern. Same constants
# app/api/metric_catalog.py's present_core_services logic already uses
# for the Metrics to Monitor page -- reused here, not reinvented. See
# apply_fix_nlb_ghost_thresholds.py.
_ALB_ARN_PATTERN = "loadbalancer/app/"
_NLB_ARN_PATTERN = "loadbalancer/net/"


def _require_account_access(account_id: int, current_user: dict) -> None:
    """
    SECURITY: every endpoint in this file takes account_id as a plain
    query/body param and, until this fix, never checked it against the
    caller's RBAC scope at all -- only the role-level permission
    (alerts.view / alerts.configure) was enforced. That meant a viewer
    could read another account's alert-threshold configuration, and an
    EDITOR could silently create/modify/disable alerting thresholds
    (or trigger a live threshold check/alert write) for any account_id
    in the system, not just the accounts their access_scopes/
    group_policies actually grant them -- a materially worse gap than
    a read-only IDOR, since it lets a scoped-down editor blind
    monitoring for infrastructure outside their assignment. Mirrors
    the account_id-not-in-accessible pattern already used in
    app/api/live_data.py, app/api/admin/accounts.py, and
    app/api/alerts.py.
    """
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None and account_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")


def _get_threshold_account_id(threshold_id: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT aws_account_id FROM thresholds WHERE id = %s", (threshold_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row["aws_account_id"] if row else None


def _metrics_with_data_for_account(account_id: int) -> set:
    """
    {(resource_type, metric_name_lower, resource_id), ...} -- every
    (resource_type, metric_name, resource ARN) combination that has at
    least one RECENT row in the `metrics` last-value cache for a
    resource belonging to this account. metric_name is lowercased here
    because metric_catalog.metric_name stores CloudWatch-style names
    ("CPUUtilization") while app/collector/metrics/runner.py's
    write_metric() writes its own lowercase db_metric_name convention
    ("cpuutilization") into `metrics` -- comparing them as plain Python
    strings without normalizing case would incorrectly treat every AWS
    metric as having no data, since the two sides never match by
    construction. (SQL comparisons elsewhere in this app, e.g.
    alert_evaluator.py's JOIN, happen to work despite this because
    MySQL's default collation is case-insensitive; this is a plain
    Python set membership check, which is not.) Callers must also
    .lower() the metric_name they're checking against this set.

    resource_id (the resource's ARN, not its DB primary key) is
    included so callers can further disambiguate cases where
    resource_type alone is too coarse -- e.g. ALB vs NLB, which both
    normalize to 'elb'. See _has_data_for_threshold_row() below.

    RECENT, not just present: `metrics` is a last-value cache with NO
    equivalent of metric_history's prune_metric_history() -- a row
    written once, ever, sits there forever even after whatever collected
    it stops running entirely. Confirmed live: EBS BurstBalance (dropped
    from collection entirely by Phase 1, see
    apply_dashboard_charts_metric_history.py) still had a row from ~20
    hours before this fix, permanently making has_data report a false
    positive with no way for it to ever self-correct. _STALE_DATA_CUTOFF
    below is deliberately generous (well beyond the slowest normal
    collection tier, 15 minutes) so a brief scheduler restart or hiccup
    never falsely hides a metric that's still genuinely being collected
    -- it's tuned to catch abandoned metrics measured in hours/days, not
    to be a tight liveness check.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT r.resource_type, m.metric_name, r.resource_id
        FROM metrics m
        JOIN resources r ON r.id = m.resource_id
        WHERE r.aws_account_id = %s
          AND m.metric_timestamp >= DATE_SUB(NOW(), INTERVAL %s MINUTE)
    """, (account_id, _STALE_DATA_CUTOFF_MINUTES))
    triples = {
        (resource_type, metric_name.lower(), resource_id or "")
        for resource_type, metric_name, resource_id in cur.fetchall()
    }
    cur.close(); conn.close()
    return triples


def _has_data_for_threshold_row(service, resource_type, db_metric_name, has_data_triples):
    """
    True if this threshold row's (service, resource_type, metric_name)
    genuinely has recent data for THIS row's own service -- not a
    same-resource_type, same-metric-name sibling service's data.

    For alb/nlb specifically, resource_type alone ('elb' for both) and
    metric_name alone (frequently shared, e.g. HealthyHostCount) are
    both too coarse: an account with real ALBs and zero NLBs would
    otherwise make the NLB row look populated purely because an ALB's
    data happens to match on both fields. Disambiguated with the same
    ARN pattern check apply_metrics_to_monitor_cleanup.py already uses
    for Metrics to Monitor (loadbalancer/app/ vs loadbalancer/net/).

    Every other service keeps the original, simpler
    (resource_type, metric_name) membership check -- unaffected.
    """
    if service in ("alb", "nlb"):
        pattern = _ALB_ARN_PATTERN if service == "alb" else _NLB_ARN_PATTERN
        return any(
            rt == resource_type and mn == db_metric_name and pattern in rid
            for rt, mn, rid in has_data_triples
        )
    return any(
        rt == resource_type and mn == db_metric_name
        for rt, mn, _rid in has_data_triples
    )


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
    _require_account_access(account_id, current_user)
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

    has_data_triples = _metrics_with_data_for_account(account_id)
    no_data_count = 0
    out = []
    for r in rows:
        db_metric_name = resolve_db_metric_name(r["resource_type"], r["metric_name"])
        has_data = _has_data_for_threshold_row(r["service"], r["resource_type"], db_metric_name, has_data_triples)
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
    _require_account_access(account_id, current_user)
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

    _write_audit(current_user["username"], "Threshold updated",
                 f"account={account_id} metric_id={metric_id} warn={warning_value} crit={critical_value}",
                 role=current_user["role"].upper())
    return {"status": "saved", "id": new_id}


@router.patch("/thresholds/{threshold_id}/toggle")
def toggle_threshold(threshold_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("alerts.configure"))):
    account_id = _get_threshold_account_id(threshold_id)
    if account_id is None:
        raise HTTPException(status_code=404, detail="Threshold not found")
    _require_account_access(account_id, current_user)

    enabled = int(payload.get("enabled", 1))
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE thresholds SET enabled=%s WHERE id=%s", (enabled, threshold_id))
    conn.commit(); cur.close(); conn.close()
    return {"status": "updated", "enabled": enabled}


@router.post("/thresholds/seed")
def seed_default_thresholds(account_id: int = Query(3), current_user: dict = Depends(require_permission("alerts.configure"))):
    _require_account_access(account_id, current_user)
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
    _require_account_access(account_id, current_user)
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


def _write_audit(actor, action, detail, role="ADMIN"):
    try:
        conn = get_connection(); cur = conn.cursor()
        cur.execute(
            "INSERT INTO audit_logs (actor, action, payload) VALUES (%s,%s,%s)",
            (actor, action, json.dumps({"detail": detail, "role": role}))
        )
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        logger.warning(f"Audit: {e}")