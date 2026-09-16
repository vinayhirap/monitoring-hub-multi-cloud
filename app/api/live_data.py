# app/api/live_data.py
from fastapi import APIRouter, HTTPException, Query, Depends
from app.auth.permissions import require_permission
from app.auth.authorization import get_accessible_account_ids
from app.aws.collector_direct import (
    collect_ec2_instances,
    collect_ebs_volumes,
    collect_rds_instances,
    collect_lambda_functions,
    collect_s3_buckets,
    collect_elb,
    collect_ecs_clusters,
    collect_nlb,
    collect_acm_certificates,
    collect_backup_resources,
    collect_dms_instances,
    collect_direct_connections,
    collect_state_machines,
    collect_apigateway,
    collect_dynamodb_tables,
    collect_sqs_queues,
    collect_sns_topics,
    collect_cloudfront_distributions,
    collect_elasticache_clusters,
    collect_opensearch_domains,
    collect_eks_clusters,
    collect_efs_filesystems,
    collect_documentdb_clusters,
    collect_neptune_clusters,
    collect_msk_clusters,
    collect_kinesis_streams,
    collect_firehose_streams,
    collect_autoscaling_groups,
    collect_nat_gateways,
    collect_transit_gateways,
    collect_route53_zones,
    collect_waf_web_acls,
    collect_redshift_clusters,
    collect_memorydb_clusters,
    collect_dax_clusters,
    collect_eventbridge_rules,
    collect_kms_keys,
    collect_cloudwatch_log_groups,
    collect_vpn_connections,
    collect_cognito_user_pools,
    collect_global_accelerator_accelerators,
    get_account_summary,
    get_ec2_metric_series,
    get_s3_metric_series,
    _get_ebs_metric_series,
    _metric_history_query_range,
    _get_lambda_metric_series,
    _get_rds_metric_series,
    _get_elb_metric_series,
    _get_ecs_metric_series,
)
from app.db import get_connection
from app.alert_visibility import hidden_metrics_sql
import datetime
import time
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/live", tags=["Live Data"])

# Cache: 30s for near-real-time updates
_accounts_cache: dict = {"data": None, "ts": 0}
CACHE_TTL = 60   # seconds — near-real-time


def invalidate_accounts_cache():
    """
    Call this any time the `alerts` table changes (ack/resolve/mute/
    clear -- see app/api/alerts.py's own _invalidate_cache(), which
    this is the live_data-side counterpart to). Without it, resolving
    or clearing alerts only invalidated alerts.py's OWN cache -- this
    module's separate _accounts_cache kept serving pre-resolve
    critical/warning counts (via _get_active_alert_counts_by_account()/
    _get_ec2_instance_health_by_account(), both read fresh from here
    only on a cache miss) for up to CACHE_TTL seconds after the DB
    already agreed the alert was gone. That's a second, timing-based
    way for the Overview banner/tiles to disagree with the Alerts
    page/sidebar badge on top of the raw-count-vs-distinct-resource
    issue already fixed there -- same class of bug, different cause.
    """
    global _accounts_cache
    _accounts_cache = {"data": None, "ts": 0}


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
                default_region, status, role_arn, external_id,
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


def _check_account_scope(user: dict, account_db_id: int):
    """403s if this account is outside the caller's effective scope.
    None from get_accessible_account_ids means unrestricted (admin);
    otherwise account_db_id must be in the returned set."""
    accessible = get_accessible_account_ids(user)
    if accessible is not None and account_db_id not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this account")


def _check_resource_scope(user: dict, resource_identifier: str):
    """Same idea as _check_account_scope, but for the metrics-by-
    resource-id endpoints below (instance id / volume id / db id /
    function name / bucket name) which don't take account_db_id
    directly -- resolves the owning account via the `resources` table
    first. If the resource isn't tracked yet (not in `resources`),
    this intentionally does NOT block -- there's nothing to check
    against, and returning a scope error would be misleading for what
    is really just an empty/unknown metric lookup."""
    accessible = get_accessible_account_ids(user)
    if accessible is None:
        return
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT aws_account_id FROM resources WHERE resource_id = %s LIMIT 1",
        (resource_identifier,),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if row and row["aws_account_id"] not in accessible:
        raise HTTPException(status_code=403, detail="You do not have access to this resource")


def _get_active_alert_counts_by_account() -> dict:
    """
    THE authoritative source for account-level health: {aws_account_id:
    {"critical": N, "warning": N}}, counting DISTINCT alerting
    resources of each severity, resolved to an account via `resources`
    (which carries aws_account_id for every resource type this app
    discovers — EC2, EBS, RDS, Lambda, ELB, ECS), not just EC2.

    This replaces the previous _get_active_alert_resources(), which
    returned raw (critical_ids, warning_ids) sets that the caller then
    intersected against ONLY that account's EC2 instance_ids — meaning
    a critical alert on an EBS volume, S3 bucket, RDS instance, or
    Lambda function never counted toward that account's status at all.
    That's exactly how a dashboard can show "7 CRITICAL · 3 WARNING"
    in the alerts banner (built straight from the alerts table) while
    the account summary tiles above it say 0 critical, 0 healthy — the
    two were computed from different data. Routing both through this
    one function is what keeps them in agreement.

    "Active" is defined EXACTLY once here, identically to the banner's
    own query (app/api/alerts.py: open_alerts): status = 'active' AND
    resolved_at IS NULL, resolved to an ACTIVE account via the same
    `aws_accounts.status = 'active'` gate the banner uses. No extra
    age window is applied — an alert open for 10 minutes and one open
    for 10 days both count for as long as they remain unresolved.
    (A previous version of this query additionally required
    `triggered_at > NOW() - 24h`, a clause the banner never had; any
    alert older than a day was silently excluded from account health
    while still showing in the banner, reproducing the exact
    banner/tiles disagreement this function exists to prevent. Alert
    *age* is a display concern — see alerts.py's `stale` flag — never
    a reason to stop counting an alert that is still open.)

    Also excludes app.alert_visibility.HIDDEN_FROM_ALERTS_UI_METRICS
    (2026-09-15 fix) -- every alerts.py query this function is meant to
    agree with already excludes these (multivariate_anomaly rows: real
    in the DB, deliberately invisible in the UI). Before this fix, this
    was the one query that didn't, so an account with hidden anomaly-
    detector warnings open could show a HIGHER warning count here than
    on the Alerts page's own Active/Warning tabs for the same account at
    the same moment -- the same disagreement this function's docstring
    above already describes fixing for a different cause.
    """
    try:
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT r.aws_account_id, a.severity, COUNT(DISTINCT a.resource_id) AS cnt
            FROM alerts a
            JOIN resources r      ON r.resource_id = a.resource_id
            JOIN aws_accounts acc ON acc.id = r.aws_account_id
                                   AND acc.status = 'active'
            WHERE a.status = 'active'
              AND a.resolved_at IS NULL
              AND a.metric_name NOT IN ({hidden})
            GROUP BY r.aws_account_id, a.severity
        """.format(hidden=hidden_metrics_sql()))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        out = {}
        for r in rows:
            bucket = out.setdefault(r["aws_account_id"], {"critical": 0, "warning": 0})
            sev = (r["severity"] or "").upper()
            if sev == "CRITICAL":
                bucket["critical"] += r["cnt"]
            elif sev == "WARNING":
                bucket["warning"] += r["cnt"]
        return out
    except Exception as e:
        logger.error(f"Active alert count fetch error: {e}")
        return {}


# Metrics that genuinely mean an EC2 instance is slow, degraded, or down --
# i.e. actually worth painting that instance's HealthRing wedge red/amber
# for. Deliberately a curated ALLOWLIST, not "any active alert on this
# resource": confirmed live on HCS-PROD-MD-01 (2026-09-11) that most
# alerts on a resource can be pure noise with zero performance impact --
# 37 CRITICAL alerts were disk_used_percent__snap_* (permanently-100%-used
# snap loopback/pseudo-fs mounts, see apply_cleanup_disk_mount_noise.py;
# these are read-only images sized exactly to their content and were never
# going to be a real "disk filling up" concern) against exactly 1 alert
# that reflected genuine resource pressure (NetworkOut, WARNING). Painting
# the ring off the raw alert count would have shown that instance as the
# account's worst offender when it was, in reality, the healthiest one
# with real signal on it.
#
# Matched by exact metric_name for EC2's own metrics. disk_used_percent
# (the real root-filesystem reading, see apply_add_cwagent_disk_threshold.py)
# IS included; the auto-registered disk_used_percent__<mount-slug> family
# is deliberately NOT -- see _is_performance_impacting() below for why an
# exact-match set handles that distinction for free.
PERFORMANCE_IMPACTING_EC2_METRICS = {
    "CPUUtilization",      # sustained high CPU -- instance genuinely slow
    "mem_used_percent",    # memory pressure -- instance genuinely slow
    "disk_used_percent",   # root filesystem full -- instance genuinely degraded
    "NetworkIn",           # bandwidth saturation
    "NetworkOut",          # bandwidth saturation
    "StatusCheckFailed",             # >0 = instance/system health check failing -- literally down
    "StatusCheckFailed_Instance",
    "StatusCheckFailed_System",
    "CPUCreditBalance",    # T-class credits nearly exhausted -- imminent CPU throttling
}

# EBS metrics that mean the ATTACHED instance is I/O-bottlenecked -- rolls
# up to that instance's wedge the same as the instance's own metrics
# above. Deliberately excludes plain throughput counters (VolumeReadOps/
# WriteOps/*Bytes, VolumeIdleTime, VolumeTotalRead/WriteTime) -- a volume
# doing a lot of I/O isn't itself a problem; only these two (per their own
# catalog descriptions in metric_catalog_data.py -- "I/O requests waiting,
# bottleneck signal" and burst-credit exhaustion) indicate the instance is
# actually waiting on disk.
PERFORMANCE_IMPACTING_EBS_METRICS = {
    "VolumeQueueLength",
    "BurstBalance",
}


def _is_performance_impacting(resource_type: str, metric_name: str) -> bool:
    """True if an alert on this (resource_type, metric_name) means the
    attached EC2 instance is actually slow/degraded/down -- see the two
    sets above for the reasoning and the concrete noise case that
    prompted this. Everything else on the instance/its attached EBS
    still counts toward the account-wide critical/warning tiles via
    _get_active_alert_counts_by_account() -- this function ONLY narrows
    what the HealthRing wedges react to."""
    if resource_type == "ec2":
        return metric_name in PERFORMANCE_IMPACTING_EC2_METRICS
    if resource_type == "ebs":
        return metric_name in PERFORMANCE_IMPACTING_EBS_METRICS
    return False


def _get_running_ec2_ids_by_account() -> dict:
    """
    {aws_account_id: {running EC2 resource_id, ...}}, straight from
    `resources` (kept current by discovery_ec2.py). Used to scope
    _get_ec2_instance_health_by_account() below to instances that
    actually appear in the HealthRing's own denominator (ec2_running
    -- see Overview.jsx's HealthRing/`total`). Without this, an alert
    on an EBS volume still attached to a now-STOPPED instance would
    count toward a wedge that doesn't exist in a ring sized by running
    count alone.
    """
    try:
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT aws_account_id, resource_id
            FROM resources
            WHERE resource_type = 'ec2' AND instance_state = 'running'
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        out = {}
        for r in rows:
            out.setdefault(r["aws_account_id"], set()).add(r["resource_id"])
        return out
    except Exception as e:
        logger.error(f"Running EC2 id fetch error: {e}")
        return {}


def _get_ec2_instance_health_by_account() -> dict:
    """
    EC2 "server" health for the Overview dashboard's HealthRing,
    keyed by aws_account_id: {aws_account_id: {"critical": N,
    "warning": N}}, where N counts DISTINCT running EC2 instances --
    not raw alert rows, and not alerts on resource types the ring
    doesn't represent.

    _get_active_alert_counts_by_account() above is deliberately
    account/severity-wide -- every resource type (EC2, EBS, RDS,
    Lambda, S3, ELB...) rolls into it, which is correct for the top
    status pill and the account-level "N critical / N warning" tiles
    (an S3 policy alert SHOULD turn the account red). But the ring
    drawn under each account card visually represents that account's
    running EC2 fleet specifically (its centre label literally reads
    "N running"). Feeding it the account-wide alert count meant an
    alert with zero relationship to any EC2 instance -- an S3 bucket,
    a Lambda function, anything -- could still paint EC2 wedges red or
    amber: e.g. 2 critical + 2 warning anywhere in the account, 6
    running instances, ring shows 4 of 6 wedges unhealthy regardless
    of which resource actually alerted. Reported by Vinay 2026-09-10
    (dashboard screenshot: 6 running, ring showing 4 unhealthy wedges
    while the account only had 6 running instances and the alerts
    were partly on non-EC2 resources).

    Fix, matching how tools like Datadog/CloudWatch roll host status
    up from attached resources: an instance's wedge is only unhealthy
    if the alert is on the instance itself, OR on an EBS volume / ENI
    physically attached to it, AND the alerting metric is actually
    performance-impacting (see PERFORMANCE_IMPACTING_EC2_METRICS /
    _EBS_METRICS below -- added after a second real case: an alert
    existing on the resource is not the same as that resource actually
    being slow or down, e.g. a permanently-100%-used snap loopback
    mount alerting CRITICAL forever). discovery_ec2.py already tags
    every EBS volume and ENI with tags.parent_ec2 = <instance_id> at
    collection time (see its EBS/ENI INHERITANCE blocks) -- this reads
    that same linkage rather than adding a new one. Alerts on any
    other resource type (S3, Lambda, RDS, ELB, SQS, ...), or on a
    matching resource type but a non-performance-impacting metric,
    never touch this ring; they still count toward the account-wide
    tiles via _get_active_alert_counts_by_account(), unchanged.

    Per-instance severity is the worst of its own + any inherited
    alerts (critical beats warning) -- but ONLY among alerts whose
    metric is actually performance-impacting; see
    PERFORMANCE_IMPACTING_EC2_METRICS/_EBS_METRICS and
    _is_performance_impacting() above. An instance is never counted in
    both buckets. Stopped/terminated instances are excluded via
    _get_running_ec2_ids_by_account() -- see that function's docstring.
    """
    try:
        conn   = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT r.aws_account_id, r.resource_type, r.resource_id, r.tags,
                   a.severity, a.metric_name
            FROM alerts a
            JOIN resources r      ON r.resource_id = a.resource_id
            JOIN aws_accounts acc ON acc.id = r.aws_account_id
                                   AND acc.status = 'active'
            WHERE a.status = 'active'
              AND a.resolved_at IS NULL
              AND r.resource_type IN ('ec2', 'ebs', 'eni')
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        running_by_account = _get_running_ec2_ids_by_account()

        # account_id -> instance_id -> worst severity seen so far
        worst: dict = {}
        for r in rows:
            acc_id = r["aws_account_id"]

            if not _is_performance_impacting(r["resource_type"], r["metric_name"]):
                continue

            if r["resource_type"] == "ec2":
                instance_id = r["resource_id"]
            else:
                tags = r.get("tags")
                if isinstance(tags, str):
                    try:
                        tags = json.loads(tags)
                    except (TypeError, ValueError):
                        tags = {}
                instance_id = (tags or {}).get("parent_ec2")

            if not instance_id:
                continue
            if instance_id not in running_by_account.get(acc_id, set()):
                continue

            sev = (r["severity"] or "").upper()
            if sev not in ("CRITICAL", "WARNING"):
                continue

            bucket = worst.setdefault(acc_id, {})
            if sev == "CRITICAL":
                bucket[instance_id] = "CRITICAL"
            elif bucket.get(instance_id) != "CRITICAL":
                bucket[instance_id] = "WARNING"

        out = {}
        for acc_id, instances in worst.items():
            out[acc_id] = {
                "critical": sum(1 for s in instances.values() if s == "CRITICAL"),
                "warning":  sum(1 for s in instances.values() if s == "WARNING"),
            }
        return out
    except Exception as e:
        logger.error(f"EC2 instance health fetch error: {e}")
        return {}


@router.get("/accounts")
def live_accounts(current_user: dict = Depends(require_permission("resources.view"))):
    global _accounts_cache

    now = time.time()
    if _accounts_cache["data"] is not None and now - _accounts_cache["ts"] < CACHE_TTL:
        accessible = get_accessible_account_ids(current_user)
        cached = _accounts_cache["data"]
        if accessible is not None:
            cached = [a for a in cached if a["id"] in accessible]
        return cached

    accounts = _get_db_accounts()
    accessible = get_accessible_account_ids(current_user)
    if accessible is not None:
        accounts = [a for a in accounts if a["id"] in accessible]

    alert_counts_by_account = _get_active_alert_counts_by_account()
    ec2_health_by_account   = _get_ec2_instance_health_by_account()

    def process_account(acc):
        region  = acc.get("default_region")
        summary = get_account_summary(region, role_arn=acc.get("role_arn"), external_id=acc.get("external_id"))
        running = summary.get("ec2_running", 0)
        total   = summary.get("ec2_total",   0)
        avg_cpu = summary.get("ec2_avg_cpu", 0)

        counts        = alert_counts_by_account.get(acc["id"], {"critical": 0, "warning": 0})
        acct_critical = counts["critical"]
        acct_warning  = counts["warning"]

        # EC2-scoped rollup for the HealthRing only -- see
        # _get_ec2_instance_health_by_account()'s docstring. Deliberately
        # separate from acct_critical/acct_warning above, which stay
        # account-wide (all resource types) and keep driving `health`,
        # the status pill, and the CRITICAL/WARNING tiles exactly as
        # before -- only the ring's own numbers change here.
        ec2_health          = ec2_health_by_account.get(acc["id"], {"critical": 0, "warning": 0})
        ec2_critical_ring   = ec2_health["critical"]
        ec2_warning_ring    = ec2_health["warning"]

        # Real active alerts (any resource type) are authoritative.
        # avg_cpu is only a fallback heuristic for the rare case where
        # nothing has alerted yet at all — it must never override an
        # actual open alert, critical or warning.
        if acct_critical > 0:
            health = "critical"
        elif acct_warning > 0:
            health = "warning"
        elif avg_cpu > 80:
            health = "critical"
        elif avg_cpu > 60:
            health = "warning"
        else:
            health = "healthy"

        unhealthy_count = min(acct_critical + acct_warning, running) if running else (acct_critical + acct_warning)
        healthy_count   = max(running - unhealthy_count, 0)

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
            # Was hardcoded to 0 before this fix, regardless of reality.
            "alerts":           acct_critical + acct_warning,
            "critical_alerts":  acct_critical,
            "warning_alerts":   acct_warning,
            # EC2-scoped counts for the HealthRing wedge colouring --
            # see _get_ec2_instance_health_by_account(). Intentionally
            # separate from critical_alerts/warning_alerts above.
            "ec2_critical_instances": ec2_critical_ring,
            "ec2_warning_instances":  ec2_warning_ring,
            "instance_count":   total,
            "healthy_resources":   healthy_count,
            "unhealthy_resources": unhealthy_count,
            "services":         services,
            "created_at":       acc.get("created_at"),
            "last_synced_at":   acc.get("last_synced_at"),
        })

    result = []
    with ThreadPoolExecutor(max_workers=min(len(accounts), 8) or 1) as ex:
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


@router.get("/ec2/{account_db_id}")
def live_ec2(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):
    _check_account_scope(current_user, account_db_id)
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_ec2_instances(region))


@router.get("/ebs/{account_db_id}")
def live_ebs(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):
    _check_account_scope(current_user, account_db_id)
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_ebs_volumes(region))


@router.get("/rds/{account_db_id}")
def live_rds(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):
    _check_account_scope(current_user, account_db_id)
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_rds_instances(region))


@router.get("/lambda/{account_db_id}")
def live_lambda(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):
    _check_account_scope(current_user, account_db_id)
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_lambda_functions(region))


@router.get("/s3/{account_db_id}")
def live_s3(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):
    _check_account_scope(current_user, account_db_id)
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_s3_buckets(region))


@router.get("/elb/{account_db_id}")
def live_elb(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):
    _check_account_scope(current_user, account_db_id)
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_elb(region))


@router.get("/ecs/{account_db_id}")
def live_ecs(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):
    _check_account_scope(current_user, account_db_id)
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region") 
    return _serialize(collect_ecs_clusters(region))


# ── Real-time per-service resource counts (ALL providers, ALL tiers) ──
# Used by the Services page to decide whether a tile should be shown at
# all — dynamically, per account, per service — instead of a hardcoded
# list anywhere in the app.
#
# PREVIOUSLY: AWS-only, 41 separate live boto3 calls per page load via
# _RESOURCE_COLLECTORS (one per curated AWS service), with GCP/Azure
# accounts getting no count at all (frontend comment: "no resource-count
# data source yet for GCP/Azure — they always show"). Two real problems
# with that: (1) a transient AWS error on any ONE of those 41 live calls
# (timeout, AccessDenied) came back as None, and the frontend treated
# Only AWS discovery (core + extended) runs on a known recurring
# schedule today -- app/collector/scheduler.py's DISCOVERY_INTERVAL,
# 15 minutes. 3 missed cycles (45 min) is a deliberately generous
# margin above that -- one slow/skipped cycle from an account-level
# error (see discovery/runner.py's per-account try/except) shouldn't
# make a tile flicker, but a resource genuinely gone from 3 consecutive
# cycles is a real signal, not noise. GCP/Azure are excluded from this
# entirely -- see 042_resource_last_seen_tracking.sql for why applying
# any time-based staleness rule to them today would be actively wrong.
_AWS_STALE_AFTER_MINUTES = 45


def _resource_scope_sql(provider: str) -> str:
    """Returns the extra WHERE-clause fragment (and nothing else -- no
    params) that scopes a `resources` query to "still considered
    present" for the given provider. AWS gets a recency filter because
    AWS has a real, known discovery cadence to measure staleness
    against; every other provider gets no extra filter, because "not
    recently re-synced" and "no longer exists" are NOT the same thing
    for a provider whose discovery only runs at onboarding or on a
    manual click."""
    if provider == "aws":
        return f" AND last_seen_at > NOW() - INTERVAL {_AWS_STALE_AFTER_MINUTES} MINUTE"
    return ""


# None exactly like a real zero -- so a broken/under-permissioned
# collector silently hid a service instead of surfacing that it wasn't
# being checked; (2) GCP/Azure had zero presence-checking at all.
#
# NOW: every provider's discovery pipeline — app/collector/discovery/
# runner.py (AWS core), app/collector/discovery/extended.py (AWS
# extended-tier), app/providers/gcp/discovery.py, app/providers/azure/
# discovery.py — already upserts every resource it finds into the same
# shared `resources` table (aws_account_id, resource_type), regardless
# of cloud. So instead of live-calling 41 AWS APIs and leaving GCP/Azure
# uncovered, this does ONE query against data that's already collected,
# for whichever provider the account actually is. That means:
#   - No live-call failure mode left to confuse with "genuinely zero" —
#     a resource_type with no rows IS zero, not "unknown".
#   - Automatically covers every resource_type any collector has ever
#     written for this account, core or extended, AWS or GCP or Azure —
#     no per-service allowlist to maintain here.
#   - One fast DB read instead of up to 41 parallel live API calls.
#
# Trade-off worth knowing: this is only as fresh as the last discovery
# cycle for that account. For AWS that's within ~15 minutes; for
# GCP/Azure, discovery is onboarding-or-manual-only (no recurring
# schedule exists yet — confirmed by reading scheduler.py end to end),
# so "freshness" there can genuinely be however long it's been since
# the account was set up or last manually re-synced.
#
# A resource deleted in the actual cloud drops out of this count for
# AWS once last_seen_at falls outside _AWS_STALE_AFTER_MINUTES — no
# row is ever deleted, so its full metric_history stays intact if
# discovery finds it again a cycle later (see
# 042_resource_last_seen_tracking.sql for why a hard delete would be
# dangerous here: ON DELETE CASCADE would wipe that history
# irreversibly on what might just be one flaky cycle). GCP/Azure rows
# are never filtered by age at all, for the reasons above — closing
# that gap needs recurring GCP/Azure discovery first, which doesn't
# exist yet.
@router.get("/resource-counts/{account_db_id}")
def live_resource_counts(account_db_id: int, current_user: dict = Depends(require_permission("resources.view"))):
    _check_account_scope(current_user, account_db_id)
    acc = _get_db_account(account_db_id)  # 404s if the account doesn't exist / isn't accessible
    provider = acc.get("provider") or "aws"

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT resource_type, COUNT(*) AS cnt FROM resources WHERE aws_account_id = %s"
            + _resource_scope_sql(provider) + " GROUP BY resource_type",
            (account_db_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()

    return {row["resource_type"]: row["cnt"] for row in rows}


# ── Generic resource listing (any service, any provider, any tier) ────
# Backs the "Services" page's generic detail view for the ~30+ services
# that don't have a bespoke page like ServiceDetail.jsx's EC2/EBS/RDS/S3/
# ECS/ELB/Lambda views. Same `resources` table this file's resource-
# counts endpoint above now reads from, filtered to one resource_type —
# works identically for an AWS-extended service, a GCP service, or an
# Azure service, since discovery for all of them upserts into this one
# table (see the comment above live_resource_counts for the full list of
# discovery modules this relies on, and for why the staleness filter
# below only applies to AWS). No per-provider branching beyond that one
# filter is needed here.
@router.get("/resources-list/{account_db_id}/{service}")
def live_resources_list(
    account_db_id: int,
    service: str,
    current_user: dict = Depends(require_permission("resources.view")),
):
    _check_account_scope(current_user, account_db_id)
    acc = _get_db_account(account_db_id)
    provider = acc.get("provider") or "aws"

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT resource_id, name, region, tags, instance_state, created_at, last_seen_at
            FROM resources
            WHERE aws_account_id = %s AND resource_type = %s
            """ + _resource_scope_sql(provider) + """
            ORDER BY name
            """,
            (account_db_id, service),
        )
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()

    for r in rows:
        if isinstance(r.get("tags"), str):
            try:
                r["tags"] = json.loads(r["tags"])
            except (TypeError, ValueError):
                r["tags"] = {}
    return _serialize(rows)


# ── Generic metric charts (any service, any provider, any tier) ───────
# Powers the chart section on the generic detail page for any service
# without a bespoke page. Deliberately reuses
# app/aws/collector_direct.py's _metric_history_query_range() as-is --
# that's the exact same function every bespoke EC2/EBS/RDS/Lambda chart
# in this app already calls, reading the same metric_history table
# every provider's metrics collector already writes into (Phase 1 GMD
# for AWS, Phase 2/3 for Azure/GCP -- see that function's own
# docstring). No new query logic, no new failure mode: same
# never-raises, degrade-to-empty-list contract as every existing chart.
#
# What's genuinely new here is deciding WHICH metric names to even try
# charting for an arbitrary service, since there's no per-service chart
# component (like ServiceDetail.jsx's switch-per-service) to hardcode
# that list. Answer: metric_catalog already has this mapping -- it's
# the same table Settings -> Metrics reads to know which metrics exist
# per service, per provider. Only metrics with at least one real data
# point in range are returned, so this never renders a wall of empty
# charts for metrics that are enabled but haven't collected anything
# yet (or aren't the right unit/resource combination for this specific
# resource).
@router.get("/metrics/generic/{account_db_id}/{service}/{resource_id}")
def live_generic_metrics(
    account_db_id: int,
    service: str,
    resource_id: str,
    hours: int = Query(24),
    current_user: dict = Depends(require_permission("metrics.view")),
):
    _check_account_scope(current_user, account_db_id)
    acc = _get_db_account(account_db_id)
    provider = acc.get("provider") or "aws"

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT DISTINCT metric_name, unit, statistic, description
            FROM metric_catalog
            WHERE provider = %s AND service = %s
              AND (metric_name != '' AND metric_name IS NOT NULL)
            """,
            (provider, service),
        )
        catalog_rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()

    end   = datetime.datetime.utcnow()
    start = end - datetime.timedelta(hours=hours)

    result = {}
    for row in catalog_rows:
        series = _metric_history_query_range(
            service, resource_id, row["metric_name"], start, end, match_field="resource_id"
        )
        if not series:
            continue  # no data yet for this metric/resource combo -- skip, don't render an empty chart
        result[row["metric_name"]] = {
            "unit": row.get("unit"),
            "statistic": row.get("statistic"),
            "description": row.get("description"),
            "series": series,
        }
    return result


# ── CloudWatch metric series endpoints ───────────────────────

@router.get("/metrics/ec2/{instance_id}")
def live_ec2_metrics(
    instance_id: str,
    region: str = Query(None),
    hours: int  = Query(6),
    current_user: dict = Depends(require_permission("metrics.view")),
):
    _check_resource_scope(current_user, instance_id)
    return get_ec2_metric_series(instance_id, region, hours)


@router.get("/metrics/ebs/{volume_id}")
def live_ebs_metrics(
    volume_id: str,
    region: str = Query(None),
    hours: int  = Query(6),
    current_user: dict = Depends(require_permission("metrics.view")),
):
    _check_resource_scope(current_user, volume_id)
    return _get_ebs_metric_series(volume_id, region, hours)


@router.get("/metrics/rds/{db_id}")
def live_rds_metrics(
    db_id: str,
    region: str = Query(None),
    hours: int  = Query(6),
    current_user: dict = Depends(require_permission("metrics.view")),
):
    _check_resource_scope(current_user, db_id)
    return _get_rds_metric_series(db_id, region, hours)


@router.get("/metrics/lambda/{function_name}")
def live_lambda_metrics(
    function_name: str,
    region: str = Query(None),
    hours: int  = Query(6),
    current_user: dict = Depends(require_permission("metrics.view")),
):
    _check_resource_scope(current_user, function_name)
    return _get_lambda_metric_series(function_name, region, hours)


@router.get("/metrics/s3/{bucket_name:path}")
def live_s3_metrics(
    bucket_name: str,
    hours: int = Query(24),
    current_user: dict = Depends(require_permission("metrics.view")),
):
    _check_resource_scope(current_user, bucket_name)
    return get_s3_metric_series(bucket_name, hours)


@router.get("/metrics/elb/{account_db_id}")
def live_elb_metrics(
    account_db_id: int,
    lb_name: str = Query(..., description="Load balancer name"),
    region: str  = Query(None),
    hours: int   = Query(6),
    current_user: dict = Depends(require_permission("metrics.view")),
):
    """
    ELB CloudWatch metrics for a specific load balancer by name.
    Frontend calls: /api/live/metrics/elb/{accountId}?lb_name=<name>&region=<r>&hours=<h>
    """
    _check_account_scope(current_user, account_db_id)
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
    current_user: dict = Depends(require_permission("metrics.view")),
):
    """
    ECS CloudWatch metrics for a cluster or specific service.
    Frontend calls: /api/live/metrics/ecs/{accountId}?cluster_name=<c>&service_name=<s>&region=<r>&hours=<h>
    """
    _check_account_scope(current_user, account_db_id)
    acc = _get_db_account(account_db_id)
    resolved_region = region or acc.get("default_region")
    return _get_ecs_metric_series(cluster_name, service_name, resolved_region, hours)