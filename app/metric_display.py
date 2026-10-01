# app/metric_display.py
"""
ONE place that decides how a metric is PRESENTED -- for every cloud and every
service. Pure functions + data; nothing here touches the network.

Why this exists (CloudOps vs AWS Console audit, 2026-10-01):
  * chart titles/units/scaling were hand-typed per call site in the frontend
    (60+ MetricChart calls), so they drifted from CloudWatch -- e.g. EC2
    Network and EBS ops were divided by a hard-coded 60 although the data is
    collected at Period=300 since 2026-09-29;
  * the polling cadence of a metric lived only in polling_model.py and was
    invisible in the UI;
  * thresholds were read once per page mount, so a Settings change (or a
    dynamic/baseline band) never reached an open chart.

Everything below degrades to "show the metric as-is": an unknown cloud /
service / metric never raises, it simply gets default presentation.
"""
import math
import os
from datetime import datetime, timezone
from functools import lru_cache

# metric_history is pruned to this many days (scheduler.py low tier).
# Ranges longer than this CANNOT have more data; the UI says so.
METRIC_HISTORY_RETENTION_DAYS = 30

STATS = ("Average", "Minimum", "Maximum", "Sum", "SampleCount")
_STAT_FIELD = {"Average": "a", "Minimum": "mn", "Maximum": "mx", "Sum": "s", "SampleCount": "n"}

# ── AWS Console titles (what the console prints above each chart) ───────
# {(service, CloudWatch MetricName): console title}. Anything missing falls
# back to the official CloudWatch metric name, so adding services never breaks.
AWS_CONSOLE_TITLES = {
    ("ec2", "CPUUtilization"): "CPU utilization (%)",
    ("ec2", "NetworkIn"): "Network in (bytes)",
    ("ec2", "NetworkOut"): "Network out (bytes)",
    ("ec2", "NetworkPacketsIn"): "Network packets in (count)",
    ("ec2", "NetworkPacketsOut"): "Network packets out (count)",
    ("ec2", "MetadataNoToken"): "Metadata no token (count)",
    ("ec2", "CPUCreditUsage"): "CPU credit usage (count)",
    ("ec2", "CPUCreditBalance"): "CPU credit balance (count)",
    ("ec2", "DiskReadBytes"): "Disk read (bytes)",
    ("ec2", "DiskWriteBytes"): "Disk write (bytes)",
    ("ec2", "StatusCheckFailed"): "Status check failed (any) (count)",
    ("ec2", "StatusCheckFailed_Instance"): "Status check failed (instance) (count)",
    ("ec2", "StatusCheckFailed_System"): "Status check failed (system) (count)",
    ("ebs", "VolumeReadOps"): "Read operations (Ops/s)",
    ("ebs", "VolumeWriteOps"): "Write operations (Ops/s)",
    ("ebs", "VolumeReadBytes"): "Read throughput (KiB/s)",
    ("ebs", "VolumeWriteBytes"): "Write throughput (KiB/s)",
    ("ebs", "VolumeQueueLength"): "Average queue length (Operations)",
    ("ebs", "BurstBalance"): "Burst balance (%)",
    ("elb", "RequestCount"): "Requests",
    ("elb", "TargetResponseTime"): "Target Response Time",
    ("elb", "HTTPCode_Target_5XX_Count"): "Target 5XXs",
    ("elb", "HTTPCode_Target_4XX_Count"): "Target 4XXs",
    ("elb", "HTTPCode_Target_2XX_Count"): "Target 2XXs",
    ("elb", "HTTPCode_Target_3XX_Count"): "Target 3XXs",
    ("elb", "HTTPCode_ELB_5XX_Count"): "ELB 5XXs",
    ("elb", "HTTPCode_ELB_4XX_Count"): "ELB 4XXs",
    ("elb", "ActiveConnectionCount"): "Active Connection Count",
    ("elb", "NewConnectionCount"): "New Connection Count",
    ("elb", "ProcessedBytes"): "Processed Bytes",
    ("elb", "RejectedConnectionCount"): "Sum rejected connections",
    ("elb", "TargetConnectionErrorCount"): "Target connection errors",
}
# alb/nlb catalog rows share the elb titles
for (_s, _m), _t in list(AWS_CONSOLE_TITLES.items()):
    if _s == "elb":
        AWS_CONSOLE_TITLES[("alb", _m)] = _t

# ── Rate display: stored value is a per-period TOTAL -> per-second ───────
# (service, metric) -> (display unit, divisor applied to value/period_secs)
# EBS ops/bytes are stored as CloudWatch Sum over `period_sec` (300 for
# EBS/EC2 -- polling_model.AWS_CORE_METRICS); the console shows Ops/s and
# KiB/s, i.e. Sum / period (/1024 for KiB).
RATE_DISPLAY = {
    ("ebs", "VolumeReadOps"): ("Count/Second", 1.0),
    ("ebs", "VolumeWriteOps"): ("Count/Second", 1.0),
    ("ebs", "VolumeReadBytes"): ("KiB/s", 1024.0),
    ("ebs", "VolumeWriteBytes"): ("KiB/s", 1024.0),
}
# ── Period cutover (EBS rate metrics) ────────────────────────────────────
# EBS ops/bytes were collected at Period=60 until 2026-09-29 and at Period=300
# afterwards (polling_model.AWS_CORE_METRICS). Stored points are per-period
# TOTALS, so a point stored before the cutover must be divided by 60 and one
# stored after by 300 -- otherwise old history reads ~5x too low for as long
# as it stays inside the 30-day retention window.
# The cutover is a per-server fact (dev and prod were deployed at different
# times), so it can be overridden with METRIC_PERIOD_CUTOVER_UTC in .env
# (ISO, UTC, e.g. 2026-09-29T10:55:00; "none" turns the legacy handling off).
# Default = prod: last 1-minute point 10:54, first 5-minute point 10:59 UTC.
RATE_LEGACY_PERIOD_SECS = 60
PERIOD_CUTOVER_DEFAULT = "2026-09-29T10:55:00"


def period_cutover_epoch():
    """UTC epoch of the 60 s -> 300 s switch, or None (disabled / unparsable).
    Never raises: a bad value must degrade to 'no legacy handling'."""
    raw = os.environ.get("METRIC_PERIOD_CUTOVER_UTC")
    raw = PERIOD_CUTOVER_DEFAULT if raw is None else raw.strip()
    if not raw or raw.lower() in ("none", "off", "0"):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", ""))
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


# EC2 NetworkIn/Out etc. are shown RAW (Bytes per period) exactly like the
# EC2 console ("Network in (bytes)"). To switch them to B/s, add
# ("ec2","NetworkIn"): ("Bytes/Second", 1.0) here -- nothing else changes.

# ── Sum display for metrics the collector stores as Average ──────────────
# The EC2 console graphs NetworkIn/NetworkOut as SUM per period. CloudOps
# stores them as Average (polling_model.AWS_CORE_METRICS; changing that would
# rescale the history the dynamic-baseline / anomaly alerts learned from).
# EC2 publishes one sample per minute, so a 300 s datapoint has SampleCount 5
# and Average = Sum / 5 (verified live 2026-10-01 with
# scripts/check_ec2_network_stat.py: SampleCount 5 on every full period).
# Display-only fix: shown value = stored Average x (period / 60). Threshold
# lines get the same factor (metric_meta multiplies by spec["scale"]), so an
# alert on "Average > X" is drawn at the matching Sum level, and the chart's
# default statistic becomes Sum, exactly like the console. No stored data and
# no alert evaluation change, so there is no cutover to handle.
SUM_FROM_AVG = {("ec2", "NetworkIn"), ("ec2", "NetworkOut")}
SECONDS_PER_SAMPLE = 60.0   # EC2 publishes one sample per minute

# Frontend response key -> CloudWatch metric, per bespoke service endpoint in
# app/api/live_data.py. Used to scale/aggregate those payloads generically.
RESPONSE_KEYS = {
    "ec2": {"cpu": "CPUUtilization", "network_in": "NetworkIn", "network_out": "NetworkOut",
            "disk_read": "DiskReadBytes", "disk_write": "DiskWriteBytes",
            "mem_utilization": "mem_used_percent", "disk_used_percent": "disk_used_percent"},
    "ebs": {"read_ops": "VolumeReadOps", "write_ops": "VolumeWriteOps",
            "read_bytes": "VolumeReadBytes", "write_bytes": "VolumeWriteBytes",
            "queue_length": "VolumeQueueLength", "burst_balance": "BurstBalance"},
    "rds": {"cpu": "CPUUtilization", "db_connections": "DatabaseConnections",
            "freeable_memory": "FreeableMemory", "read_iops": "ReadIOPS", "write_iops": "WriteIOPS",
            "read_latency": "ReadLatency", "write_latency": "WriteLatency"},
    "lambda": {"invocations": "Invocations", "errors": "Errors", "duration": "Duration",
               "throttles": "Throttles", "concurrent": "ConcurrentExecutions"},
    "elb": {"requests": "RequestCount", "errors_5xx": "HTTPCode_Target_5XX_Count",
            "errors_4xx": "HTTPCode_Target_4XX_Count", "errors_elb_5xx": "HTTPCode_ELB_5XX_Count",
            "latency": "TargetResponseTime", "healthy_hosts": "HealthyHostCount",
            "unhealthy_hosts": "UnHealthyHostCount", "active_connections": "ActiveConnectionCount",
            "new_connections": "NewConnectionCount"},
    "ecs": {"cpu_utilization": "CPUUtilization", "mem_utilization": "MemoryUtilization"},
}

# CloudWatch unit -> short suffix used in titles/tooltips
UNIT_SYMBOL = {"Percent": "%", "Bytes": "B", "Count": "", "Seconds": "s", "Milliseconds": "ms",
               "Microseconds": "µs", "Count/Second": "/s", "Bytes/Second": "B/s", "KiB/s": "KiB/s", "None": ""}


def _norm(s):
    return (s or "").strip()


def service_for_display(service):
    s = _norm(service).lower()
    return "elb" if s in ("alb", "nlb") else s


def display_spec(provider, service, metric_name, catalog_unit=None, catalog_stat=None):
    """-> dict(title, metric_name, unit, unit_symbol, scale, rate, native_stat).
    `scale` multiplies stored values; thresholds must be multiplied by it too."""
    svc = _norm(service).lower()
    name = _norm(metric_name)
    unit = _norm(catalog_unit) or "None"
    scale = 1.0
    legacy_scale = None   # divisor for points stored BEFORE the period cutover
    rate = False
    sum_from_avg = False
    native = _norm(catalog_stat) or "Average"
    if (provider or "aws") == "aws":
        title = AWS_CONSOLE_TITLES.get((svc, name)) or AWS_CONSOLE_TITLES.get((service_for_display(svc), name)) or name
        rd = RATE_DISPLAY.get((service_for_display(svc), name))
        if rd:
            rate = True
            unit, div = rd
            period = _aws_period_seconds(service_for_display(svc), name) or 300
            scale = 1.0 / (period * div)
            if period != RATE_LEGACY_PERIOD_SECS:
                legacy_scale = 1.0 / (RATE_LEGACY_PERIOD_SECS * div)
        elif (service_for_display(svc), name) in SUM_FROM_AVG:
            period = _aws_period_seconds(service_for_display(svc), name) or 300
            scale = max(1.0, period / SECONDS_PER_SAMPLE)
            sum_from_avg = True
            native = "Sum"
    else:
        title = name
    return {"title": title, "metric_name": name, "unit": unit,
            "unit_symbol": UNIT_SYMBOL.get(unit, ""), "scale": scale, "rate": rate,
            "legacy_scale": legacy_scale, "sum_from_avg": sum_from_avg,
            "native_stat": native}


def stats_available(native_stat, rate=False):
    """Stats that are mathematically honest for data STORED at `native_stat`.
    Re-aggregating stored points: Sum of stored averages is meaningless, so it
    is only offered for natively-Sum metrics; rates (already /period) never
    offer Sum."""
    out = ["Average", "Minimum", "Maximum"]
    if native_stat == "Sum" and not rate:
        out.append("Sum")
    out.append("SampleCount")
    return out


# ── Polling cadence (reads app/collector/polling_model.py -- the same data
#    the scheduler/runner use, so the UI cannot drift from the backend) ─────
def _aws_period_seconds(service, cw_name):
    from app.collector import polling_model as pm
    for m in pm.AWS_CORE_METRICS:
        if m.resource_type == service and m.cw_name == cw_name:
            return m.period_sec
    try:
        if service in pm.AWS_SLOW_EXTENDED_SERVICES or service not in pm._AWS_CORE_TYPES:
            return pm.aws_extended_period_sec(service, cw_name)
    except Exception:
        pass
    return None


@lru_cache(maxsize=1)
def _overrides():
    from app.collector import polling_model as pm
    return pm.metric_interval_overrides()


def polling_info(provider, service, metric_name, db_metric_name):
    """-> dict(interval_seconds, period_seconds, tier, source). Never raises."""
    provider = provider or "aws"
    svc = _norm(service).lower()
    try:
        from app.collector import polling_model as pm
        db = (db_metric_name or "").lower()
        rtype = service_for_display(svc)
        if provider == "aws":
            for m in pm.AWS_CORE_METRICS:
                if m.resource_type == rtype and m.cw_name == metric_name:
                    return {"interval_seconds": pm.TIER_SECONDS[m.tier], "period_seconds": m.period_sec,
                            "tier": m.tier, "lookback_min": m.lookback_min,
                            "source": f"app/collector/polling_model.py AWS_CORE_METRICS[{rtype}.{m.cw_name}] "
                                      f"tier={m.tier}; scheduler.py {m.tier.upper()}_INTERVAL"}
            if rtype == "ec2" and db == "mem_used_percent":
                t = pm.CWAGENT_MEM_TIER
                return {"interval_seconds": pm.TIER_SECONDS[t], "period_seconds": 60, "tier": t,
                        "lookback_min": pm.CWAGENT_MEM_LOOKBACK,
                        "source": "polling_model.py CWAGENT_MEM_TIER (CWAgent publishes every 60s)"}
            if rtype == "ec2" and (db == "disk_used_percent" or db.startswith("disk_used_percent__")):
                t = pm.CWAGENT_DISK_TIER
                return {"interval_seconds": pm.TIER_SECONDS[t], "period_seconds": 60, "tier": t,
                        "lookback_min": pm.CWAGENT_DISK_LOOKBACK,
                        "source": "polling_model.py CWAGENT_DISK_TIER (CWAgent publishes every 60s)"}
            if (rtype, db) in pm.AWS_DESCRIBE_METRICS:
                return {"interval_seconds": pm.DESCRIBE_POLL_SECONDS, "period_seconds": None, "tier": "describe",
                        "source": "app/aws/describe_polling.py poll_all (Describe* API, free)"}
        ov = _overrides().get((provider, rtype, db))
        if ov is None:
            for (p, r, prefix, secs) in pm.PREFIX_INTERVAL_OVERRIDES:
                if p == provider and r == rtype and db.startswith(prefix):
                    ov = secs
                    break
        if ov is None:
            ov = pm._CLASS_DEFAULT_INTERVAL[pm._aws_class(rtype)] if provider == "aws" else 300
        tier = next((k for k, v in pm.TIER_SECONDS.items() if v == ov), "custom")
        per = _aws_period_seconds(rtype, metric_name) if provider == "aws" else None
        return {"interval_seconds": ov, "period_seconds": per, "tier": tier,
                "source": {"aws": "polling_model.py AWS_EXTENDED_TIER_OVERRIDES / metric_interval_overrides()",
                           "azure": "providers/azure/severity_tiers.py metric_tiers()",
                           "gcp": "providers/gcp/severity_tiers.py metric_tiers()"}.get(provider, "polling_model.py")}
    except Exception:
        return {"interval_seconds": None, "period_seconds": None, "tier": None, "source": None}


def fmt_interval(seconds):
    if not seconds:
        return None
    if seconds % 86400 == 0:
        return f"{seconds // 86400} day" + ("s" if seconds > 86400 else "")
    if seconds % 3600 == 0:
        return f"{seconds // 3600} hr"
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} s"


# ── Range -> bucket size, stats per bucket ───────────────────────────────
_NICE = (60, 300, 900, 3600, 10800, 21600, 43200, 86400)


def bucket_seconds(hours, target_points=500):
    """Bucket size for a look-back of `hours`, <= ~target_points points, on a
    CloudWatch-style 'nice' step (1m/5m/15m/1h/3h/6h/12h/1d)."""
    raw = hours * 3600 / max(1, target_points)
    for n in _NICE:
        if n >= raw:
            return n
    return _NICE[-1]


def effective_hours(hours):
    """Ranges beyond metric_history retention cannot return more data."""
    return min(int(hours), METRIC_HISTORY_RETENTION_DAYS * 24)


def _epoch(t):
    d = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    if d.tzinfo is None:          # naive timestamps are UTC throughout this app
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def core_native_stat(service, cw_name):
    """Stat the collector stores for this metric (AWS_CORE_METRICS), else Average."""
    try:
        from app.collector import polling_model as pm
        for m in pm.AWS_CORE_METRICS:
            if m.resource_type == service_for_display(service) and m.cw_name == cw_name:
                return m.stat
    except Exception:
        pass
    return "Average"


def bucketize(series, bucket_secs, native_stat="Average", scale=1.0, rate=False,
              legacy_scale=None, cutover=None):
    """[{t,v}] -> [{t, v, a, mn, mx, s, n}]. `v` is the metric's native-stat
    aggregate (so un-aware consumers see the same meaning as before).
    Every point is multiplied by `scale` (rate display) BEFORE aggregating; a
    point older than `cutover` (epoch) uses `legacy_scale` instead, so a bucket
    spanning the 60 s -> 300 s period change is still correct. Never raises:
    on any malformed input the original series is returned."""
    try:
        if not isinstance(series, list):
            return series
        buckets = {}
        order = []
        for p in series:
            v = p.get("v")
            if v is None:
                continue
            ts = _epoch(p["t"])
            key = int(ts // bucket_secs * bucket_secs) if bucket_secs else int(ts)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            sc = legacy_scale if (legacy_scale is not None and cutover is not None and ts < cutover) else scale
            buckets[key].append(float(v) * sc)
        out = []
        for key in sorted(order):
            vals = buckets[key]
            n, s = len(vals), sum(vals)
            a, mn, mx = s / n, min(vals), max(vals)
            native = a if rate else {"Sum": s, "Minimum": mn, "Maximum": mx}.get(native_stat, a)
            t_iso = datetime.fromtimestamp(key, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            out.append({"t": t_iso, "v": round(native, 6), "a": round(a, 6),
                        "mn": round(mn, 6), "mx": round(mx, 6),
                        "s": round(s, 6), "n": n})
        return out
    except Exception:
        return series


def shape_response(service, result, hours, native_stats=None):
    """Post-process a bespoke metrics payload ({key: [{t,v}] | None | scalar}):
    scale rate metrics and bucket every series for the requested range.
    Unknown keys are bucketed with the Average aggregate, never dropped."""
    if not isinstance(result, dict):
        return result
    svc = service_for_display(service)
    keys = RESPONSE_KEYS.get(svc, {})
    native_stats = native_stats or {}
    hrs = effective_hours(hours)
    bsec = bucket_seconds(hrs)
    out = dict(result)
    for k, v in result.items():
        if not (isinstance(v, list) and v and isinstance(v[0], dict) and "t" in v[0] and "v" in v[0]):
            continue
        cw = keys.get(k)
        spec = display_spec("aws", svc, cw) if cw else None
        ns = spec["native_stat"] if (spec and spec.get("sum_from_avg")) else (
            native_stats.get(cw) or core_native_stat(svc, cw) if cw else "Average")
        out[k] = bucketize(v, bsec, ns,
                           spec["scale"] if spec else 1.0, rate=bool(spec and spec["rate"]),
                           legacy_scale=spec["legacy_scale"] if spec else None,
                           cutover=period_cutover_epoch())
    out["bucket_secs"] = bsec
    out["effective_hours"] = hrs
    out["requested_hours"] = int(hours)
    out["retention_days"] = METRIC_HISTORY_RETENTION_DAYS
    return out
