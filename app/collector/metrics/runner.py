# app/collector/metrics/runner.py
"""
POLLING MODEL (2026-09-23 metric-polling audit): which metric is polled on
which tier, with which look-back, is defined ONLY in
app/collector/polling_model.py (AWS_CORE_METRICS). The per-service notes
below are history; where they disagree with polling_model.py, the model
wins.

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
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from app.db import get_connection
from app.aws.sts import get_boto3_session
from app.aws.boto_config import STANDARD_RETRY
from app.collector.metrics_writer import write_metrics_batch, write_metric_history_batch
from app.collector.disk_mounts import all_cwagent_disk_dims, ensure_disk_mount_metric_registered
import boto3

logger = logging.getLogger(__name__)

# ── Metric definitions ────────────────────────────────────────
# Format: (CW_MetricName, db_metric_name, Statistic, Namespace)

# Metric definitions live in app/collector/polling_model.py (single
# source of truth shared with the alert evaluator's freshness windows).
from app.collector import polling_model
from app.collector import api_usage

CORE_METRICS = polling_model.AWS_CORE_METRICS


def _legacy(resource_type, tier):
    return [(m.cw_name, m.db_name, m.stat, m.namespace) for m in CORE_METRICS
            if m.resource_type == resource_type and m.tier == tier]


# Legacy names kept for importers; derived, not a second source of truth.
EC2_METRICS_CRITICAL = _legacy("ec2", "standard")
EC2_METRICS_LOW = []          # DiskRead/WriteBytes removed: instance-store only
EBS_METRICS = _legacy("ebs", "standard") + _legacy("ebs", "low")
RDS_METRICS = _legacy("rds", "critical") + _legacy("rds", "standard") + _legacy("rds", "low")
ELB_METRICS = _legacy("elb", "critical")
LAMBDA_METRICS_STANDARD = _legacy("lambda", "standard")
LAMBDA_METRICS_LOW = _legacy("lambda", "low")

# Tier currently being collected (set per run_metrics_collection call; the
# scheduler runs tiers sequentially) -- used only for api_usage labelling.
_CURRENT_TIER = "unknown"

# SEARCH expressions per GetMetricData request (CloudWatch's documented
# per-request cap is 5 SEARCH expressions).
GMD_MAX_SEARCH_PER_REQUEST = 5
_GMD_MAX_PAGES = 50


GMD_MAX_QUERIES = 500

# Resources discovery has not re-confirmed for this long are skipped (the
# resource was deleted; its `resources` row is only pruned much later).
# Discovery refreshes last_seen_at every 15 min, so 48h only excludes rows
# that are genuinely gone -- each of which was a billed, always-empty
# GetMetricData query every cycle.
STALE_RESOURCE_HOURS = 48


def _chunk_gmd_queries(queries, size=GMD_MAX_QUERIES):
    """Split into <= size chunks WITHOUT separating a ReturnData=False
    input query from the Expression that references it (CWAgent Windows
    disk: raw + "100 - raw") -- a chunk never ends on a hidden input."""
    chunks, current = [], []
    for q in queries:
        current.append(q)
        if len(current) >= size and q.get("ReturnData", True):
            chunks.append(current)
            current = []
        elif len(current) >= size:
            # last one is a hidden input: move it to the next chunk
            carry = current.pop()
            chunks.append(current)
            current = [carry]
    if current:
        chunks.append(current)
    return chunks

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


def _is_search(q):
    return "SEARCH(" in (q.get("Expression") or "")


def _request_chunks(queries):
    """<=500 queries per request, and SEARCH-expression queries in their own
    requests of <= GMD_MAX_SEARCH_PER_REQUEST (a request over the SEARCH cap
    fails as a whole)."""
    plain = [q for q in queries if not _is_search(q)]
    search = [q for q in queries if _is_search(q)]
    chunks = _chunk_gmd_queries(plain) if plain else []
    for i in range(0, len(search), GMD_MAX_SEARCH_PER_REQUEST):
        chunks.append(search[i:i + GMD_MAX_SEARCH_PER_REQUEST])
    return chunks


def _billed_units(chunk):
    """Metrics requested in a chunk: MetricStat queries count 1 each;
    SEARCH expressions are billed per metric they match (unknown up front,
    counted as 1 here -- a floor, flagged in api_usage's docstring)."""
    return sum(1 for q in chunk if "MetricStat" in q or _is_search(q))


def _get_metric_data_all_pages(cw, chunk, start, end):
    """One GetMetricData request, following NextToken (a single response
    carries at most 100,800 datapoints). Returns {Id: (timestamps, values)}
    newest first."""
    merged, token, pages = {}, None, 0
    while True:
        kwargs = dict(MetricDataQueries=chunk, StartTime=start, EndTime=end,
                      ScanBy="TimestampDescending")
        if token:
            kwargs["NextToken"] = token
        resp = cw.get_metric_data(**kwargs)
        pages += 1
        for result in resp.get("MetricDataResults", []):
            ts_list, val_list = merged.setdefault(result["Id"], ([], []))
            ts_list.extend(result.get("Timestamps", []))
            val_list.extend(result.get("Values", []))
        token = resp.get("NextToken")
        # hard page cap: a misbehaving endpoint/mocked client must never
        # loop forever (50 pages = 5M datapoints, far above any real batch)
        if not isinstance(token, str) or not token or pages >= _GMD_MAX_PAGES:
            return merged, pages


def _execute_gmd(cw, queries, id_map, minutes=5):
    """Execute GetMetricData for `queries` (chunked, all pages), write
    results (latest value + full history). Returns series-with-data count."""
    if not queries:
        return 0

    end   = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    count = 0
    latest_rows = []
    history_rows = []

    for chunk in _request_chunks(queries):
        try:
            results, pages = _get_metric_data_all_pages(cw, chunk, start, end)
        except Exception as e:
            logger.error(f"GMD call failed ({len(chunk)} queries): {e}")
            api_usage.record("aws", _CURRENT_TIER, calls=1, units=_billed_units(chunk))
            continue
        api_usage.record("aws", _CURRENT_TIER, calls=pages, units=_billed_units(chunk))

        for qid, (timestamps, values) in results.items():
            if not values:
                continue
            resource_db_id, db_name = id_map.get(qid, (None, None))
            if resource_db_id is None:
                continue
            latest_rows.append((resource_db_id, db_name, values[0]))  # newest first
            count += 1
            for ts, val in zip(timestamps, values):
                history_rows.append((resource_db_id, db_name, val, ts))

    if latest_rows:
        write_metrics_batch(latest_rows)
    if history_rows:
        write_metric_history_batch(history_rows)

    return count


def _run_gmd(cw, resources, metric_defs, minutes=5, chunk_size=GMD_MAX_QUERIES):
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


def _tags(r):
    t = r.get("tags")
    if isinstance(t, str):
        try:
            t = json.loads(t)
        except (TypeError, ValueError):
            t = {}
    return t if isinstance(t, dict) else {}


def _passes_gate(r, gate):
    if gate is None:
        return True
    if gate in ("alb", "nlb"):
        prefix = "app/" if gate == "alb" else "net/"
        return (_resource_dim_value(r) or "").startswith(prefix)
    if gate == "tclass":
        return (_tags(r).get("_instance_type") or "").lower().startswith("t")
    if gate == "replica":
        return bool(_tags(r).get("_replica_source"))
    return False


def _collect_core(cw, resources, resource_type, tier):
    """Every CORE_METRICS definition for (resource_type, tier), grouped by
    look-back and gate so each GetMetricData batch only carries resources
    the definition applies to."""
    defs = [m for m in CORE_METRICS if m.resource_type == resource_type and m.tier == tier]
    if not defs:
        return 0
    if resource_type == "ec2" and tier == "standard":
        _log_monitoring_mode_mismatch(resources)
    groups = {}
    for m in defs:
        groups.setdefault((m.lookback_min, m.gate), []).append(
            (m.cw_name, m.db_name, m.stat, m.namespace))
    total = 0
    for (lookback, gate), metric_defs in groups.items():
        gated = [r for r in resources if _passes_gate(r, gate)]
        if gated:
            total += _run_gmd(cw, gated, metric_defs, minutes=lookback)
    logger.info(f"    {resource_type} [{tier}]: {total} series / {len(resources)} resources")
    return total


# ── CWAgent dimension cache ─────────────────────────────────────────────
# ListMetrics per instance per poll was the dominant non-GMD call volume
# (and a billed "standard" API above the 1M free tier). Dimension sets
# only change when the agent config changes -- re-discover hourly.
_CWAGENT_DIM_TTL = 3600
_cwagent_cache = {}          # (kind, region, instance_id) -> (expires, value)
_cwagent_cache_lock = threading.Lock()


def _cache_get(key):
    with _cwagent_cache_lock:
        hit = _cwagent_cache.get(key)
        if hit and hit[0] > time.time():
            return True, hit[1]
    return False, None


def _cache_put(key, value):
    with _cwagent_cache_lock:
        _cwagent_cache[key] = (time.time() + _CWAGENT_DIM_TTL, value)


def _cached_disk_dims(cw, r):
    key = ("disk", r.get("region"), r["resource_id"])
    ok, val = _cache_get(key)
    if ok:
        return val
    val = all_cwagent_disk_dims(cw, r["resource_id"])
    _cache_put(key, val)
    return val


def _ec2_instances_with_cwagent_mem_dims(cw, resources):
    """Cached wrapper: {instance_id: (resource, dims, cw_metric_name)}."""
    result, misses = {}, []
    for r in resources:
        ok, val = _cache_get(("mem", r.get("region"), r["resource_id"]))
        if ok:
            if val:
                result[r["resource_id"]] = (r, val[0], val[1])
        else:
            misses.append(r)
    if misses:
        found = _ec2_instances_with_cwagent_mem_dims_uncached(cw, misses)
        for r in misses:
            hit = found.get(r["resource_id"])
            _cache_put(("mem", r.get("region"), r["resource_id"]),
                       (hit[1], hit[2]) if hit else None)
            if hit:
                result[r["resource_id"]] = hit
    return result


def _ec2_instances_with_cwagent_mem_dims_uncached(cw, resources):
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
                result[r["resource_id"]] = (r, metrics[0]["Dimensions"], "mem_used_percent")
                continue
        except Exception as e:
            logger.warning(f"CWAgent presence check [{r['resource_id']}]: {e}")
            continue

        # Linux metric name found nothing -- try Windows' equivalent
        # before giving up. CWAgent's memory metric is OS-dependent:
        # Windows publishes "Memory % Committed Bytes In Use" instead
        # of "mem_used_percent". Confirmed live against
        # i-0424cb66e22e05a21 (U4RAD-JUMP, a Windows instance) that
        # CWAgent was installed and actively reporting -- just under
        # this different name, which nothing in this codebase searched
        # for until now. See WINDOWS_DISK_METRIC_NAME in
        # app/collector/disk_mounts.py for the full incident writeup
        # (same root cause, found via the disk metric first).
        #
        # ASSUMPTION, not yet independently verified against a known
        # real Windows box's Task-Manager-reported memory usage: this
        # metric is treated as equivalent in meaning to mem_used_percent
        # (higher = more memory pressure, no inversion needed, unlike
        # the disk metric).
        try:
            resp = cw.list_metrics(
                Namespace="CWAgent",
                MetricName="Memory % Committed Bytes In Use",
                Dimensions=[{"Name": "InstanceId", "Value": r["resource_id"]}],
            )
            metrics = resp.get("Metrics", [])
            if metrics:
                result[r["resource_id"]] = (r, metrics[0]["Dimensions"], "Memory % Committed Bytes In Use")
        except Exception as e:
            logger.warning(f"CWAgent presence check (Windows) [{r['resource_id']}]: {e}")
    return result


def _collect_ec2_cwagent_mem(cw, resources):
    cwagent_map = _ec2_instances_with_cwagent_mem_dims(cw, resources)
    if not cwagent_map:
        return
    queries = []
    id_map = {}
    for i, (resource, dims, cw_metric_name) in enumerate(cwagent_map.values()):
        qid = f"cwmem{i}"
        queries.append({
            "Id": qid,
            "MetricStat": {
                "Metric": {"Namespace": "CWAgent", "MetricName": cw_metric_name, "Dimensions": dims},
                "Period": 60,
                "Stat": "Average",
            },
            "ReturnData": True,
        })
        # DB metric_name stays "mem_used_percent" for Linux and Windows.
        id_map[qid] = (resource["id"], "mem_used_percent")
    n = _execute_gmd(cw, queries, id_map, minutes=polling_model.CWAGENT_MEM_LOOKBACK)
    logger.info(f"    EC2 CWAgent mem: {n} series / {len(cwagent_map)} of {len(resources)} instances")


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
        mounts = _cached_disk_dims(cw, r)
        if not mounts:
            continue
        instances_reporting += 1
        for dims, path, metric_name, cw_metric_name, invert in mounts:
            ensure_disk_mount_metric_registered(account_id, "ec2", metric_name, path)
            qid = f"cwdisk{len(queries)}"
            if invert:
                # Windows' LogicalDisk % Free Space is the INVERSE of
                # disk_used_percent -- compute (100 - free%) via a
                # CloudWatch metric-math expression so the stored value
                # keeps the same "higher = more full" meaning as every
                # existing Linux threshold/alert built on
                # disk_used_percent. See WINDOWS_DISK_METRIC_NAME in
                # app/collector/disk_mounts.py for the full writeup.
                raw_id = f"{qid}raw"
                queries.append({
                    "Id": raw_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "CWAgent",
                            "MetricName": cw_metric_name,
                            "Dimensions": dims,
                        },
                        "Period": 60,
                        "Stat": "Average",
                    },
                    "ReturnData": False,
                })
                queries.append({
                    "Id": qid,
                    "Expression": f"100 - {raw_id}",
                    "Label": metric_name,
                    "ReturnData": True,
                })
            else:
                queries.append({
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "CWAgent",
                            "MetricName": cw_metric_name,  # CW metric name never changes -- only our db metric_name is suffixed
                            "Dimensions": dims,
                        },
                        "Period": 60,
                        "Stat": "Average",
                    },
                    "ReturnData": True,
                })
            id_map[qid] = (r["id"], metric_name)

    n = _execute_gmd(cw, queries, id_map, minutes=polling_model.CWAGENT_DISK_LOOKBACK)
    logger.info(f"    EC2 CWAgent disk: {n} datapoints / {instances_reporting} of {len(resources)} instances (all mounts)")

# ── Resource fetcher ──────────────────────────────────────────

def _get_resources_for_account(account_id, tier):
    """
    Every tier: running EC2 only (stopped instances publish nothing -- the
    low tier used to include them), non-terminated everything else, and
    not-recently-seen (deleted) resources skipped. ECS is collected by the
    extended collector; ENI has no useful metrics.
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, resource_id, resource_type, name, region, tags
            FROM resources
            WHERE aws_account_id = %s
              AND (instance_state IS NULL OR instance_state != 'terminated')
              AND (last_seen_at IS NULL OR last_seen_at >= DATE_SUB(NOW(), INTERVAL %s HOUR))
              AND NOT (resource_type = 'ec2' AND COALESCE(instance_state, '') != 'running')
        """, (account_id, STALE_RESOURCE_HOURS))
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    grouped = {}
    for r in rows:
        if r["resource_type"] in ("ecs", "ecs_service", "eni"):
            continue
        key = (r["resource_type"], r["region"])
        grouped.setdefault(key, []).append(r)
    return grouped


# ── Per-account collection ────────────────────────────────────

_CORE_TYPES = ("ec2", "ebs", "rds", "elb", "lambda")


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

    tasks = []  # (callable, args)
    if tier in ("critical", "standard", "low"):
        grouped = _get_resources_for_account(account["id"], tier)
        for (resource_type, res_region), resources in grouped.items():
            if resource_type not in _CORE_TYPES:
                continue
            cw = session.client("cloudwatch", region_name=res_region, config=STANDARD_RETRY)
            tasks.append((_collect_core, (cw, resources, resource_type, tier)))
            if resource_type == "ec2":
                if tier == polling_model.CWAGENT_MEM_TIER:
                    tasks.append((_collect_ec2_cwagent_mem, (cw, resources)))
                if tier == polling_model.CWAGENT_DISK_TIER:
                    tasks.append((_collect_ec2_cwagent_disk, (cw, resources, account["id"])))

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(fn, *args) for fn, args in tasks]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                logger.error(f"Task error [{account['account_name']}]: {e}")

    # Extended-service metrics now have per-metric tiers (polling_model.
    # AWS_EXTENDED_TIER_OVERRIDES): 5-min incident signals ride "standard",
    # failure/state counters "low", the rest "extended"/"slow_extended".
    if tier in ("standard", "low", "extended", "slow_extended"):
        try:
            from app.collector.metrics.extended import collect_extended_for_account
            collect_extended_for_account(session, account, tier=tier)
        except Exception as e:
            logger.error(f"Extended collection error [{account['account_name']}]: {e}")

    conn   = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE aws_accounts SET last_synced_at = NOW() WHERE id = %s",
            (account["id"],)
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def run_metrics_collection(accounts, tier="standard"):
    """
    tier = 'critical' | 'standard' | 'low' | 'extended' | 'slow_extended'.
    What each tier collects is defined in app/collector/polling_model.py
    (AWS_CORE_METRICS for core services, aws_extended_tier() for the rest).
    """
    # Audit B14: scheduler.py hands over every ACTIVE account, including
    # Azure/GCP ones (no provider filter) -- those must never reach
    # get_boto3_session(), which falls back to the host's ambient AWS
    # credentials, nor have last_synced_at stamped by the AWS collector.
    global _CURRENT_TIER
    _CURRENT_TIER = tier
    accounts = [a for a in accounts if (a.get("provider") or "aws") == "aws"]
    if not accounts:
        return
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