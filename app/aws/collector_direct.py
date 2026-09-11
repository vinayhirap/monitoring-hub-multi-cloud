# app/aws/collector_direct.py
"""
Live data collector for frontend detail pages.

MIGRATION STATE (Phase 4a of removing VictoriaMetrics -- see
apply_dashboard_charts_metric_history.py):
  - EC2 / EBS / RDS / Lambda / ELB / ECS chart-detail series (the 6
    get_*/_get_*_metric_series functions) now read from the local
    metric_history table (written by Phase 1's GMD collector), not VM.
    Lambda/ELB/ECS keep their existing automatic boto3 fallback for
    metrics Phase 1 doesn't collect (ConcurrentExecutions, several ELB
    fields, all of ECS) -- see apply_dashboard_charts_metric_history.py's
    docstring for the one real gap this created (EBS burst_balance has
    no fallback and is now permanently empty).
  - LIST-view snapshot functions (_ec2_raw, _ebs_raw) now read from the
    `metrics` last-value cache too (Phase 4b, see
    apply_list_view_snapshots_metrics.py). check_and_write_alerts()
    (below) has its OWN separate, still-VM-dependent alerting logic --
    NOT part of Phase 4a/4b, found but deliberately not touched yet
    (needs its own investigation first -- see that script's docstring).
  - S3: still boto3-only, unrelated to VM either way.
  - EC2 StatusCheckFailed (used only by check_and_write_alerts, below)
    reads from the FREE Describe-API path (app/aws/describe_polling.py)
    instead of CloudWatch — zero GetMetricData cost, sub-second fresh.

Two GMD helpers (unchanged, still used for the boto3 fallback paths):
  _gmd_snapshot(cw, queries)  — latest single value per metric (for list views)
  _gmd_series(cw, queries)    — time-series arrays (for chart/detail views)
"""
import boto3, logging, time, math
from app.collector.disk_mounts import all_cwagent_disk_dims
from datetime import datetime, timedelta, timezone
# vm_client fully retired from THIS file (apply_final_cleanup.py): vm_query_all went in Phase 4b, vm_query's only use (StatusCheckFailed) is fixed by describe_polling.py now also writing locally. vm_client.py itself is NOT retired overall -- see that script's docstring for its one remaining legitimate use (ALB target-group health, external-Grafana-compatible, in app/aws/describe_polling.py).
from app.db import get_connection

logger = logging.getLogger(__name__)


def _metric_snapshot_query_all(resource_type, db_metric_name):
    """
    Drop-in replacement for vm_client.vm_query_all's role in the
    list-view snapshot functions below (_ec2_raw, _ebs_raw): every
    resource's CURRENT value in one query, keyed by resources.resource_id
    (bare instance_id/volume_id -- both list views only use resource_id-
    based matching, same convention Phase 4a confirmed and used).
    Reads the `metrics` last-value cache Phase 1's GMD collector already
    maintains -- no time range needed, this is a snapshot, not a series.
    Returns {} on any error -- same never-raises contract vm_query_all
    had. See apply_list_view_snapshots_metrics.py (Phase 4b).
    """
    out = {}
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                """SELECT r.resource_id, m.metric_value
                   FROM metrics m JOIN resources r ON r.id = m.resource_id
                   WHERE r.resource_type = %s AND m.metric_name = %s""",
                (resource_type, db_metric_name),
            )
            for row in cur.fetchall():
                if row["metric_value"] is not None:
                    out[row["resource_id"]] = float(row["metric_value"])
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.warning(f"metric snapshot query_all failed [{resource_type}/{db_metric_name}]: {e}")
    return out


def _metric_history_query_range(resource_type, identifier, db_metric_name,
                                 start_dt, end_dt, match_field="resource_id"):
    """
    Drop-in replacement for vm_client.vm_query_range's role in the 6
    chart-series functions below. Reads app/collector/metrics/runner.py's
    (Phase 1) local metric_history table instead of VictoriaMetrics.
    Returns the SAME shape vm_query_range did:
      [{"t": iso_timestamp, "v": rounded_float}, ...] oldest -> newest.
    Returns [] on no matching resource, no data in range, or any error --
    same never-raises, degrade-to-empty contract vm_query_range already
    had. match_field is always one of the two literal strings this file
    passes in below ("resource_id" or "name"), never user input.
    """
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                f"SELECT id FROM resources WHERE resource_type = %s AND {match_field} = %s LIMIT 1",
                (resource_type, identifier),
            )
            row = cur.fetchone()
            if not row:
                return []
            cur.execute(
                """SELECT metric_value, metric_timestamp FROM metric_history
                   WHERE resource_id = %s AND metric_name = %s
                         AND metric_timestamp BETWEEN %s AND %s
                   ORDER BY metric_timestamp""",
                (row["id"], db_metric_name, start_dt, end_dt),
            )
            rows = cur.fetchall()
        finally:
            cur.close()
            conn.close()
        return [
            {"t": r["metric_timestamp"].isoformat(), "v": round(float(r["metric_value"]), 2)}
            for r in rows if r["metric_value"] is not None
        ]
    except Exception as e:
        logger.warning(f"metric_history query_range failed [{resource_type}/{identifier}/{db_metric_name}]: {e}")
        return []

_cache: dict = {}
_CACHE_TTL   = 60

# Stopgap cache for Lambda metric-series boto3 fallback (see Mumbai GMD
# cost audit, Aug 2026): VM has no aws_lambda_* series yet, so every
# _get_lambda_metric_series() call falls through to a real, uncached
# boto3 GetMetricData batch. 5 min balances chart freshness against
# cost until YACE is actually scraping Lambda for this account.
_LAMBDA_SERIES_CACHE_TTL = 300


def _cached(key: str, fn, ttl: int = None):
    ttl = _CACHE_TTL if ttl is None else ttl
    now = time.time()
    if key in _cache and now - _cache[key]["ts"] < ttl:
        return _cache[key]["data"]
    result = fn()
    _cache[key] = {"data": result, "ts": now}
    return result


def get_session(region=None, role_arn=None, external_id=None):
    """
    Cross-account credentials for a SPECIFIC AWS account (role_arn
    given) come from a real STS AssumeRole (app.aws.sts.assume_role) —
    the same helper already used correctly by discovery and console
    federation. Without role_arn, falls back to ambient credentials
    (env vars / instance profile / shared config), unchanged from
    before — every OTHER caller of get_session in this file that
    doesn't pass role_arn is unaffected.

    Why this matters beyond correctness: before this, every collector
    below fell through to ambient-credential resolution regardless of
    which account was being queried. For a cross-account setup with
    no matching ambient credentials, that meant every AWS API call (7
    of them per account, in get_account_summary) had to exhaust
    botocore's full credential-provider chain — including an IMDS
    probe — before failing, on EVERY request. A single AssumeRole call
    resolves once and is reused for every client built from the
    returned session.
    """
    if role_arn:
        from app.aws.sts import assume_role
        base_session = assume_role(role_arn, external_id, session_name="mh-account-summary")
        creds = base_session.get_credentials().get_frozen_credentials()
        return boto3.Session(
            aws_access_key_id=creds.access_key,
            aws_secret_access_key=creds.secret_key,
            aws_session_token=creds.token,
            region_name=region,
        )
    return boto3.Session(region_name=region)


def _smart_period(hours: int) -> int:
    """CloudWatch max 1440 datapoints/request. Period must be multiple of 60."""
    period = math.ceil(hours * 3600 / 1440)
    period = max(60, period)
    return math.ceil(period / 60) * 60


# ── GMD core helpers (still used for ECS / Lambda / uncovered-ALB) ──────

def _make_query(qid, namespace, metric_name, dimensions, stat, period=60):
    return {
        "Id": qid,
        "MetricStat": {
            "Metric": {
                "Namespace":  namespace,
                "MetricName": metric_name,
                "Dimensions": dimensions,
            },
            "Period": period,
            "Stat":   stat,
        },
        "ReturnData": True,
    }


def _safe_qid(s: str) -> str:
    """
    AWS GetMetricData query IDs must match ^[a-z][a-zA-Z0-9_]*$ — no hyphens,
    no dots, must start with a lowercase letter. Resource IDs (i-xxxx,
    vol-xxxx, cluster/service names) commonly contain hyphens, so any qid
    built from a raw resource_id needs to go through this first.
    """
    import re
    s = re.sub(r'[^a-zA-Z0-9_]', '_', s)
    if not s or not s[0].islower():
        s = 'q' + s
    return s


def _gmd_snapshot(cw, queries, minutes=3):
    """
    Fetch latest single value for each query. Returns {query_id: float} --
    only for queries that actually returned a datapoint.

    Deliberately OMITS the key entirely when CloudWatch returns zero
    datapoints, rather than defaulting to 0.0. "No data" and "the value
    is 0" are different facts -- e.g. a gp3 EBS volume never publishes
    BurstBalance at all (it's a gp2-only credit-bucket metric; gp3 uses
    provisioned baseline performance instead), so CloudWatch always
    returns nothing for it. Silently treating that as 0.0 used to make
    _EVERY_ gp3 volume look like it had exhausted its (nonexistent)
    burst credits -- the worst possible reading for a "< threshold"
    comparison -- and check_and_write_alerts() below would breach and
    permanently alert on all of them (confirmed live, 2026-09-11: 21
    gp3 volumes, all CRITICAL, current_value=0/threshold=10, every one
    a false positive).

    Existing callers are unaffected: both consume this via
    `.get(key, 0.0)` for list-view display (still get 0.0 for a
    genuinely-missing key, same as before) except check_and_write_alerts,
    which iterates `.items()` and must NOT see a fabricated 0.0 for a
    resource/metric combo that was never really measured -- that's
    exactly the bug this fixes.
    """
    if not queries:
        return {}
    end   = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    out   = {}
    try:
        for i in range(0, len(queries), 500):
            chunk = queries[i:i + 500]
            resp  = cw.get_metric_data(
                MetricDataQueries=chunk,
                StartTime=start,
                EndTime=end,
                ScanBy="TimestampDescending",
            )
            for r in resp.get("MetricDataResults", []):
                vals = r.get("Values", [])
                if vals:
                    out[r["Id"]] = vals[0]
    except Exception as e:
        logger.error(f"GMD snapshot failed: {e}")
    return out


def _gmd_series(cw, queries, hours=6):
    """Fetch time-series arrays for each query. Returns {query_id: [{t,v},...]}."""
    if not queries:
        return {}
    end    = datetime.now(timezone.utc)
    start  = end - timedelta(hours=hours)
    period = _smart_period(hours)
    out    = {}

    adjusted = []
    for q in queries:
        q2 = dict(q)
        q2["MetricStat"] = dict(q["MetricStat"])
        q2["MetricStat"]["Period"] = period
        adjusted.append(q2)

    try:
        for i in range(0, len(adjusted), 500):
            chunk = adjusted[i:i + 500]
            resp  = cw.get_metric_data(
                MetricDataQueries=chunk,
                StartTime=start,
                EndTime=end,
                ScanBy="TimestampAscending",
            )
            for r in resp.get("MetricDataResults", []):
                timestamps = r.get("Timestamps", [])
                values     = r.get("Values", [])
                out[r["Id"]] = [
                    {"t": t.isoformat(), "v": round(v, 4)}
                    for t, v in zip(timestamps, values)
                ]
    except Exception as e:
        logger.error(f"GMD series failed: {e}")
    return out


# ── EC2 ───────────────────────────────────────────────────────────────

def collect_ec2_instances(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"ec2_{region}_{role_arn or 'self'}", lambda: _ec2_raw(region, role_arn, external_id))

def _ec2_raw(region, role_arn=None, external_id=None) -> list:
    try:
        ec2 = get_session(region, role_arn, external_id).client("ec2")
        instances = []
        for r in ec2.describe_instances()["Reservations"]:
            for inst in r["Instances"]:
                instances.append(inst)

        # One DB query per metric gets EVERY instance's current value at
        # once -- same "one call, not one per instance" shape the VM call
        # this replaces had, just against the local `metrics` cache now.
        cpu_map    = _metric_snapshot_query_all("ec2", "cpuutilization")
        netin_map  = _metric_snapshot_query_all("ec2", "networkin")
        netout_map = _metric_snapshot_query_all("ec2", "networkout")

        out = []
        for inst in instances:
            iid   = inst["InstanceId"]
            state = inst["State"]["Name"]
            tags  = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
            out.append({
                "instance_id":       iid,
                "name":              tags.get("Name", iid),
                "instance_type":     inst.get("InstanceType", ""),
                "state":             state,
                "region":            region,
                "availability_zone": inst.get("Placement", {}).get("AvailabilityZone", ""),
                "private_ip":        inst.get("PrivateIpAddress", "—"),
                "launch_time":       inst["LaunchTime"].isoformat() if inst.get("LaunchTime") else "",
                "cpu_utilization":   round(cpu_map.get(iid, 0.0), 2),
                "network_in_kb":     round(netin_map.get(iid, 0.0) / 1024, 2),
                "network_out_kb":    round(netout_map.get(iid, 0.0) / 1024, 2),
                "uptime_days":       _calc_uptime(inst.get("LaunchTime")),
                "tags":              tags,
            })
        running = [i for i in instances if i["State"]["Name"] == "running"]
        logger.info(f"EC2: {len(out)} in {region} ({len(running)} running, via metrics cache)")
        return out
    except Exception as e:
        logger.error(f"EC2 [{region}]: {e}"); return []


# ── EBS ───────────────────────────────────────────────────────────────

def collect_ebs_volumes(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"ebs_{region}_{role_arn or 'self'}", lambda: _ebs_raw(region, role_arn, external_id))

def _ebs_raw(region, role_arn=None, external_id=None) -> list:
    try:
        ec2  = get_session(region, role_arn, external_id).client("ec2")
        vols = ec2.describe_volumes().get("Volumes", [])

        read_ops_map  = _metric_snapshot_query_all("ebs", "volumereadops")
        write_ops_map = _metric_snapshot_query_all("ebs", "volumewriteops")
        read_b_map    = _metric_snapshot_query_all("ebs", "volumereadbytes")
        write_b_map   = _metric_snapshot_query_all("ebs", "volumewritebytes")
        queue_map     = _metric_snapshot_query_all("ebs", "volumequeuelength")
        # burst_balance: Phase 1's GMD collector never collects this
        # (dropped per its own triage note, "gp3 irrelevant") -- always
        # empty now, same documented gap as the EBS chart-detail page
        # (Phase 4a). Unlike that page, this list view's burst_balance
        # column has always defaulted to 0.0 via .get(vid, 0.0) below
        # rather than showing "no data", so the visible behavior here is
        # unchanged either way -- just always 0.0 now instead of
        # sometimes-VM-sometimes-0.0.
        burst_map     = _metric_snapshot_query_all("ebs", "volumeburstbalance")

        out = []
        for v in vols:
            vid         = v["VolumeId"]
            tags        = {t["Key"]: t["Value"] for t in v.get("Tags", [])}
            attachments = v.get("Attachments", [])
            attached_to = attachments[0].get("InstanceId", "") if attachments else ""
            out.append({
                "volume_id":         vid,
                "name":              tags.get("Name", vid),
                "state":             v.get("State", ""),
                "size_gb":           v.get("Size", 0),
                "volume_type":       v.get("VolumeType", ""),
                "iops":              v.get("Iops"),
                "throughput":        v.get("Throughput"),
                "encrypted":         v.get("Encrypted", False),
                "availability_zone": v.get("AvailabilityZone", ""),
                "attached_to":       attached_to,
                "create_time":       v["CreateTime"].isoformat() if v.get("CreateTime") else "",
                "region":            region,
                "tags":              tags,
                "read_ops":          round(read_ops_map.get(vid,  0.0), 2),
                "write_ops":         round(write_ops_map.get(vid, 0.0), 2),
                "read_bytes_kb":     round(read_b_map.get(vid,    0.0) / 1024, 2),
                "write_bytes_kb":    round(write_b_map.get(vid,   0.0) / 1024, 2),
                "queue_length":      round(queue_map.get(vid,     0.0), 4),
                "burst_balance":     round(burst_map.get(vid, 0.0), 2),
            })
        return out
    except Exception as e:
        logger.error(f"EBS [{region}]: {e}"); return []


# ── RDS (discovery only — no CW metrics fetched here) ───────────────────

def collect_rds_instances(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"rds_{region}_{role_arn or 'self'}", lambda: _rds_raw(region, role_arn, external_id))

def _rds_raw(region, role_arn=None, external_id=None) -> list:
    try:
        rds = get_session(region, role_arn, external_id).client("rds")
        out = []
        for db in rds.describe_db_instances()["DBInstances"]:
            out.append({
                "db_instance_id":    db["DBInstanceIdentifier"],
                "identifier":        db["DBInstanceIdentifier"],
                "engine":            db.get("Engine", ""),
                "engine_version":    db.get("EngineVersion", ""),
                "instance_class":    db.get("DBInstanceClass", ""),
                "status":            db.get("DBInstanceStatus", ""),
                "region":            region,
                "multi_az":          db.get("MultiAZ", False),
                "allocated_storage": db.get("AllocatedStorage"),
                "endpoint":          db.get("Endpoint", {}).get("Address", ""),
            })
        return out
    except Exception as e:
        logger.error(f"RDS [{region}]: {e}"); return []


# ── S3 (unchanged — not in YACE config) ─────────────────────────────────

def collect_s3_buckets(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"s3_global_{role_arn or 'self'}", lambda: _s3_raw(role_arn, external_id))

def _s3_bucket_detail(s3, b) -> dict:
    """
    The 3 per-bucket detail calls (location/versioning/public-access
    block) for ONE bucket -- called concurrently, once per bucket, by
    _s3_raw below. Same fallback-to-default behavior as before: any
    individual call failing (e.g. a permissions gap on just that one
    API) still returns the bucket with its other fields populated.
    """
    name          = b["Name"]
    bucket_region = "us-east-1"
    versioning    = "Disabled"
    public_access = False
    try:
        loc = s3.get_bucket_location(Bucket=name)
        bucket_region = loc.get("LocationConstraint") or "us-east-1"
    except Exception: pass
    try:
        v = s3.get_bucket_versioning(Bucket=name)
        versioning = v.get("Status", "Disabled") or "Disabled"
    except Exception: pass
    try:
        cfg = s3.get_public_access_block(Bucket=name).get("PublicAccessBlockConfiguration", {})
        public_access = not all([
            cfg.get("BlockPublicAcls",      True),
            cfg.get("BlockPublicPolicy",     True),
            cfg.get("RestrictPublicBuckets", True),
        ])
    except Exception: pass
    cd = b.get("CreationDate", "")
    return {
        "bucket_name":   name,
        "name":          name,
        "region":        bucket_region,
        "creation_date": cd.isoformat() if hasattr(cd, "isoformat") else str(cd),
        "versioning":    versioning,
        "public_access": public_access,
        "object_count":  None,
        "size_bytes":    None,
    }


def _s3_raw(role_arn=None, external_id=None) -> list:
    """
    Lists buckets, then fetches each bucket's location/versioning/
    public-access-block details CONCURRENTLY (one worker per bucket,
    capped at 20 in flight) instead of one bucket at a time. For N
    buckets that were previously 3*N sequential AWS calls, this is
    now roughly "as long as the single slowest bucket's 3 calls take"
    -- for 45 buckets, ~35s down to ~1-2s in practice. Result content
    and per-call failure handling are unchanged from before.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    try:
        s3      = get_session(None, role_arn, external_id).client("s3")
        buckets = s3.list_buckets().get("Buckets", [])
        out     = []
        with ThreadPoolExecutor(max_workers=min(len(buckets), 20) or 1) as ex:
            futures = [ex.submit(_s3_bucket_detail, s3, b) for b in buckets]
            for f in as_completed(futures):
                try:
                    out.append(f.result())
                except Exception as e:
                    logger.error(f"S3 bucket detail error: {e}")
        logger.info(f"S3: {len(out)} buckets")
        return out
    except Exception as e:
        logger.error(f"S3: {e}"); return []


# ── S3 metric series (unchanged — not in YACE config) ────────────────────

def get_s3_metric_series(bucket_name: str, hours: int = 24) -> dict:
    try:
        cw            = boto3.client("cloudwatch", region_name="us-east-1")
        end           = datetime.now(timezone.utc)
        effective_hrs = max(hours, 24 * 14)
        start         = end - timedelta(hours=effective_hrs)
        period        = max(_smart_period(effective_hrs), 86400)

        def storage_series(metric, storage_type="StandardStorage"):
            dims = [
                {"Name": "BucketName",  "Value": bucket_name},
                {"Name": "StorageType", "Value": storage_type},
            ]
            from botocore.exceptions import ClientError
            try:
                r = cw.get_metric_statistics(
                    Namespace="AWS/S3", MetricName=metric, Dimensions=dims,
                    StartTime=start, EndTime=end, Period=period,
                    Statistics=["Average"],
                )
                return sorted(
                    [{"t": p["Timestamp"].isoformat(), "v": round(p["Average"], 2)} for p in r["Datapoints"]],
                    key=lambda x: x["t"]
                )
            except ClientError:
                return []

        def request_series(metric):
            dims = [
                {"Name": "BucketName", "Value": bucket_name},
                {"Name": "FilterId",   "Value": "EntireBucket"},
            ]
            try:
                r = cw.get_metric_statistics(
                    Namespace="AWS/S3", MetricName=metric, Dimensions=dims,
                    StartTime=end - timedelta(hours=min(hours, 168)),
                    EndTime=end, Period=max(_smart_period(hours), 300),
                    Statistics=["Sum"],
                )
                return sorted(
                    [{"t": p["Timestamp"].isoformat(), "v": round(p["Sum"], 2)} for p in r["Datapoints"]],
                    key=lambda x: x["t"]
                )
            except Exception:
                return []

        return {
            "bucket_name":    bucket_name,
            "bucket_size":    storage_series("BucketSizeBytes", "StandardStorage"),
            "object_count":   storage_series("NumberOfObjects", "AllStorageTypes"),
            "all_requests":   request_series("AllRequests"),
            "get_requests":   request_series("GetRequests"),
            "put_requests":   request_series("PutRequests"),
            "errors_4xx":     request_series("4xxErrors"),
            "errors_5xx":     request_series("5xxErrors"),
            "bytes_download": request_series("BytesDownloaded"),
            "bytes_upload":   request_series("BytesUploaded"),
            "period_hours":   hours,
            "note": "Storage metrics: daily. Request metrics require per-bucket CW config.",
        }
    except Exception as e:
        logger.error(f"S3 metrics [{bucket_name}]: {e}")
        return {"bucket_name": bucket_name, "bucket_size": [], "object_count": [],
                "all_requests": [], "get_requests": [], "put_requests": [],
                "errors_4xx": [], "errors_5xx": [], "bytes_download": [],
                "bytes_upload": [], "period_hours": hours, "note": str(e)}


# ── ELB (discovery only — no CW metrics fetched here) ────────────────────

def collect_elb(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"elb_{region}_{role_arn or 'self'}", lambda: _elb_raw(region, role_arn, external_id))

def _elb_raw(region, role_arn=None, external_id=None) -> list:
    try:
        elb = get_session(region, role_arn, external_id).client("elbv2")
        out = []
        for lb in elb.describe_load_balancers().get("LoadBalancers", []):
            ct = lb.get("CreatedTime", "")
            out.append({
                "name":               lb.get("LoadBalancerName", ""),
                "load_balancer_arn":  lb.get("LoadBalancerArn", ""),
                "dns_name":           lb.get("DNSName", ""),
                "type":               lb.get("Type", ""),
                "scheme":             lb.get("Scheme", ""),
                "state":              lb.get("State", {}).get("Code", ""),
                "region":             region,
                "availability_zones": [az["ZoneName"] for az in lb.get("AvailabilityZones", [])],
                "created_time":       ct.isoformat() if hasattr(ct, "isoformat") else str(ct),
            })
        logger.info(f"ELB: {len(out)} in {region}")
        return out
    except Exception as e:
        logger.error(f"ELB [{region}]: {e}"); return []


# ── Extended services (lightweight discovery only, no CW metrics) ───────
# One cheap describe/list call each, used ONLY to answer "does this
# account have any resources of this type right now" for the Services
# page tile filter (see app/api/live_data.py resource-counts endpoint).
# Cached the same way as the core collectors above.

def collect_nlb(region=None) -> list:
    return _cached(f"nlb_{region}", lambda: _nlb_raw(region))

def _nlb_raw(region) -> list:
    try:
        elb = get_session(region).client("elbv2")
        out = [lb for lb in elb.describe_load_balancers().get("LoadBalancers", [])
               if lb.get("Type") == "network"]
        logger.info(f"NLB: {len(out)} in {region}")
        return out
    except Exception as e:
        logger.error(f"NLB [{region}]: {e}"); return []


def collect_acm_certificates(region=None) -> list:
    return _cached(f"acm_{region}", lambda: _acm_raw(region))

def _acm_raw(region) -> list:
    try:
        acm = get_session(region).client("acm")
        out = []
        for page in acm.get_paginator("list_certificates").paginate():
            out.extend(page.get("CertificateSummaryList", []))
        logger.info(f"ACM: {len(out)} certificates in {region}")
        return out
    except Exception as e:
        logger.error(f"ACM [{region}]: {e}"); return []


def collect_backup_resources(region=None) -> list:
    return _cached(f"backup_{region}", lambda: _backup_raw(region))

def _backup_raw(region) -> list:
    try:
        backup = get_session(region).client("backup")
        out = []
        for page in backup.get_paginator("list_protected_resources").paginate():
            out.extend(page.get("Results", []))
        logger.info(f"Backup: {len(out)} protected resources in {region}")
        return out
    except Exception as e:
        logger.error(f"Backup [{region}]: {e}"); return []


def collect_dms_instances(region=None) -> list:
    return _cached(f"dms_{region}", lambda: _dms_raw(region))

def _dms_raw(region) -> list:
    try:
        dms = get_session(region).client("dms")
        out = []
        for page in dms.get_paginator("describe_replication_instances").paginate():
            out.extend(page.get("ReplicationInstances", []))
        logger.info(f"DMS: {len(out)} replication instances in {region}")
        return out
    except Exception as e:
        logger.error(f"DMS [{region}]: {e}"); return []


def collect_direct_connections(region=None) -> list:
    return _cached(f"directconnect_{region}", lambda: _directconnect_raw(region))

def _directconnect_raw(region) -> list:
    try:
        dx = get_session(region).client("directconnect")
        out = dx.describe_connections().get("connections", [])
        logger.info(f"Direct Connect: {len(out)} connections in {region}")
        return out
    except Exception as e:
        logger.error(f"Direct Connect [{region}]: {e}"); return []


def collect_state_machines(region=None) -> list:
    return _cached(f"states_{region}", lambda: _states_raw(region))

def _states_raw(region) -> list:
    try:
        sfn = get_session(region).client("stepfunctions")
        out = []
        for page in sfn.get_paginator("list_state_machines").paginate():
            out.extend(page.get("stateMachines", []))
        logger.info(f"Step Functions: {len(out)} state machines in {region}")
        return out
    except Exception as e:
        logger.error(f"Step Functions [{region}]: {e}"); return []


# ── Directory-tier services (lightweight discovery only) ────────────────
# One cheap list/describe call each, same purpose as the extended-service
# collectors above: answer "does this account have any of these right
# now" for the Services page tile filter. Nothing here fetches metrics.

def collect_apigateway(region=None) -> list:
    return _cached(f"apigateway_{region}", lambda: _apigateway_raw(region))

def _apigateway_raw(region) -> list:
    try:
        s = get_session(region)
        rest = s.client("apigateway").get_paginator("get_rest_apis")
        rest_items = [i for p in rest.paginate() for i in p.get("items", [])]
        http_items = []
        try:
            v2 = s.client("apigatewayv2").get_paginator("get_apis")
            http_items = [i for p in v2.paginate() for i in p.get("Items", [])]
        except Exception:
            pass
        return rest_items + http_items
    except Exception as e:
        logger.error(f"API Gateway [{region}]: {e}"); return []


def collect_dynamodb_tables(region=None) -> list:
    return _cached(f"dynamodb_{region}", lambda: _dynamodb_raw(region))

def _dynamodb_raw(region) -> list:
    try:
        ddb = get_session(region).client("dynamodb")
        out = []
        for page in ddb.get_paginator("list_tables").paginate():
            out.extend(page.get("TableNames", []))
        return out
    except Exception as e:
        logger.error(f"DynamoDB [{region}]: {e}"); return []


def collect_sqs_queues(region=None) -> list:
    return _cached(f"sqs_{region}", lambda: _sqs_raw(region))

def _sqs_raw(region) -> list:
    try:
        sqs = get_session(region).client("sqs")
        out = []
        for page in sqs.get_paginator("list_queues").paginate():
            out.extend(page.get("QueueUrls", []))
        return out
    except Exception as e:
        logger.error(f"SQS [{region}]: {e}"); return []


def collect_sns_topics(region=None) -> list:
    return _cached(f"sns_{region}", lambda: _sns_raw(region))

def _sns_raw(region) -> list:
    try:
        sns = get_session(region).client("sns")
        out = []
        for page in sns.get_paginator("list_topics").paginate():
            out.extend(page.get("Topics", []))
        return out
    except Exception as e:
        logger.error(f"SNS [{region}]: {e}"); return []


def collect_cloudfront_distributions(region=None) -> list:
    # Global service — region argument intentionally unused.
    return _cached("cloudfront", _cloudfront_raw)

def _cloudfront_raw() -> list:
    try:
        cf = boto3.client("cloudfront")
        resp = cf.list_distributions()
        return resp.get("DistributionList", {}).get("Items", [])
    except Exception as e:
        logger.error(f"CloudFront: {e}"); return []


def collect_elasticache_clusters(region=None) -> list:
    return _cached(f"elasticache_{region}", lambda: _elasticache_raw(region))

def _elasticache_raw(region) -> list:
    try:
        ec = get_session(region).client("elasticache")
        out = []
        for page in ec.get_paginator("describe_cache_clusters").paginate():
            out.extend(page.get("CacheClusters", []))
        return out
    except Exception as e:
        logger.error(f"ElastiCache [{region}]: {e}"); return []


def collect_opensearch_domains(region=None) -> list:
    return _cached(f"opensearch_{region}", lambda: _opensearch_raw(region))

def _opensearch_raw(region) -> list:
    try:
        es = get_session(region).client("opensearch")
        return es.list_domain_names().get("DomainNames", [])
    except Exception as e:
        logger.error(f"OpenSearch [{region}]: {e}"); return []


def collect_eks_clusters(region=None) -> list:
    return _cached(f"eks_{region}", lambda: _eks_raw(region))

def _eks_raw(region) -> list:
    try:
        eks = get_session(region).client("eks")
        out = []
        for page in eks.get_paginator("list_clusters").paginate():
            out.extend(page.get("clusters", []))
        return out
    except Exception as e:
        logger.error(f"EKS [{region}]: {e}"); return []


def collect_efs_filesystems(region=None) -> list:
    return _cached(f"efs_{region}", lambda: _efs_raw(region))

def _efs_raw(region) -> list:
    try:
        efs = get_session(region).client("efs")
        out = []
        for page in efs.get_paginator("describe_file_systems").paginate():
            out.extend(page.get("FileSystems", []))
        return out
    except Exception as e:
        logger.error(f"EFS [{region}]: {e}"); return []


def collect_documentdb_clusters(region=None) -> list:
    return _cached(f"documentdb_{region}", lambda: _documentdb_raw(region))

def _documentdb_raw(region) -> list:
    try:
        docdb = get_session(region).client("docdb")
        out = []
        for page in docdb.get_paginator("describe_db_clusters").paginate():
            out.extend(page.get("DBClusters", []))
        return out
    except Exception as e:
        logger.error(f"DocumentDB [{region}]: {e}"); return []


def collect_neptune_clusters(region=None) -> list:
    return _cached(f"neptune_{region}", lambda: _neptune_raw(region))

def _neptune_raw(region) -> list:
    try:
        neptune = get_session(region).client("neptune")
        out = []
        for page in neptune.get_paginator("describe_db_clusters").paginate():
            out.extend(page.get("DBClusters", []))
        return out
    except Exception as e:
        logger.error(f"Neptune [{region}]: {e}"); return []


def collect_msk_clusters(region=None) -> list:
    return _cached(f"msk_{region}", lambda: _msk_raw(region))

def _msk_raw(region) -> list:
    try:
        kafka = get_session(region).client("kafka")
        out = []
        for page in kafka.get_paginator("list_clusters_v2").paginate():
            out.extend(page.get("ClusterInfoList", []))
        return out
    except Exception as e:
        logger.error(f"MSK [{region}]: {e}"); return []


def collect_kinesis_streams(region=None) -> list:
    return _cached(f"kinesis_{region}", lambda: _kinesis_raw(region))

def _kinesis_raw(region) -> list:
    try:
        k = get_session(region).client("kinesis")
        out = []
        for page in k.get_paginator("list_streams").paginate():
            out.extend(page.get("StreamNames", []))
        return out
    except Exception as e:
        logger.error(f"Kinesis [{region}]: {e}"); return []


def collect_firehose_streams(region=None) -> list:
    return _cached(f"firehose_{region}", lambda: _firehose_raw(region))

def _firehose_raw(region) -> list:
    try:
        fh = get_session(region).client("firehose")
        return fh.list_delivery_streams().get("DeliveryStreamNames", [])
    except Exception as e:
        logger.error(f"Firehose [{region}]: {e}"); return []


def collect_autoscaling_groups(region=None) -> list:
    return _cached(f"autoscaling_{region}", lambda: _autoscaling_raw(region))

def _autoscaling_raw(region) -> list:
    try:
        asg = get_session(region).client("autoscaling")
        out = []
        for page in asg.get_paginator("describe_auto_scaling_groups").paginate():
            out.extend(page.get("AutoScalingGroups", []))
        return out
    except Exception as e:
        logger.error(f"Auto Scaling [{region}]: {e}"); return []


def collect_nat_gateways(region=None) -> list:
    return _cached(f"natgateway_{region}", lambda: _natgateway_raw(region))

def _natgateway_raw(region) -> list:
    try:
        ec2 = get_session(region).client("ec2")
        out = []
        for page in ec2.get_paginator("describe_nat_gateways").paginate():
            out.extend([n for n in page.get("NatGateways", []) if n.get("State") != "deleted"])
        return out
    except Exception as e:
        logger.error(f"NAT Gateway [{region}]: {e}"); return []


def collect_transit_gateways(region=None) -> list:
    return _cached(f"transitgateway_{region}", lambda: _transitgateway_raw(region))

def _transitgateway_raw(region) -> list:
    try:
        ec2 = get_session(region).client("ec2")
        out = []
        for page in ec2.get_paginator("describe_transit_gateways").paginate():
            out.extend([t for t in page.get("TransitGateways", []) if t.get("State") != "deleted"])
        return out
    except Exception as e:
        logger.error(f"Transit Gateway [{region}]: {e}"); return []


def collect_route53_zones(region=None) -> list:
    # Global service — region argument intentionally unused.
    return _cached("route53", _route53_raw)

def _route53_raw() -> list:
    try:
        r53 = boto3.client("route53")
        out = []
        for page in r53.get_paginator("list_hosted_zones").paginate():
            out.extend(page.get("HostedZones", []))
        return out
    except Exception as e:
        logger.error(f"Route 53: {e}"); return []


def collect_waf_web_acls(region=None) -> list:
    return _cached(f"wafv2_{region}", lambda: _waf_raw(region))

def _waf_raw(region) -> list:
    try:
        waf = get_session(region).client("wafv2")
        return waf.list_web_acls(Scope="REGIONAL").get("WebACLs", [])
    except Exception as e:
        logger.error(f"WAF [{region}]: {e}"); return []


def collect_redshift_clusters(region=None) -> list:
    return _cached(f"redshift_{region}", lambda: _redshift_raw(region))

def _redshift_raw(region) -> list:
    try:
        rs = get_session(region).client("redshift")
        out = []
        for page in rs.get_paginator("describe_clusters").paginate():
            out.extend(page.get("Clusters", []))
        return out
    except Exception as e:
        logger.error(f"Redshift [{region}]: {e}"); return []


def collect_memorydb_clusters(region=None) -> list:
    return _cached(f"memorydb_{region}", lambda: _memorydb_raw(region))

def _memorydb_raw(region) -> list:
    try:
        mdb = get_session(region).client("memorydb")
        return mdb.describe_clusters().get("Clusters", [])
    except Exception as e:
        logger.error(f"MemoryDB [{region}]: {e}"); return []


def collect_dax_clusters(region=None) -> list:
    return _cached(f"dax_{region}", lambda: _dax_raw(region))

def _dax_raw(region) -> list:
    try:
        dax = get_session(region).client("dax")
        return dax.describe_clusters().get("Clusters", [])
    except Exception as e:
        logger.error(f"DAX [{region}]: {e}"); return []


def collect_eventbridge_rules(region=None) -> list:
    return _cached(f"events_{region}", lambda: _eventbridge_raw(region))

def _eventbridge_raw(region) -> list:
    try:
        ev = get_session(region).client("events")
        out = []
        for page in ev.get_paginator("list_rules").paginate():
            out.extend(page.get("Rules", []))
        return out
    except Exception as e:
        logger.error(f"EventBridge [{region}]: {e}"); return []


def collect_kms_keys(region=None) -> list:
    return _cached(f"kms_{region}", lambda: _kms_raw(region))

def _kms_raw(region) -> list:
    try:
        kms = get_session(region).client("kms")
        out = []
        for page in kms.get_paginator("list_keys").paginate():
            out.extend(page.get("Keys", []))
        return out
    except Exception as e:
        logger.error(f"KMS [{region}]: {e}"); return []


def collect_cloudwatch_log_groups(region=None) -> list:
    return _cached(f"logs_{region}", lambda: _logs_raw(region))

def _logs_raw(region) -> list:
    try:
        logs_client = get_session(region).client("logs")
        out = []
        for page in logs_client.get_paginator("describe_log_groups").paginate():
            out.extend(page.get("logGroups", []))
        return out
    except Exception as e:
        logger.error(f"CloudWatch Logs [{region}]: {e}"); return []


def collect_vpn_connections(region=None) -> list:
    return _cached(f"vpn_{region}", lambda: _vpn_raw(region))

def _vpn_raw(region) -> list:
    try:
        ec2 = get_session(region).client("ec2")
        conns = ec2.describe_vpn_connections().get("VpnConnections", [])
        return [c for c in conns if c.get("State") not in ("deleted", "deleting")]
    except Exception as e:
        logger.error(f"Site-to-Site VPN [{region}]: {e}"); return []


def collect_cognito_user_pools(region=None) -> list:
    return _cached(f"cognito_{region}", lambda: _cognito_raw(region))

def _cognito_raw(region) -> list:
    try:
        idp = get_session(region).client("cognito-idp")
        out = []
        for page in idp.get_paginator("list_user_pools").paginate(PaginationConfig={"PageSize": 60}):
            out.extend(page.get("UserPools", []))
        return out
    except Exception as e:
        logger.error(f"Cognito [{region}]: {e}"); return []


def collect_global_accelerator_accelerators(region=None) -> list:
    # Global service — Global Accelerator's control-plane API is only
    # available in us-west-2 regardless of the account's default region.
    return _cached("globalaccelerator", _global_accelerator_raw)

def _global_accelerator_raw() -> list:
    try:
        ga = boto3.client("globalaccelerator", region_name="us-west-2")
        out = []
        for page in ga.get_paginator("list_accelerators").paginate():
            out.extend(page.get("Accelerators", []))
        return out
    except Exception as e:
        logger.error(f"Global Accelerator: {e}"); return []


# ── ECS (unchanged — not in YACE config) ─────────────────────────────────

def collect_ecs_clusters(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"ecs_{region}_{role_arn or 'self'}", lambda: _ecs_raw(region, role_arn, external_id))

def _ecs_raw(region, role_arn=None, external_id=None) -> list:
    try:
        session = get_session(region, role_arn, external_id)
        ecs = session.client("ecs")
        cw  = session.client("cloudwatch")
        cluster_arns = ecs.list_clusters().get("clusterArns", [])
        if not cluster_arns:
            return []
        clusters = ecs.describe_clusters(clusters=cluster_arns, include=["STATISTICS"]).get("clusters", [])

        queries  = []
        qid_map  = {}
        svc_data = {}

        for c in clusters:
            cname    = c["clusterName"]
            svc_arns = ecs.list_services(cluster=cname).get("serviceArns", [])
            svcs     = []
            if svc_arns:
                svcs = ecs.describe_services(cluster=cname, services=svc_arns[:10]).get("services", [])
            svc_data[cname] = svcs

            for s in svcs:
                sname = s["serviceName"]
                dims  = [
                    {"Name": "ClusterName", "Value": cname},
                    {"Name": "ServiceName", "Value": sname},
                ]
                for metric, key in [("CPUUtilization", "cpu"), ("MemoryUtilization", "mem")]:
                    qid = _safe_qid(f"{cname}__{sname}__{key}")
                    queries.append(_make_query(qid, "AWS/ECS", metric, dims, "Average"))
                    qid_map[qid] = (cname, sname, key)

        snap = _gmd_snapshot(cw, queries, minutes=6)
        metrics = {}
        for qid, val in snap.items():
            cname, sname, key = qid_map[qid]
            metrics.setdefault((cname, sname), {})[key] = val

        out = []
        for c in clusters:
            cname    = c["clusterName"]
            services = []
            for s in svc_data.get(cname, []):
                sname = s["serviceName"]
                m     = metrics.get((cname, sname), {})
                services.append({
                    "service_name":    sname,
                    "service_arn":     s["serviceArn"],
                    "status":          s.get("status", ""),
                    "desired_count":   s.get("desiredCount", 0),
                    "running_count":   s.get("runningCount", 0),
                    "pending_count":   s.get("pendingCount", 0),
                    "task_definition": s.get("taskDefinition", "").split("/")[-1],
                    "launch_type":     s.get("launchType", "FARGATE"),
                    "cpu_utilization": round(m.get("cpu", 0.0), 2),
                    "mem_utilization": round(m.get("mem", 0.0), 2),
                })
            out.append({
                "cluster_name":         cname,
                "cluster_arn":          c["clusterArn"],
                "status":               c.get("status", ""),
                "registered_instances": c.get("registeredContainerInstancesCount", 0),
                "running_tasks":        c.get("runningTasksCount", 0),
                "pending_tasks":        c.get("pendingTasksCount", 0),
                "active_services":      c.get("activeServicesCount", 0),
                "region":               region,
                "services":             services,
            })
        logger.info(f"ECS: {len(out)} clusters in {region} (1 GMD call)")
        return out
    except Exception as e:
        logger.warning(f"ECS [{region}]: {e}"); return []


# ── Lambda (unchanged — not in YACE config) ──────────────────────────────

def collect_lambda_functions(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"lambda_{region}_{role_arn or 'self'}", lambda: _lambda_raw(region, role_arn, external_id))

def _lambda_raw(region, role_arn=None, external_id=None) -> list:
    try:
        lmb = get_session(region, role_arn, external_id).client("lambda")
        out = []
        for page in lmb.get_paginator("list_functions").paginate():
            for fn in page["Functions"]:
                out.append({
                    "function_name": fn["FunctionName"],
                    "function_arn":  fn.get("FunctionArn", ""),
                    "runtime":       fn.get("Runtime", ""),
                    "memory_size":   fn.get("MemorySize", 0),
                    "timeout":       fn.get("Timeout", 0),
                    "last_modified": fn.get("LastModified", ""),
                    "code_size":     fn.get("CodeSize"),
                    "region":        region,
                })
        return out
    except Exception as e:
        logger.warning(f"Lambda [{region}]: {e}"); return []


# ── Metric series — EC2 (now VM-backed) ──────────────────────────────────

def _ec2_cwagent_installed(instance_id, region=None) -> bool:
    """
    True iff the CWAgent CloudWatch namespace has ANY metric published
    for this instance — the only reliable signal that the CloudWatch
    Agent is actually installed AND reporting (an SSM association only
    proves an install was *attempted*, not that the agent process is
    running and publishing). Cached briefly since this runs on every
    EC2 detail-page open.
    """
    return _cached(
        f"cwagent_present_{instance_id}_{region}",
        lambda: _ec2_cwagent_installed_raw(instance_id, region),
        ttl=300,
    )


def _ec2_cwagent_installed_raw(instance_id, region=None) -> bool:
    try:
        cw = boto3.client("cloudwatch", region_name=region)
        resp = cw.list_metrics(
            Namespace="CWAgent",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        )
        return bool(resp.get("Metrics"))
    except Exception as e:
        logger.warning(f"CWAgent presence check [{instance_id}]: {e}")
        return False


def _ec2_cwagent_dimensions(cw, metric_name, instance_id):
    """
    Find the exact dimension set CWAgent published `metric_name` under
    for this instance. Used for mem_used_percent, which CWAgent
    dimensions by InstanceId alone (or close to it) -- GetMetricData
    needs the COMPLETE dimension set a datapoint was actually published
    under; a partial match (InstanceId only) returns nothing.
    disk_used_percent is NOT routed through this function -- it's
    multi-mount (one series per path) and goes through
    app/collector/disk_mounts.py's all_cwagent_disk_dims() instead,
    which returns every mount rather than picking one.
    """
    try:
        resp = cw.list_metrics(
            Namespace="CWAgent", MetricName=metric_name,
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        )
        metrics = resp.get("Metrics", [])
        return metrics[0]["Dimensions"] if metrics else None
    except Exception as e:
        logger.warning(f"CWAgent dimension lookup [{instance_id}/{metric_name}]: {e}")
        return None


def get_ec2_metric_series(instance_id, region=None, hours=6) -> dict:
    try:
        end    = datetime.now(timezone.utc)
        start  = end - timedelta(hours=hours)
        period = _smart_period(hours)
        dim    = f'dimension_InstanceId="{instance_id}"'

        def s(db_metric_name):
            return _metric_history_query_range("ec2", instance_id, db_metric_name, start, end)

        cwagent_installed = _ec2_cwagent_installed(instance_id, region)

        # Memory/disk-space utilization ONLY exist if the CloudWatch
        # Agent is installed and reporting — EC2 never publishes these
        # from the hypervisor side the way CPU/Network/StatusCheck are.
        # Skip the GetMetricData calls entirely when the agent isn't
        # present (zero extra cost) rather than making them and
        # getting empty series back — the frontend uses
        # cwagent_installed to decide whether to render the chart
        # boxes at all, not just whether they have data.
        mem_utilization        = []
        disk_used_percent      = []
        disk_used_percent_by_mount = {}  # path -> series, ALL mounts (new -- see app/collector/disk_mounts.py)
        if cwagent_installed:
            try:
                cw        = boto3.client("cloudwatch", region_name=region)
                cw_period = max(period, 60)  # CWAgent's own default reporting interval

                mem_dims = _ec2_cwagent_dimensions(cw, "mem_used_percent", instance_id)
                mounts   = all_cwagent_disk_dims(cw, instance_id)  # [(dims, path, metric_name), ...] -- every mount

                queries = []
                if mem_dims:
                    queries.append(_make_query("mem", "CWAgent", "mem_used_percent", mem_dims, "Average", cw_period))
                for dims, path, metric_name in mounts:
                    queries.append(_make_query(f"disk_{metric_name}", "CWAgent", "disk_used_percent", dims, "Average", cw_period))

                if queries:
                    fb = _gmd_series(cw, queries, hours)
                    mem_utilization = fb.get("mem", [])
                    for dims, path, metric_name in mounts:
                        series = fb.get(f"disk_{metric_name}", [])
                        disk_used_percent_by_mount[path] = series
                        if path in ("/", "C:"):
                            disk_used_percent = series  # unchanged key, root-preferred -- backward compatible
                    if disk_used_percent == [] and disk_used_percent_by_mount:
                        # no root mount reporting -- fall back to whichever CloudWatch returned first,
                        # same fallback behavior the old single-mount picker had
                        disk_used_percent = next(iter(disk_used_percent_by_mount.values()))
            except Exception as e:
                logger.warning(f"CWAgent series [{instance_id}]: {e}")

        return {
            "instance_id":               instance_id,
            "cpu":                       s("cpuutilization"),
            "network_in":                s("networkin"),
            "network_out":               s("networkout"),
            "disk_read":                 s("diskreadbytes"),
            "disk_write":                s("diskwritebytes"),
            "cwagent_installed":         cwagent_installed,
            "mem_utilization":           mem_utilization,
            "disk_used_percent":         disk_used_percent,
            "disk_used_percent_by_mount": disk_used_percent_by_mount,
            "period_hours":              hours,
            "period_secs":               period,
        }
    except Exception as e:
        logger.warning(f"EC2 series [{instance_id}]: {e}")
        return {"instance_id": instance_id, "cpu": [], "network_in": [],
                "network_out": [], "disk_read": [], "disk_write": [],
                "cwagent_installed": False, "mem_utilization": [], "disk_used_percent": [],
                "disk_used_percent_by_mount": {}}


# ── Metric series — EBS (now VM-backed) ──────────────────────────────────

def _get_ebs_metric_series(volume_id, region=None, hours=6) -> dict:
    try:
        end    = datetime.now(timezone.utc)
        start  = end - timedelta(hours=hours)
        period = _smart_period(hours)
        dim    = f'dimension_VolumeId="{volume_id}"'

        def s(db_metric_name):
            return _metric_history_query_range("ebs", volume_id, db_metric_name, start, end)
        return {
            "volume_id":    volume_id,
            "read_ops":     s("volumereadops"),
            "write_ops":    s("volumewriteops"),
            "read_bytes":   s("volumereadbytes"),
            "write_bytes":  s("volumewritebytes"),
            "queue_length": s("volumequeuelength"),
            # burst_balance: Phase 1's GMD collector deliberately dropped
            # BurstBalance ("gp3 irrelevant" per its own triage note), and
            # unlike the other 5 fields here, this one has NO boto3
            # fallback -- it can structurally never have data, not just
            # "none in this time window". Explicit None (not an empty
            # list from a query that will always return nothing) tells
            # the frontend to hide this chart card entirely instead of
            # showing a permanent, pointless "no data" placeholder. See
            # apply_hide_no_data_metrics.py.
            "burst_balance": None,
            "period_hours": hours,
            "period_secs":  period,
        }
    except Exception as e:
        logger.warning(f"EBS series [{volume_id}]: {e}")
        return {"volume_id": volume_id, "read_ops": [], "write_ops": [],
                "read_bytes": [], "write_bytes": [], "queue_length": [], "burst_balance": []}


# ── Metric series — Lambda (SPLIT: VM once deployed, boto3 fallback) ─────
# Enable lambda.Invocations/Errors/Duration/Throttles/ConcurrentExecutions
# in the Metric Catalog + deploy the generated YACE config to get these off
# boto3 entirely. Metric-name guesses below follow the same snake-case rule
# already confirmed live for EC2/RDS/ALB (CPUUtilization -> cpuutilization,
# a lowercase word -> Uppercase word transition -> underscore) but haven't
# been checked against a running YACE for AWS/Lambda specifically — verify
# with `curl "http://<vm-host>/api/v1/label/__name__/values" | grep aws_lambda`
# after first deploy. Falls back to boto3 automatically if VM has nothing.

def _get_lambda_metric_series(function_name, region=None, hours=6) -> dict:
    return _cached(
        f"lambda_series_{function_name}_{region}_{hours}",
        lambda: _get_lambda_metric_series_raw(function_name, region, hours),
        ttl=_LAMBDA_SERIES_CACHE_TTL,
    )


def _get_lambda_metric_series_raw(function_name, region=None, hours=6) -> dict:
    try:
        end    = datetime.now(timezone.utc)
        start  = end - timedelta(hours=hours)
        period = _smart_period(hours)
        dim    = f'dimension_FunctionName="{function_name}"'

        def vm_series(db_metric_name):
            return _metric_history_query_range("lambda", function_name, db_metric_name,
                                                start, end, match_field="name")

        result = {
            "invocations": vm_series("invocations"),
            "errors":      vm_series("errors"),
            "duration":    vm_series("duration"),
            # concurrent: Phase 1's GMD collector never collects
            # ConcurrentExecutions -- metric_history never has it, so this
            # always returns [] and correctly falls through to the boto3
            # fallback below every time (safe: this function already has
            # per-metric fallback logic, unlike EBS burst_balance).
            "concurrent":  vm_series("concurrentexecutions"),
            "throttles":   vm_series("throttles"),
        }

        missing = [k for k, v in result.items() if not v]
        if missing:
            cw   = boto3.client("cloudwatch", region_name=region)
            dims = [{"Name": "FunctionName", "Value": function_name}]
            fallback_map = {
                "invocations": ("Invocations", "Sum"),
                "errors":      ("Errors", "Sum"),
                "duration":    ("Duration", "Average"),
                "throttles":   ("Throttles", "Sum"),
                "concurrent":  ("ConcurrentExecutions", "Average"),
            }
            queries = [_make_query(k, "AWS/Lambda", fallback_map[k][0], dims, fallback_map[k][1])
                       for k in missing]
            fb = _gmd_series(cw, queries, hours)
            for k in missing:
                result[k] = fb.get(k, [])

        return {
            "function_name": function_name,
            "invocations":   result["invocations"],
            "errors":        result["errors"],
            "duration":      result["duration"],
            "throttles":     result["throttles"],
            "concurrent":    result["concurrent"],
            "period_hours":  hours,
            "period_secs":   period,
        }
    except Exception as e:
        logger.warning(f"Lambda series [{function_name}]: {e}")
        return {"function_name": function_name, "invocations": [], "errors": [],
                "duration": [], "throttles": [], "concurrent": []}


# ── Metric series — RDS (now VM-backed) ──────────────────────────────────

def _get_rds_metric_series(db_id, region=None, hours=6) -> dict:
    try:
        end    = datetime.now(timezone.utc)
        start  = end - timedelta(hours=hours)
        period = _smart_period(hours)
        dim    = f'dimension_DBInstanceIdentifier="{db_id}"'

        def s(db_metric_name):
            return _metric_history_query_range("rds", db_id, db_metric_name, start, end)
        return {
            "db_id":           db_id,
            "cpu":             s("cpuutilization"),
            "free_storage":    s("freestorage"),
            "db_connections":  s("dbconnections"),
            "read_iops":       s("readiops"),
            "write_iops":      s("writeiops"),
            "read_latency":    s("readlatency"),
            "write_latency":   s("writelatency"),
            "freeable_memory": s("freeablememory"),
            "period_hours":    hours,
            "period_secs":     period,
        }
    except Exception as e:
        logger.warning(f"RDS series [{db_id}]: {e}")
        return {"db_id": db_id, "cpu": [], "free_storage": [], "db_connections": [],
                "read_iops": [], "write_iops": [], "read_latency": [],
                "write_latency": [], "freeable_memory": []}


# ── Metric series — ELB (all 8 metrics now VM-first, boto3 fallback) ────
# Your YACE config already scrapes RequestCount, TargetResponseTime,
# HealthyHostCount, UnHealthyHostCount, HTTPCode_Target_2XX/4XX/5XX_Count,
# ActiveConnectionCount. HTTPCode_ELB_5XX_Count and NewConnectionCount are
# NOT in that YACE job yet — enable "alb.HTTPCode_ELB_5XX_Count" and
# "alb.NewConnectionCount" in the Metric Catalog and redeploy the generated
# config to get them into YACE too; the VM names below follow the same
# snake-case rule already confirmed for the other 6 ALB metrics in this
# file (HTTPCode_Target_5XX_Count -> httpcode_target_5_xx_count). Until
# then — or if VM has no data yet for any reason — this falls back to
# boto3 automatically per-metric, so it's safe to ship either way.
# Dimension label: "dimension_LoadBalancer" — verify with:
#   curl "http://<vm-host>/api/v1/series?match[]=aws_applicationelb_request_count_sum"

def _get_elb_metric_series(lb_name: str, region=None, hours=6) -> dict:
    try:
        elbv2 = boto3.client("elbv2", region_name=region)

        lb_dim = lb_name
        try:
            lbs = elbv2.describe_load_balancers(Names=[lb_name]).get("LoadBalancers", [])
            if lbs:
                arn    = lbs[0]["LoadBalancerArn"]
                lb_dim = arn.split("loadbalancer/")[-1]
        except Exception:
            pass

        end    = datetime.now(timezone.utc)
        start  = end - timedelta(hours=hours)
        period = _smart_period(hours)
        dim    = f'dimension_LoadBalancer="{lb_dim}"'

        # Match on the ORIGINAL bare lb_name param (== resources.name),
        # NOT lb_dim (the ARN-suffix computed above for the CloudWatch
        # fallback dimension) -- resource_discovery stores resources.name
        # as the bare LoadBalancerName, confirmed against
        # app/collector/discovery/runner.py's _discover_elb().
        def vm_series(db_metric_name):
            return _metric_history_query_range("elb", lb_name, db_metric_name,
                                                start, end, match_field="name")

        # requests/errors_5xx/latency/healthy_hosts: Phase 1's GMD collector
        # covers these (ELB_METRICS). The other 5 keys were deliberately
        # excluded from Phase 1 (4XX/ELB-5XX/UnHealthyHostCount dropped as
        # "client noise"/"redundant" per its own triage note;
        # ActiveConnectionCount/NewConnectionCount were never in the YACE
        # config either) -- metric_history never has them, so they always
        # return [] and correctly fall through to the boto3 fallback below
        # every time. Safe: this function already had per-metric fallback
        # logic for exactly this situation.
        result = {
            "requests":           vm_series("requestcount"),
            "errors_5xx":         vm_series("errors5xx"),
            "errors_4xx":         vm_series("errors4xx"),
            "errors_elb_5xx":     vm_series("errorselb5xx"),
            "latency":            vm_series("responselatency"),
            # healthyhosts/unhealthyhosts (the plain names) NEVER had data via
            # ANY path -- confirmed against AWS's own docs: CloudWatch's
            # HealthyHostCount/UnHealthyHostCount require BOTH LoadBalancer
            # AND TargetGroup dimensions, which this app's CloudWatch-based
            # collection and its boto3 fallback never supplied. Now reads
            # from describe_polling.py's DescribeTargetHealth-based
            # aggregation instead (no CloudWatch dimension problem at all,
            # since it's not a CloudWatch call). See
            # apply_fix_alb_healthy_hosts.py.
            "healthy_hosts":      _metric_history_query_range("elb", lb_name, "healthyhosts_describe", start, end, match_field="name"),
            "unhealthy_hosts":    _metric_history_query_range("elb", lb_name, "unhealthyhosts_describe", start, end, match_field="name"),
            "active_connections": vm_series("activeconnections"),
            "new_connections":    vm_series("newconnections"),
        }

        missing = [k for k, v in result.items() if not v]
        if missing:
            cw   = boto3.client("cloudwatch", region_name=region)
            dims = [{"Name": "LoadBalancer", "Value": lb_dim}]
            ns   = "AWS/ApplicationELB"
            fallback_map = {
                "requests":           ("RequestCount", "Sum"),
                "errors_5xx":         ("HTTPCode_Target_5XX_Count", "Sum"),
                "errors_4xx":         ("HTTPCode_Target_4XX_Count", "Sum"),
                "errors_elb_5xx":     ("HTTPCode_ELB_5XX_Count", "Sum"),
                "latency":            ("TargetResponseTime", "Average"),
                # healthy_hosts/unhealthy_hosts deliberately NOT here --
                # this fallback only ever supplies a LoadBalancer
                # dimension, and CloudWatch requires TargetGroup too for
                # these two metrics (confirmed against AWS's docs). This
                # fallback would waste a real API call for a guaranteed
                # empty result. See apply_fix_alb_healthy_hosts.py --
                # these two are populated by describe_polling.py instead,
                # never by this fallback.
                "active_connections": ("ActiveConnectionCount", "Average"),
                "new_connections":    ("NewConnectionCount", "Sum"),
            }
            queries = [_make_query(k, ns, fallback_map[k][0], dims, fallback_map[k][1])
                       for k in missing if k in fallback_map]
            fb = _gmd_series(cw, queries, hours)
            for k in missing:
                result[k] = fb.get(k, [])

        result.update({"lb_name": lb_name, "period_hours": hours, "period_secs": period})
        return result
    except Exception as e:
        logger.warning(f"ELB series [{lb_name}]: {e}")
        return {"lb_name": lb_name, "requests": [], "errors_5xx": [], "errors_4xx": [],
                "errors_elb_5xx": [], "latency": [], "healthy_hosts": [],
                "unhealthy_hosts": [], "active_connections": [], "new_connections": []}


# ── Metric series — ECS (SPLIT: AWS/ECS via VM once deployed, ─────────────
# ContainerInsights via boto3 always — see note below)
#
# Once you enable ecs.CPUUtilization/MemoryUtilization in the Metric Catalog
# and deploy the generated YACE config, these two become VM-backed like
# EC2/EBS/RDS — same pattern as _get_elb_metric_series. If VM has no data yet
# (not deployed, or YACE hasn't scraped this cluster yet) it falls back to
# boto3 automatically, so this is safe to ship before the YACE side is live.
#
# ECS/ContainerInsights task-count metrics stay on boto3 unconditionally:
# YACE's metric-name snake-casing for that namespace hasn't been verified
# against a live VM instance (unlike AWS/ECS, which follows the same
# lowercase-collapse rule already confirmed for EC2/RDS CPUUtilization).
# Verify with:
#   curl "http://<vm-host>/api/v1/label/__name__/values" | grep aws_ecs
# and wire ci_series() the same way once confirmed — it's a low-volume,
# rarely-clicked chart, so it's not a priority cost driver.

def _get_ecs_metric_series(cluster_name: str, service_name: str = None,
                           region=None, hours=6) -> dict:
    try:
        dims = (
            [{"Name": "ClusterName", "Value": cluster_name},
             {"Name": "ServiceName", "Value": service_name}]
            if service_name
            else [{"Name": "ClusterName", "Value": cluster_name}]
        )

        end    = datetime.now(timezone.utc)
        start  = end - timedelta(hours=hours)
        period = _smart_period(hours)
        dim    = f'dimension_ClusterName="{cluster_name}"'
        if service_name:
            dim += f',dimension_ServiceName="{service_name}"'

        # Match on the bare cluster_name param (== resources.name), confirmed
        # against app/collector/discovery/runner.py's _discover_ecs().
        def vm_series(db_metric_name):
            return _metric_history_query_range("ecs", cluster_name, db_metric_name,
                                                start, end, match_field="name")

        # AWS/ECS CPUUtilization/MemoryUtilization are EXCLUDED from Phase
        # 1's GMD collector entirely (its own docstring: "AWS/ECS basic
        # monitoring is FREE (no API cost)" -- deliberately left on boto3).
        # metric_history never has these, so both calls always return []
        # and this always falls through to the boto3 fallback below -- a
        # behavior-preserving no-op change (this chart was already
        # effectively boto3-only in practice, same as the comments above
        # already implied before VM was ever confirmed to have this data).
        cpu = vm_series("cpuutilization")
        mem = vm_series("memoryutilization")

        # boto3 fallback for AWS/ECS if VM has nothing yet (not deployed /
        # not scraped yet) — same safety pattern as _get_elb_metric_series.
        if not cpu or not mem:
            cw = boto3.client("cloudwatch", region_name=region)
            fallback_q = [
                _make_query("cpu", "AWS/ECS", "CPUUtilization",    dims, "Average"),
                _make_query("mem", "AWS/ECS", "MemoryUtilization", dims, "Average"),
            ]
            fb = _gmd_series(cw, fallback_q, hours)
            cpu = cpu or fb.get("cpu", [])
            mem = mem or fb.get("mem", [])

        cw = boto3.client("cloudwatch", region_name=region)
        ci_ns = "ECS/ContainerInsights"
        ci_queries = [
            _make_query("running",  ci_ns, "RunningTaskCount",  dims, "Average"),
            _make_query("pending",  ci_ns, "PendingTaskCount",  dims, "Average"),
            _make_query("desired",  ci_ns, "DesiredTaskCount",  dims, "Average"),
            _make_query("cpu_res",  ci_ns, "CpuReserved",       dims, "Average"),
            _make_query("mem_res",  ci_ns, "MemoryReserved",    dims, "Average"),
        ]
        ci = _gmd_series(cw, ci_queries, hours)

        return {
            "cluster_name":       cluster_name,
            "service_name":       service_name,
            "cpu_utilization":    cpu,
            "mem_utilization":    mem,
            "running_task_count": ci.get("running", []),
            "pending_task_count": ci.get("pending", []),
            "desired_task_count": ci.get("desired", []),
            "cpu_reserved":       ci.get("cpu_res", []),
            "mem_reserved":       ci.get("mem_res", []),
            "period_hours":       hours,
            "period_secs":        period,
        }
    except Exception as e:
        logger.warning(f"ECS series [{cluster_name}/{service_name}]: {e}")
        return {"cluster_name": cluster_name, "service_name": service_name,
                "cpu_utilization": [], "mem_utilization": [],
                "running_task_count": [], "pending_task_count": [],
                "desired_task_count": [], "cpu_reserved": [], "mem_reserved": []}


# ── check_and_write_alerts (SPLIT: ec2/ebs/rds via VM, lambda via GMD) ──

def _account_metric_snapshot(account_id, resource_type, db_metric_name, key_field="resource_id"):
    """
    Reads the `metrics` last-value cache for every resource of one type
    in one account, keyed by either resource_id (bare identifier --
    ec2/ebs/rds) or name (bare name -- elb/lambda), matching whichever
    identifier check_and_write_alerts()'s SERVICE_RESOURCES already uses
    per service. Scoped to account_id, unlike Phase 4b's
    _metric_snapshot_query_all (that one didn't need account-scoping;
    this one, being explicitly per-account already, should stay that
    way). Returns {} on any error or no data -- never raises. See
    apply_check_thresholds_local_metrics.py (Phase 5).
    """
    out = {}
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                f"""SELECT r.{key_field} AS key_val, m.metric_value
                    FROM metrics m JOIN resources r ON r.id = m.resource_id
                    WHERE r.aws_account_id = %s AND r.resource_type = %s AND m.metric_name = %s""",
                (account_id, resource_type, db_metric_name),
            )
            for row in cur.fetchall():
                if row["metric_value"] is not None:
                    out[row["key_val"]] = float(row["metric_value"])
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.warning(f"account metric snapshot failed [{resource_type}/{db_metric_name}]: {e}")
    return out


def check_and_write_alerts(account_id: int, region: str, thresholds: list) -> list:
    """
    Evaluates thresholds against current data.
    ec2/ebs/rds thresholds are checked against VictoriaMetrics.
    lambda (and anything else not in YACE) still uses the boto3 GMD batch.
    Writes breaches to alerts table. Returns list of breach dicts.
    """
    from app.db import get_connection

    cw = boto3.client("cloudwatch", region_name=region)

    ec2_instances = collect_ec2_instances(region)
    ebs_volumes   = collect_ebs_volumes(region)
    rds_instances = collect_rds_instances(region)
    lambda_funcs  = collect_lambda_functions(region)
    elb_list      = collect_elb(region)   # NEW — needed to route ALB thresholds to VM

    def _lb_dim(lb):
        """YACE/CloudWatch dimension value is the ARN suffix after 'loadbalancer/', not the LB name."""
        arn = lb.get("load_balancer_arn", "")
        return arn.split("loadbalancer/")[-1] if "loadbalancer/" in arn else lb.get("name", "")

    SERVICE_RESOURCES = {
        "ec2":    [(i["instance_id"], [{"Name": "InstanceId",           "Value": i["instance_id"]}]) for i in ec2_instances if i["state"] == "running"],
        "ebs":    [(v["volume_id"],   [{"Name": "VolumeId",             "Value": v["volume_id"]}])   for v in ebs_volumes   if v["state"] == "in-use"],
        "rds":    [(d["db_instance_id"], [{"Name": "DBInstanceIdentifier","Value": d["db_instance_id"]}]) for d in rds_instances],
        "lambda": [(f["function_name"],  [{"Name": "FunctionName",      "Value": f["function_name"]}])    for f in lambda_funcs],
        "alb":    [(lb["name"],          [{"Name": "LoadBalancer",      "Value": _lb_dim(lb)}])            for lb in elb_list],  # NEW
    }
    NAMESPACE_MAP = {
        "ec2": "AWS/EC2", "ebs": "AWS/EBS",
        "rds": "AWS/RDS", "lambda": "AWS/Lambda",
        "alb": "AWS/ApplicationELB",
    }

    # svc -> resources.name (bare) vs resources.resource_id (bare) --
    # ec2/ebs/rds resource_id IS the bare identifier; elb/lambda's
    # resource_id is a full ARN, so those match on name instead. Same
    # distinction Phase 4a/4b already confirmed against
    # app/collector/discovery/runner.py.
    LOCAL_KEY_FIELD = {
        "ec2": "resource_id", "ebs": "resource_id", "rds": "resource_id",
        "alb": "name",
    }
    # metric_catalog's service key for ALB metrics is "alb" (matches
    # SERVICE_RESOURCES/NAMESPACE_MAP above, pre-existing), but
    # discovery/runner.py stores ALB resources under resource_type='elb'
    # -- confirmed, not assumed. Needed only for the local DB lookup;
    # CloudWatch/GMD calls elsewhere in this function never used
    # resources.resource_type at all, so this mapping is new, not a fix
    # to something that was broken before.
    LOCAL_RESOURCE_TYPE = {"alb": "elb"}

    # db_metric_name strings below are copied verbatim from
    # app/collector/metrics/runner.py's EC2_METRICS_CRITICAL/LOW,
    # EBS_METRICS, RDS_METRICS, ELB_METRICS tuples -- i.e. exactly what
    # Phase 1's GMD collector actually writes into the `metrics` table,
    # not a fresh guess at a naming convention. ec2 StatusCheckFailed is
    # DELIBERATELY NOT here -- see apply_check_thresholds_local_metrics.py's
    # docstring: describe_polling.py is a separate, still-live VM writer
    # for that one metric specifically, untouched by this fix.
    # EBS BurstBalance and ALB HTTPCode_Target_4XX_Count are ALSO
    # deliberately absent -- Phase 1 never collects either, so leaving
    # them out of this dict means they correctly fall through to the
    # existing GMD/boto3 fallback branch below instead of ever being
    # looked up here.
    LOCAL_METRIC_STUB = {
        ("ec2", "CPUUtilization"):  "cpuutilization",
        ("ec2", "NetworkIn"):       "networkin",
        ("ec2", "NetworkOut"):      "networkout",
        ("ec2", "DiskReadBytes"):   "diskreadbytes",
        ("ec2", "DiskWriteBytes"):  "diskwritebytes",
        # Fixed here (apply_final_cleanup.py) -- Phase 5 documented this
        # as "left on vm_query()" but its own patch actually removed the
        # vm_query() path entirely, so this was silently falling through
        # to a real billed CloudWatch call. app/aws/describe_polling.py
        # now also writes this into the local `metrics` table (in
        # addition to its existing VM push, unchanged), so it belongs
        # here for real now.
        ("ec2", "StatusCheckFailed"): "statuscheckfailed",

        ("ebs", "VolumeQueueLength"): "volumequeuelength",
        ("ebs", "VolumeReadOps"):     "volumereadops",
        ("ebs", "VolumeWriteOps"):    "volumewriteops",
        ("ebs", "VolumeReadBytes"):   "volumereadbytes",
        ("ebs", "VolumeWriteBytes"):  "volumewritebytes",

        ("rds", "CPUUtilization"):   "cpuutilization",
        ("rds", "FreeStorageSpace"): "freestorage",

        ("alb", "RequestCount"):              "requestcount",
        ("alb", "HTTPCode_Target_5XX_Count"): "errors5xx",
        ("alb", "TargetResponseTime"):        "responselatency",
        # Both HealthyHostCount and UnHealthyHostCount now map to
        # describe_polling.py's DescribeTargetHealth-based aggregation --
        # neither ever had a working CloudWatch-based source (confirmed:
        # both require a TargetGroup dimension this app never supplied).
        # UnHealthyHostCount is a NEW entry here -- it never had ANY
        # local source before this fix. See apply_fix_alb_healthy_hosts.py.
        ("alb", "HealthyHostCount"):          "healthyhosts_describe",
        ("alb", "UnHealthyHostCount"):        "unhealthyhosts_describe",
    }

    local_lookups  = []   # (t_idx, resource_id, value_or_None)
    gmd_queries    = []
    qid_map        = {}
    snapshot_cache = {}   # (resource_type, db_metric_name, key_field) -> {key: value}, fetched once per unique combo

    for t_idx, t in enumerate(thresholds):
        svc       = (t.get("service") or t.get("resource_type") or "").lower()
        namespace = NAMESPACE_MAP.get(svc, t.get("namespace", "AWS/EC2"))
        metric    = t["metric_name"]
        stat      = t.get("statistic") or "Average"
        resources = SERVICE_RESOURCES.get(svc, []) or [("account", [])]
        stub      = LOCAL_METRIC_STUB.get((svc, metric))

        if stub:
            key_field     = LOCAL_KEY_FIELD.get(svc, "resource_id")
            resource_type = LOCAL_RESOURCE_TYPE.get(svc, svc)
            cache_key     = (resource_type, stub, key_field)
            if cache_key not in snapshot_cache:
                snapshot_cache[cache_key] = _account_metric_snapshot(
                    account_id, resource_type, stub, key_field
                )
            snap = snapshot_cache[cache_key]
            for resource_id, dims in resources:
                local_lookups.append((t_idx, resource_id, snap.get(resource_id)))
        else:
            for resource_id, dims in resources:
                qid = _safe_qid(f"t{t_idx}__{resource_id}")
                gmd_queries.append(_make_query(qid, namespace, metric, dims, stat))
                qid_map[qid] = (resource_id, t_idx)

    all_vals = {}  # (t_idx, resource_id) -> value

    for t_idx, resource_id, val in local_lookups:
        if val is not None:
            all_vals[(t_idx, resource_id)] = val

    gmd_snap = _gmd_snapshot(cw, gmd_queries, minutes=3)
    for qid, val in gmd_snap.items():
        resource_id, t_idx = qid_map[qid]
        all_vals[(t_idx, resource_id)] = val

    breaches = []
    conn     = get_connection()
    cur      = conn.cursor()

    def breached(v, threshold, comp):
        return (
            (comp == ">"  and v >  threshold) or
            (comp == "<"  and v <  threshold) or
            (comp == ">=" and v >= threshold) or
            (comp == "<=" and v <= threshold)
        )

    for (t_idx, resource_id), val in all_vals.items():
        t         = thresholds[t_idx]
        comp      = t["comparison"]
        warn_val  = float(t["warning_value"])
        crit_val  = float(t["critical_value"])
        metric    = t["metric_name"]
        svc       = (t.get("service") or t.get("resource_type") or "").lower()

        if breached(val, crit_val, comp):
            severity = "CRITICAL"
        elif breached(val, warn_val, comp):
            severity = "WARNING"
        else:
            continue

        threshold_val = crit_val if severity == "CRITICAL" else warn_val
        breaches.append({
            "metric":    metric,
            "service":   svc,
            "resource":  resource_id,
            "value":     round(val, 4),
            "threshold": threshold_val,
            "severity":  severity,
        })
        try:
            # Match alert_evaluator.py's own definition of "already open"
            # -- ANY existing active alert for this resource+metric, not
            # just one from the last 10 minutes. The old 10-minute window
            # meant every manual "Check Thresholds Now" click more than
            # 10 minutes apart inserted a brand-new duplicate row on top
            # of whatever was already active, rather than refreshing it
            # -- confirmed live: the same 21 gp3 volumes got a second
            # full batch of BurstBalance alerts a day after the first,
            # doubling up instead of updating in place. Also now sets
            # last_seen_at, which the old INSERT never did at all --
            # leaving it NULL, which made `stale` (alerts.py) evaluate to
            # false forever (its check requires last_seen_at IS NOT
            # NULL), so these alerts could never even surface as stale
            # for an operator to notice and clean up manually.
            cur.execute("""
                SELECT id FROM alerts
                WHERE resource_id=%s AND metric_name=%s AND status='active'
                LIMIT 1
            """, (resource_id, metric))
            existing = cur.fetchone()
            if existing:
                cur.execute("""
                    UPDATE alerts
                    SET current_value=%s, threshold=%s, value=%s,
                        severity=%s, last_seen_at=NOW()
                    WHERE id=%s
                """, (round(val, 4), threshold_val, round(val, 4),
                      severity, existing[0]))
            else:
                cur.execute("""
                    INSERT INTO alerts
                      (resource_id, metric_name, severity, status,
                       current_value, threshold, value,
                       triggered_at, last_seen_at, environment)
                    VALUES (%s,%s,%s,'active',%s,%s,%s,NOW(),NOW(),'PROD')
                """, (resource_id, metric, severity,
                      round(val, 4), threshold_val, round(val, 4)))
        except Exception as db_err:
            logger.warning(f"Alert insert [{resource_id}/{metric}]: {db_err}")

    conn.commit()
    cur.close()
    conn.close()
    return breaches


# ── Account summary (unchanged) ──────────────────────────────────────────

def get_account_summary(region=None, role_arn=None, external_id=None) -> dict:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    collectors = {
        "ec2": lambda: collect_ec2_instances(region, role_arn, external_id),
        "ebs": lambda: collect_ebs_volumes(region, role_arn, external_id),
        "rds": lambda: collect_rds_instances(region, role_arn, external_id),
        "lmb": lambda: collect_lambda_functions(region, role_arn, external_id),
        "s3":  lambda: collect_s3_buckets(region, role_arn, external_id),
        "elb": lambda: collect_elb(region, role_arn, external_id),
        "ecs": lambda: collect_ecs_clusters(region, role_arn, external_id),
    }
    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(fn): key for key, fn in collectors.items()}
        for f in as_completed(futures):
            key = futures[f]
            try:
                results[key] = f.result()
            except Exception as e:
                logger.error(f"Collector [{key}]: {e}")
                results[key] = []

    ec2  = results.get("ec2", [])
    run  = [i for i in ec2 if i["state"] == "running"]
    stop = [i for i in ec2 if i["state"] == "stopped"]
    avg  = round(sum(i["cpu_utilization"] for i in run) / len(run), 2) if run else 0.0

    return {
        "ec2_total":    len(ec2),           "ec2_running":  len(run),
        "ec2_stopped":  len(stop),          "ec2_avg_cpu":  avg,
        "ebs_total":    len(results.get("ebs", [])),
        "rds_total":    len(results.get("rds", [])),
        "lambda_total": len(results.get("lmb", [])),
        "s3_total":     len(results.get("s3",  [])),
        "elb_total":    len(results.get("elb", [])),
        "ecs_total":    len(results.get("ecs", [])),
        "instances":    ec2,
        "ebs":          results.get("ebs", []),
        "rds":          results.get("rds", []),
        "lambdas":      results.get("lmb", []),
        "s3":           results.get("s3",  []),
        "elb":          results.get("elb", []),
        "ecs":          results.get("ecs", []),
    }


# ── Helpers ────────────────────────────────────────────────────────────

def _calc_uptime(lt) -> int:
    if not lt:
        return 0
    try:
        now = datetime.now(timezone.utc)
        if lt.tzinfo is None:
            lt = lt.replace(tzinfo=timezone.utc)
        return (now - lt).days
    except:
        return 0