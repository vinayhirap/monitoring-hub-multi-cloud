# app/api/live_data.py
from fastapi import APIRouter, HTTPException, Query
from app.aws.collector_direct import (
    collect_ec2_instances,
    collect_ebs_volumes,
    collect_rds_instances,
    collect_lambda_functions,
    collect_s3_buckets,
    collect_elb,
    collect_ecs_clusters,
    get_account_summary,
    get_ec2_metric_series,
    get_s3_metric_series,
    _get_ebs_metric_series,
    _get_lambda_metric_series,
    _get_rds_metric_series,
    _get_elb_metric_series,
    _get_ecs_metric_series,
)
from app.db import get_connection
import datetime
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/live", tags=["Live Data"])

# Cache: 30s for near-real-time updates
_accounts_cache: dict = {"data": None, "ts": 0}
CACHE_TTL = 60   # seconds — near-real-time


def _serialize(obj):
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    return obj


def _get_db_accounts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
            SELECT id, account_name, account_id,
                default_region, status,
                created_at, last_synced_at
            FROM aws_accounts
            WHERE status = 'active'
            ORDER BY created_at DESC
        """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def _get_db_account(account_db_id: int) -> dict:
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM aws_accounts WHERE id = %s", (account_db_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Account not found")
    return row


def _get_active_alert_resources() -> set:
    try:
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT resource_id, severity
            FROM alerts
            WHERE status = 'active'
              AND (resolved_at IS NULL)
              AND triggered_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        critical = {r["resource_id"] for r in rows if (r["severity"] or "").upper() == "CRITICAL"}
        warning  = {r["resource_id"] for r in rows if (r["severity"] or "").upper() == "WARNING"}
        return critical, warning
    except Exception as e:
        logger.error(f"Active alert fetch error: {e}")
        return set(), set()


@router.get("/accounts")
def live_accounts():
    global _accounts_cache

    now = time.time()
    if _accounts_cache["data"] is not None and now - _accounts_cache["ts"] < CACHE_TTL:
        return _accounts_cache["data"]

    accounts = _get_db_accounts()

    critical_resources, warning_resources = _get_active_alert_resources()

    def process_account(acc):
        region  = acc.get("default_region")
        summary = get_account_summary(region)
        running = summary.get("ec2_running", 0)
        total   = summary.get("ec2_total",   0)
        avg_cpu = summary.get("ec2_avg_cpu", 0)

        ec2_list     = summary.get("instances", [])
        instance_ids = {i["instance_id"] for i in ec2_list}

        has_critical = bool(critical_resources & instance_ids)
        has_warning  = bool(warning_resources  & instance_ids)

        if has_critical:
            health = "critical"
        elif has_warning:
            health = "warning"
        elif avg_cpu > 80:
            health = "critical"
        elif avg_cpu > 60:
            health = "warning"
        else:
            health = "healthy"

        unhealthy_ids   = (critical_resources | warning_resources) & instance_ids
        healthy_count   = running - len(unhealthy_ids)
        unhealthy_count = len(unhealthy_ids)

        services = []
        if summary.get("ec2_total", 0) > 0:
            services.append({
                "name":           "EC2",
                "status":         "ok",
                "instance_count": running,
                "cpu":            avg_cpu,
                "memory":         0,
            })
        if summary.get("rds_total", 0) > 0:
            services.append({
                "name":           "RDS",
                "status":         "ok",
                "instance_count": summary["rds_total"],
            })
        if summary.get("lambda_total", 0) > 0:
            services.append({
                "name":           "Lambda",
                "status":         "ok",
                "instance_count": summary["lambda_total"],
            })

        return _serialize({
            "id":               acc["id"],
            "account_name":     acc["account_name"],
            "account_id":       acc["account_id"],
            "region":           region,
            "status":           health,
            "environment":      acc.get("environment", "PROD"),
            "owner_team":       acc.get("owner_team", acc.get("team", "")),
            "ec2_total":        total,
            "ec2_running":      running,
            "ec2_stopped":      summary.get("ec2_stopped", 0),
            "ebs_total":        summary.get("ebs_total",    0),
            "rds_total":        summary.get("rds_total",    0),
            "lambda_total":     summary.get("lambda_total", 0),
            "s3_total":         summary.get("s3_total",     0),
            "elb_total":        summary.get("elb_total",    0),
            "ecs_total":        summary.get("ecs_total",    0),
            "avg_cpu":          avg_cpu,
            "alerts":           0,
            "instance_count":   total,
            "healthy_resources":   max(healthy_count, 0),
            "unhealthy_resources": unhealthy_count,
            "services":         services,
            "created_at":       acc.get("created_at"),
            "last_synced_at":   acc.get("last_synced_at"),
        })

    result = []
    with ThreadPoolExecutor(max_workers=min(len(accounts), 8)) as ex:
        futures = {ex.submit(process_account, acc): acc for acc in accounts}
        for f in as_completed(futures):
            try:
                result.append(f.result())
            except Exception as e:
                logger.error(f"Account processing error: {e}")

    status_order = {"critical": 0, "warning": 1, "healthy": 2}
    result.sort(key=lambda a: status_order.get(a.get("status", "healthy"), 9))

    _accounts_cache = {"data": result, "ts": now}
    return result


@router.get("/resource-counts/{account_db_id}")
def live_resource_counts(account_db_id: int):
    """
    Lightweight per-account resource counts for the Services page
    (ServiceList.jsx tile visibility) — deliberately scoped to ONE
    account instead of GET /api/live/accounts, which fans out to
    every active account (including ones with no real deployment,
    e.g. accounts without a working role/YACE setup) and makes the
    caller wait for the slowest one. Reuses the same 60s in-process
    cache in collector_direct.py, so this costs nothing extra beyond
    what get_account_summary() already does for the Overview page.
    """
    acc     = _get_db_account(account_db_id)
    region  = acc.get("default_region")
    summary = get_account_summary(region)
    return {
        "ec2_total":    summary.get("ec2_total",    0),
        "ebs_total":    summary.get("ebs_total",    0),
        "rds_total":    summary.get("rds_total",    0),
        "lambda_total": summary.get("lambda_total", 0),
        "s3_total":     summary.get("s3_total",     0),
        "elb_total":    summary.get("elb_total",    0),
        "ecs_total":    summary.get("ecs_total",    0),
    }


@router.get("/ec2/{account_db_id}")
def live_ec2(account_db_id: int):
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_ec2_instances(region))


@router.get("/ebs/{account_db_id}")
def live_ebs(account_db_id: int):
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_ebs_volumes(region))


@router.get("/rds/{account_db_id}")
def live_rds(account_db_id: int):
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_rds_instances(region))


@router.get("/lambda/{account_db_id}")
def live_lambda(account_db_id: int):
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_lambda_functions(region))


@router.get("/s3/{account_db_id}")
def live_s3(account_db_id: int):
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_s3_buckets(region))


@router.get("/elb/{account_db_id}")
def live_elb(account_db_id: int):
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_elb(region))


@router.get("/ecs/{account_db_id}")
def live_ecs(account_db_id: int):
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_ecs_clusters(region))


# ── Real-time per-service resource counts ─────────────────────
# Used by the Services page to decide whether a tile should be shown
# at all — dynamically, based on whether the account actually HAS any
# resources of that type right now, instead of only whether a metric
# is selected for it. Only covers the 7 services with a live collector
# (same ones the /ec2, /ebs, /rds, /lambda, /s3, /elb, /ecs endpoints
# above use) — there's no resource-level collector yet for the
# extended (metric-catalog-only) services, so those aren't included
# here; the frontend treats a missing key as "unknown" and keeps that
# tile visible rather than hiding it on a guess.
_CORE_RESOURCE_COLLECTORS = {
    "ec2":    collect_ec2_instances,
    "ebs":    collect_ebs_volumes,
    "rds":    collect_rds_instances,
    "lambda": collect_lambda_functions,
    "s3":     collect_s3_buckets,
    "elb":    collect_elb,
    "ecs":    collect_ecs_clusters,
}


@router.get("/resource-counts/{account_db_id}")
def live_resource_counts(account_db_id: int):
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region")
    counts = {}
    for svc, collector in _CORE_RESOURCE_COLLECTORS.items():
        try:
            counts[svc] = len(collector(region))
        except Exception as e:
            # Unknown, not zero — a transient AWS/permissions error
            # shouldn't hide a tile that may well have real resources.
            logger.warning(f"resource-counts: {svc} failed for account {account_db_id}: {e}")
            counts[svc] = None
    return counts


# ── CloudWatch metric series endpoints ───────────────────────

@router.get("/metrics/ec2/{instance_id}")
def live_ec2_metrics(
    instance_id: str,
    region: str = Query(None),
    hours: int  = Query(6),
):
    return get_ec2_metric_series(instance_id, region, hours)


@router.get("/metrics/ebs/{volume_id}")
def live_ebs_metrics(
    volume_id: str,
    region: str = Query(None),
    hours: int  = Query(6),
):
    return _get_ebs_metric_series(volume_id, region, hours)


@router.get("/metrics/rds/{db_id}")
def live_rds_metrics(
    db_id: str,
    region: str = Query(None),
    hours: int  = Query(6),
):
    return _get_rds_metric_series(db_id, region, hours)


@router.get("/metrics/lambda/{function_name}")
def live_lambda_metrics(
    function_name: str,
    region: str = Query(None),
    hours: int  = Query(6),
):
    return _get_lambda_metric_series(function_name, region, hours)


@router.get("/metrics/s3/{bucket_name:path}")
def live_s3_metrics(
    bucket_name: str,
    hours: int = Query(24),
):
    return get_s3_metric_series(bucket_name, hours)


@router.get("/metrics/elb/{account_db_id}")
def live_elb_metrics(
    account_db_id: int,
    lb_name: str = Query(..., description="Load balancer name"),
    region: str  = Query(None),
    hours: int   = Query(6),
):
    """
    ELB CloudWatch metrics for a specific load balancer by name.
    Frontend calls: /api/live/metrics/elb/{accountId}?lb_name=<name>&region=<r>&hours=<h>
    """
    acc = _get_db_account(account_db_id)
    resolved_region = region or acc.get("default_region") 
    return _get_elb_metric_series(lb_name, resolved_region, hours)


@router.get("/metrics/ecs/{account_db_id}")
def live_ecs_metrics(
    account_db_id: int,
    cluster_name: str  = Query(..., description="ECS cluster name"),
    service_name: str  = Query(None, description="ECS service name (optional — omit for cluster-level)"),
    region: str        = Query(None),
    hours: int         = Query(6),
):
    """
    ECS CloudWatch metrics for a cluster or specific service.
    Frontend calls: /api/live/metrics/ecs/{accountId}?cluster_name=<c>&service_name=<s>&region=<r>&hours=<h>
    """
    acc = _get_db_account(account_db_id)
    resolved_region = region or acc.get("default_region")
    return _get_ecs_metric_series(cluster_name, service_name, resolved_region, hours)