# app/collector/metrics/runner.py
"""
Collects metrics using GetMetricData (GMD) — batches up to 500 metrics per
API call vs 1 per call for GetMetricStatistics.

Cost:     Same $0.01/1k metric requests — benefit is fewer TCP connections
          and parallel fetch of all metrics in one round-trip per account.
Filter:   Only running EC2 (instance_state = 'running') — skips stopped.
          ECS metrics excluded — AWS/ECS basic monitoring is FREE (no API cost).
Metrics:  Trimmed per triage:
          - EC2:    CPU, NetworkIn, NetworkOut -- STANDARD TIER ONLY (5 min).
                    Was critical tier (2 min) until 2026-09-10: live DEV
                    data showed this account's EC2 fleet is 100% on AWS
                    basic monitoring (5-min publish, free), so a 2-min poll
                    could only re-return an already-seen datapoint on ~60%
                    of calls -- moved to match AWS's real publication
                    cadence, a deliberate decision after confirming the
                    fleet's actual monitoring mode (not a blind default;
                    see _log_monitoring_mode_mismatch()'s docstring for the
                    detailed-monitoring case this doesn't apply to).
                    DiskRead/Write (low).
          - EBS:    ReadOps, WriteOps, ReadBytes, WriteBytes, QueueLength --
                    STANDARD TIER ONLY (5-6 min), matching AWS's real 5-min
                    publication cadence. Was also re-polled on "low" (15
                    min) -- removed as a duplicate read of the same
                    5-min-resolution data. See metric_audit.md §8.
                    (BurstBalance DROPPED — gp3 irrelevant)
          - RDS:    All 8 kept — revenue-critical, CRITICAL TIER ONLY (2
                    min). Previously had no tier gate at all ("always"),
                    meaning it was redundantly re-polled whenever standard/
                    low coincided with critical's own always-running loop
                    -- same duplicate-call bug as EC2/ELB/EBS above, just
                    never gated to begin with. Critical tier's ~2-min
                    cadence alone already matches RDS's real 1-min publish
                    rate well; gating removes the redundant extra calls
                    without losing any freshness.
          - ELB:    RequestCount, 5XX, TargetResponseTime -- CRITICAL TIER
                    ONLY (2 min), same duplicate-call fix as EC2 above.
                    (4XX DROPPED — client noise. HealthyHostCount /
                    UnHealthyHostCount REMOVED entirely, not just
                    trimmed — confirmed against AWS's own docs that both
                    require a TargetGroup dimension this collector never
                    supplied, so they never returned data via this path;
                    both are now sourced from app/aws/describe_polling.py's
                    free DescribeTargetHealth-based aggregation instead.
                    See apply_fix_alb_healthy_hosts.py.)
          - Lambda: Errors, Duration (standard); Invocations, Throttles (low)
          - ECS:    Removed from paid GMD calls (AWS/ECS basic = free)

Called by scheduler with tier argument — determines collection frequency.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from app.db import get_connection
from app.aws.sts import get_boto3_session
from app.collector.metrics_writer import write_metric, write_metric_history_batch
from app.collector.disk_mounts import all_cwagent_disk_dims, ensure_disk_mount_metric_registered
import boto3

logger = logging.getLogger(__name__)

# ── Metric definitions ────────────────────────────────────────
# Format: (CW_MetricName, db_metric_name, Statistic, Namespace)

EC2_METRICS_CRITICAL = [
    ("CPUUtilization", "cpuutilization", "Average", "AWS/EC2"),
    ("NetworkIn",      "networkin",      "Average", "AWS/EC2"),
    ("NetworkOut",     "networkout",     "Average", "AWS/EC2"),
]

EC2_METRICS_LOW = [
    # Trend OK at 15-min — not alertable
    ("DiskReadBytes",  "diskreadbytes",  "Average", "AWS/EC2"),
    ("DiskWriteBytes", "diskwritebytes", "Average", "AWS/EC2"),
]

EBS_METRICS = [
    ("VolumeReadOps",     "volumereadops",     "Average", "AWS/EBS"),
    ("VolumeWriteOps",    "volumewriteops",    "Average", "AWS/EBS"),
    ("VolumeReadBytes",   "volumereadbytes",   "Average", "AWS/EBS"),
    ("VolumeWriteBytes",  "volumewritebytes",  "Average", "AWS/EBS"),
    ("VolumeQueueLength", "volumequeuelength", "Average", "AWS/EBS"),
    # BurstBalance DROPPED — gp3 volumes: irrelevant
]

RDS_METRICS = [
    ("CPUUtilization",      "cpuutilization", "Average", "AWS/RDS"),
    ("DatabaseConnections", "dbconnections",  "Average", "AWS/RDS"),
    ("FreeStorageSpace",    "freestorage",    "Average", "AWS/RDS"),
    ("ReadIOPS",            "readiops",       "Average", "AWS/RDS"),
    ("WriteIOPS",           "writeiops",      "Average", "AWS/RDS"),
    ("ReadLatency",         "readlatency",    "Average", "AWS/RDS"),
    ("WriteLatency",        "writelatency",   "Average", "AWS/RDS"),
    ("FreeableMemory",      "freeablememory", "Average", "AWS/RDS"),
]

ELB_METRICS = [
    # 4XX DROPPED — mostly client noise
    # HealthyHostCount / UnHealthyHostCount REMOVED (apply_fix_alb_healthy_hosts.py) --
    # confirmed against AWS's own docs that these require BOTH
    # LoadBalancer AND TargetGroup dimensions; this collector only ever
    # supplied LoadBalancer, so this GetMetricData call has NEVER once
    # returned data for either metric -- pure wasted CloudWatch cost.
    # Both are now correctly sourced from describe_polling.py's free
    # DescribeTargetHealth-based aggregation instead (see
    # app/aws/describe_polling.py's poll_alb_target_health()).
    ("RequestCount",              "requestcount",    "Sum",     "AWS/ApplicationELB"),
    ("HTTPCode_Target_5XX_Count", "errors5xx",       "Sum",     "AWS/ApplicationELB"),
    ("TargetResponseTime",        "responselatency", "Average", "AWS/ApplicationELB"),
]

LAMBDA_METRICS_STANDARD = [
    ("Errors",   "errors",   "Sum",     "AWS/Lambda"),
    ("Duration", "duration", "Average", "AWS/Lambda"),
]

LAMBDA_METRICS_LOW = [
    ("Invocations", "invocations", "Sum", "AWS/Lambda"),
    ("Throttles",   "throttles",   "Sum", "AWS/Lambda"),
]

# ECS intentionally excluded — AWS/ECS namespace = free basic monitoring


# ── GMD helpers ───────────────────────────────────────────────

def _resource_dim_value(r):
    """Return the CW dimension value for this resource."""
    rt = r["resource_type"]
    if rt == "elb":
        arn = r["resource_id"]
        return arn.split("loadbalancer/")[-1] if "loadbalancer/" in arn else arn
    if rt == "lambda":
        return r.get("name") or r["resource_id"]
    return r["resource_id"]


_DIM_NAME = {
    "ec2":    "InstanceId",
    "ebs":    "VolumeId",
    "rds":    "DBInstanceIdentifier",
    "elb":    "LoadBalancer",
    "lambda": "FunctionName",
}


def _build_queries(resources, metric_defs):
    """
    Build GetMetricData MetricDataQueries.
    Returns (queries_list, id_map {qid: (resource_db_id, db_metric_name)}).
    """
    queries = []
    id_map  = {}

    for r in resources:
        dim_val  = _resource_dim_value(r)
        dim_name = _DIM_NAME.get(r["resource_type"], "InstanceId")
        if not dim_val:
            continue

        for cw_name, db_name, stat, namespace in metric_defs:
            qid = f"q{len(queries)}"
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace":  namespace,
                        "MetricName": cw_name,
                        "Dimensions": [{"Name": dim_name, "Value": dim_val}],
                    },
                    "Period": 60,
                    "Stat":   stat,
                },
                "ReturnData": True,
            })
            id_map[qid] = (r["id"], db_name)

    return queries, id_map


def _execute_gmd(cw, queries, id_map, minutes=5):
    """Execute one GMD call, write results (latest value + full history).
    Returns datapoint count."""
    if not queries:
        return 0

    end   = datetime.utcnow()
    start = end - timedelta(minutes=minutes)
    count = 0
    history_rows = []

    try:
        resp = cw.get_metric_data(
            MetricDataQueries=queries,
            StartTime=start,
            EndTime=end,
            ScanBy="TimestampDescending",
        )
    except Exception as e:
        logger.error(f"GMD call failed: {e}")
        return 0

    for result in resp.get("MetricDataResults", []):
        values = result.get("Values", [])
        timestamps = result.get("Timestamps", [])
        if not values:
            continue
        resource_db_id, db_name = id_map.get(result["Id"], (None, None))
        if resource_db_id is None:
            continue
        write_metric(resource_db_id, db_name, values[0])  # values[0] = most recent
        count += 1
        # Full history -- every returned datapoint, not just the latest.
        # Timestamps/Values are parallel lists per boto3's own contract.
        for ts, val in zip(timestamps, values):
            history_rows.append((resource_db_id, db_name, val, ts))

    if history_rows:
        write_metric_history_batch(history_rows)

    return count


def _run_gmd(cw, resources, metric_defs, minutes=5, chunk_size=500):
    """Build + chunk + execute GMD. Returns total datapoints written."""
    queries, id_map = _build_queries(resources, metric_defs)
    if not queries:
        return 0

    total = 0
    for i in range(0, len(queries), chunk_size):
        chunk     = queries[i:i + chunk_size]
        chunk_map = {q["Id"]: id_map[q["Id"]] for q in chunk}
        total    += _execute_gmd(cw, chunk, chunk_map, minutes)
    return total


# ── Per-service collectors ────────────────────────────────────

def _log_monitoring_mode_mismatch(resources):
    """
    Visibility only -- does NOT change polling behavior. EC2 basic
    monitoring publishes CPUUtilization/NetworkIn/NetworkOut every 5 min
    (AWS-confirmed, free); detailed monitoring publishes every 1 min
    (opt-in, billed separately from GetMetricData).

    This task runs on the "standard" (5-min) tier -- moved 2026-09-10 from
    "critical" (2-min) after this exact log first showed a real DEV
    account's EC2 fleet was 100% basic monitoring, meaning the old 2-min
    poll was wasting ~60% of its GetMetricData calls on data that hadn't
    changed. At 5-min cadence:
      - BASIC-monitoring instances are now well-matched: no waste, no lost
        freshness, since AWS itself has nothing newer to offer between
        polls.
      - DETAILED-monitoring instances (if any -- billed separately by AWS,
        this app never enables it) are now the interesting case: AWS
        publishes fresh data for them every 1 min, but this poll only
        captures it every 5 -- not wasted spend, but freshness genuinely
        available and not being used. Surfaced below so a human can decide
        whether that's worth a faster, per-instance-aware poll later; not
        fixed automatically (monitoring-hub-metric-audit.md §10 item #7 --
        enabling/prioritizing around detailed monitoring is a deliberate,
        account-owner decision, not this app's to make unilaterally).
    Relies on tags._cw_monitoring_state, populated by discovery/runner.py's
    _discover_ec2 at zero extra API cost.
    """
    basic = detailed = unknown = 0
    for r in resources:
        tags = r.get("tags")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (TypeError, ValueError):
                tags = {}
        state = (tags or {}).get("_cw_monitoring_state", "unknown")
        if state == "enabled":
            detailed += 1
        elif state in ("disabled", "pending"):
            basic += 1
        else:
            unknown += 1
    if detailed:
        logger.info(
            f"    EC2 (standard tier): {detailed} of {len(resources)} instances on "
            f"DETAILED monitoring (1-min publish) but polled every 5 min -- fresher "
            f"data is available from AWS than this app is currently capturing for "
            f"them. {basic} on basic (5-min, already well-matched), {unknown} unknown."
        )
    elif basic:
        logger.info(
            f"    EC2 (standard tier): {basic} of {len(resources)} instances on BASIC "
            f"monitoring (5-min publish), polled every 5 min -- well-matched, no "
            f"wasted calls. {unknown} unknown (discovery not yet re-run since this "
            f"check was added)."
        )


def _collect_ec2_critical(cw, resources):
    _log_monitoring_mode_mismatch(resources)
    # minutes=6: was 3 (matched to the old 2-min "critical" cadence with a
    # ~1-min buffer). Now on the 5-min "standard" tier, widened to match
    # the same minutes=6 lookback every other standard-tier collector in
    # this file (_collect_ebs, _collect_rds, _collect_elb) already uses --
    # a ~1-min buffer against a 5-min cycle, consistent rather than a
    # one-off value.
    n = _run_gmd(cw, resources, EC2_METRICS_CRITICAL, minutes=6)
    logger.info(f"    EC2 (standard tier): {n} datapoints / {len(resources)} instances")

def _collect_ec2_low(cw, resources):
    n = _run_gmd(cw, resources, EC2_METRICS_LOW, minutes=16)
    logger.info(f"    EC2 low: {n} datapoints / {len(resources)} instances")

# CWAgent's mem_used_percent CAN be dimensioned by InstanceId alone, but
# is NOT guaranteed to be -- append_dimensions in the agent's own config
# can add more (ImageId, InstanceType, etc.), varying per instance. The
# collector below discovers each instance's REAL, complete dimension set
# via ListMetrics rather than assuming a fixed shape -- see
# apply_fix_cwagent_mem_dimensions.py for why an earlier, simpler
# version of this (reusing _run_gmd's uniform single-dimension path)
# returned zero data despite correctly identifying which instances have
# the agent installed.


def _ec2_instances_with_cwagent_mem_dims(cw, resources):
    """
    {resource: full_dimension_list} for every EC2 instance that has
    actually published mem_used_percent to CWAgent.

    CORRECTED: originally assumed mem_used_percent is always dimensioned
    by InstanceId alone and built GetMetricData queries with only that
    one dimension -- confirmed live this returns ZERO data even for an
    instance that genuinely, visibly has real memory data (the same
    HCS-PROD-MD-01 instance the Services page already shows 83.4%
    memory for). Root cause, already documented elsewhere in this exact
    codebase (collector_direct.py's _ec2_cwagent_dimensions(), the
    function powering that already-working chart): "GetMetricData needs
    the COMPLETE dimension set a datapoint was actually published
    under; a partial match (InstanceId only) returns nothing." CWAgent's
    append_dimensions config can add extra dimensions (ImageId,
    InstanceType, etc.) beyond InstanceId depending on how it's
    configured -- this varies per instance/config, not something to
    assume uniformly. Now discovers the REAL, complete dimension set via
    the same ListMetrics call already being made (no extra API cost --
    presence-check and dimension-discovery are the same underlying
    data), instead of assuming InstanceId alone. See
    apply_fix_cwagent_mem_dimensions.py.
    """
    result = {}
    for r in resources:
        try:
            resp = cw.list_metrics(
                Namespace="CWAgent",
                MetricName="mem_used_percent",
                Dimensions=[{"Name": "InstanceId", "Value": r["resource_id"]}],
            )
            metrics = resp.get("Metrics", [])
            if metrics:
                result[r["resource_id"]] = (r, metrics[0]["Dimensions"])
        except Exception as e:
            logger.warning(f"CWAgent presence check [{r['resource_id']}]: {e}")
    return result


def _collect_ec2_cwagent_mem(cw, resources):
    cwagent_map = _ec2_instances_with_cwagent_mem_dims(cw, resources)
    if not cwagent_map:
        logger.info(f"    EC2 CWAgent mem: 0/{len(resources)} instances have CWAgent reporting")
        return

    queries = []
    id_map = {}
    for i, (resource, dims) in enumerate(cwagent_map.values()):
        qid = f"cwmem{i}"
        queries.append({
            "Id": qid,
            "MetricStat": {
                "Metric": {
                    "Namespace": "CWAgent",
                    "MetricName": "mem_used_percent",
                    "Dimensions": dims,  # full, DISCOVERED set -- not assumed
                },
                "Period": 60,
                "Stat": "Average",
            },
            "ReturnData": True,
        })
        id_map[qid] = (resource["id"], "mem_used_percent")

    n = _execute_gmd(cw, queries, id_map, minutes=16)
    logger.info(f"    EC2 CWAgent mem: {n} datapoints / {len(cwagent_map)} of {len(resources)} instances")


def _collect_ec2_cwagent_disk(cw, resources, account_id):
    """
    Per-mount disk collection -- supersedes the old root-only
    _ec2_instances_with_cwagent_disk_dims()/single-series approach (see
    app/collector/disk_mounts.py's module docstring for the full
    design). Every mount point CWAgent reports gets its own GMD query
    and its own metric_name (root stays `disk_used_percent`,
    additional mounts get `disk_used_percent__<slug>`); non-root mounts
    are registered into metric_catalog/thresholds/account_metric_selections
    on first sight so they alert through the existing, unmodified
    alert_evaluator join.
    """
    queries = []
    id_map = {}
    instances_reporting = 0

    for r in resources:
        mounts = all_cwagent_disk_dims(cw, r["resource_id"])
        if not mounts:
            continue
        instances_reporting += 1
        for dims, path, metric_name in mounts:
            ensure_disk_mount_metric_registered(account_id, "ec2", metric_name, path)
            qid = f"cwdisk{len(queries)}"
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "CWAgent",
                        "MetricName": "disk_used_percent",  # CW metric name never changes -- only our db metric_name is suffixed
                        "Dimensions": dims,
                    },
                    "Period": 60,
                    "Stat": "Average",
                },
                "ReturnData": True,
            })
            id_map[qid] = (r["id"], metric_name)

    n = _execute_gmd(cw, queries, id_map, minutes=16)
    logger.info(f"    EC2 CWAgent disk: {n} datapoints / {instances_reporting} of {len(resources)} instances (all mounts)")

def _collect_ebs(cw, resources):
    n = _run_gmd(cw, resources, EBS_METRICS, minutes=6)
    logger.info(f"    EBS: {n} datapoints / {len(resources)} volumes")

def _collect_rds(cw, resources):
    n = _run_gmd(cw, resources, RDS_METRICS, minutes=6)
    logger.info(f"    RDS: {n} datapoints / {len(resources)} instances")

def _collect_elb(cw, resources):
    n = _run_gmd(cw, resources, ELB_METRICS, minutes=6)
    logger.info(f"    ELB: {n} datapoints / {len(resources)} LBs")

def _collect_lambda_standard(cw, resources):
    n = _run_gmd(cw, resources, LAMBDA_METRICS_STANDARD, minutes=6)
    logger.info(f"    Lambda standard: {n} datapoints")

def _collect_lambda_low(cw, resources):
    n = _run_gmd(cw, resources, LAMBDA_METRICS_LOW, minutes=16)
    logger.info(f"    Lambda low: {n} datapoints")


# ── Resource fetcher ──────────────────────────────────────────

def _get_resources_for_account(account_id, tier):
    """
    critical/standard: running EC2 only — skips stopped (Phase 1 cost cut)
    low:               all non-terminated (for disk trend metrics)
    ECS always excluded — free basic monitoring, no paid CW calls needed.
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)

    # tags included (previously omitted) so the "critical" tier's EC2
    # basic-vs-detailed monitoring visibility check (see
    # _log_monitoring_mode_mismatch above) can read tags._cw_monitoring_state
    # without a second query. No other caller of this function used tags
    # before, so this is additive, not a behavior change for them.
    if tier in ("critical", "standard"):
        cursor.execute("""
            SELECT id, resource_id, resource_type, name, region, tags
            FROM resources
            WHERE aws_account_id  = %s
              AND instance_state != 'terminated'
              AND NOT (resource_type = 'ec2' AND instance_state != 'running')
        """, (account_id,))
    else:
        cursor.execute("""
            SELECT id, resource_id, resource_type, name, region, tags
            FROM resources
            WHERE aws_account_id  = %s
              AND instance_state != 'terminated'
        """, (account_id,))

    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    grouped = {}
    for r in rows:
        if r["resource_type"] in ("ecs", "ecs_service", "eni"):
            continue  # ECS free; ENI has no useful CW metrics
        key = (r["resource_type"], r["region"])
        grouped.setdefault(key, []).append(r)

    return grouped


# ── Per-account collection ────────────────────────────────────

def _collect_account(account, tier="standard"):
    region = account.get("default_region")
    if not region:
        return

    logger.info(f"[{tier}] {account['account_name']}")

    try:
        session = get_boto3_session(account)
    except Exception as e:
        logger.error(f"Session failed [{account['account_name']}]: {e}")
        return

    grouped = _get_resources_for_account(account["id"], tier)
    tasks   = []  # list of (cw_client, resources, task_type)

    for (resource_type, res_region), resources in grouped.items():
        cw = session.client("cloudwatch", region_name=res_region)

        if resource_type == "ec2":
            # CPU/Network (ec2_critical -- name kept for minimal diff, see
            # note below) run on the "standard" tier (5 min), not
            # "critical" (2 min). Moved 2026-09-10 after live DEV data
            # (the _log_monitoring_mode_mismatch check below) confirmed
            # this account's EC2 fleet is 100% on AWS basic monitoring
            # (5-min publish, free) -- polling it every 2 min could only
            # ever re-return an already-seen datapoint on ~60% of calls,
            # pure wasted GetMetricData spend with zero freshness benefit,
            # since AWS genuinely does not have new data more often than
            # every 5 min for these instances. Deliberate human decision,
            # not an automatic cost-driven default (a fleet running
            # detailed/1-min monitoring should NOT make this same move --
            # see _log_monitoring_mode_mismatch's updated log text below,
            # which now flags the opposite case too).
            #
            # Earlier fix (still true, unaffected by this change): this
            # task previously ALSO fired on "standard" in addition to
            # "critical" (tier in ("critical","standard")), a genuine
            # duplicate-call bug now moot since there's only one tier
            # gate left here.
            if tier == "standard":
                tasks.append((cw, resources, "ec2_critical"))
            if tier == "low":
                tasks.append((cw, resources, "ec2_low"))
                tasks.append((cw, resources, "ec2_cwagent_mem"))
                tasks.append((cw, resources, "ec2_cwagent_disk"))

        elif resource_type == "ebs":
            # EBS metrics publish at 5-min resolution (AWS-confirmed) --
            # polling them again at the 15-min "low" tier on top of the
            # 5-6 min "standard" tier could only ever re-return a
            # datapoint standard tier already fetched. Kept on "standard"
            # only, which already matches EBS's real publication cadence.
            # See monitoring-hub-metric-audit.md §8 flaw #2.
            if tier == "standard":
                tasks.append((cw, resources, "ebs"))

        elif resource_type == "rds":
            # RDS publishes at 1-min resolution (AWS-confirmed, free,
            # automatic -- no basic/detailed distinction like EC2). This
            # had NO tier gate at all until now ("always — revenue-
            # critical") -- since "critical" already runs every ~2 min
            # unconditionally (scheduler.py's run_loop calls it every
            # iteration, no interval check), that alone already gives RDS
            # continuous, well-matched coverage against its real 1-min
            # publish rate. The missing gate meant RDS was re-polled AGAIN,
            # redundantly, in any cycle where "standard" (5 min) or "low"
            # (15 min) happened to also fire in that same loop iteration --
            # the identical duplicate-call pattern already fixed for ALB/
            # EBS/EC2 above, just never gated to begin with. Gated to
            # "critical" only now; revenue-critical priority is preserved
            # (still the fastest tier, still every cycle that tier runs),
            # the redundant extra calls on coincident standard/low cycles
            # are not. See monitoring-hub-metric-audit.md §8, scheduler.py's
            # module docstring (previously flagged this as a known,
            # unfixed issue -- now fixed).
            if tier == "critical":
                tasks.append((cw, resources, "rds"))

        elif resource_type == "elb":
            # ALB metrics (1-min resolution) — "critical" tier's 2-min
            # cadence already exceeds that resolution; dropped from
            # "standard" for the same duplicate-call reason as ec2_critical
            # above. See monitoring-hub-metric-audit.md §8 flaw #1.
            if tier == "critical":
                tasks.append((cw, resources, "elb"))

        elif resource_type == "lambda":
            if tier == "standard":
                tasks.append((cw, resources, "lambda_standard"))
            elif tier == "low":
                tasks.append((cw, resources, "lambda_low"))

    _DISPATCH = {
        "ec2_critical":     _collect_ec2_critical,
        "ec2_low":          _collect_ec2_low,
        "ec2_cwagent_mem":  _collect_ec2_cwagent_mem,
        "ec2_cwagent_disk": _collect_ec2_cwagent_disk,
        "ebs":              _collect_ebs,
        "rds":              _collect_rds,
        "elb":              _collect_elb,
        "lambda_standard":  _collect_lambda_standard,
        "lambda_low":       _collect_lambda_low,
    }

    def _run(task_cw, task_res, task_type):
        fn = _DISPATCH.get(task_type)
        if not fn:
            return
        if task_type == "ec2_cwagent_disk":
            fn(task_cw, task_res, account["id"])  # needs account_id to register new mounts
        else:
            fn(task_cw, task_res)

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_run, c, r, t) for c, r, t in tasks]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                logger.error(f"Task error [{account['account_name']}]: {e}")

    # Extended-tier services -- same "low" (15-min) cadence as EC2
    # CWAgent mem/disk, not latency-sensitive enough for critical/
    # standard tiers. See apply_add_extended_service_discovery.py.
    if tier == "low":
        try:
            from app.collector.metrics.extended import collect_extended_for_account
            collect_extended_for_account(session, account)
        except Exception as e:
            logger.error(f"Extended collection error [{account['account_name']}]: {e}")

    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE aws_accounts SET last_synced_at = NOW() WHERE id = %s",
        (account["id"],)
    )
    conn.commit()
    cursor.close()
    conn.close()


# ── Main entry point ──────────────────────────────────────────

def run_metrics_collection(accounts, tier="standard"):
    """
    tier = 'critical'  — EC2 CPU/Network + RDS + ELB    (2-min cycle)
    tier = 'standard'  — above + EBS + Lambda Errors     (5-min cycle)
    tier = 'low'       — EC2 Disk + Lambda Invocations   (15-min cycle)
    """
    logger.info(f"Metrics [{tier}] — {len(accounts)} accounts")

    with ThreadPoolExecutor(max_workers=min(len(accounts), 10)) as ex:
        futures = {ex.submit(_collect_account, acc, tier): acc for acc in accounts}
        for future in as_completed(futures):
            acc = futures[future]
            try:
                future.result()
            except Exception as e:
                logger.error(f"Metrics failed [{acc['account_name']}]: {e}")

    logger.info(f"Metrics [{tier}] complete")