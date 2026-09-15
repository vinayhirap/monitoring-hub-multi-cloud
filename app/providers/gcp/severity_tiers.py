# app/providers/gcp/severity_tiers.py
"""
Per-metric severity tiers for GCP, mirroring the CRITICAL/STANDARD/LOW
split app/collector/metrics/runner.py already hand-curates for AWS --
applied here because GCP's read API genuinely bills the same way AWS's
does in spirit: a per-unit cost that scales with what you request, not a
flat free allowance like Azure's. See metrics_collector.py's own docstring:
Cloud Monitoring's ListTimeSeries bills per TIME SERIES RETURNED as of
Google's Oct 2, 2025 pricing change ($0.50/million series returned, first
1,000,000/billing-account/month free), and that volume scales directly
with (metrics enabled) x (resources of that type) because list_time_series()
is fleet-wide per metric type. Tiering by severity here is the direct GCP
equivalent of why AWS keeps RDS on a tight critical-only gate instead of
polling everything on one cadence.

Why metric-name allowlists, not a new metric_catalog.category value: see
the identical note in app/providers/azure/severity_tiers.py -- same
reasoning, same no-DB-migration constraint.

CRITICAL here uses AWS's own 2-min critical interval (not Azure's 1-min),
deliberately -- Azure's tier is freshness-driven because Azure reads are
free at this app's scale; GCP's tier is cost-driven because GCP reads are
billed per series above the free allotment, so it inherits AWS's
cost-conscious cadence instead of Azure's freshness-driven one. This is
the "for paid API calls, decide by severity with reference to AWS" half of
the policy; Azure's severity_tiers.py is the "for free API calls, poll
close to real-time" half.
"""

# ── CORE tier: alertable / latency-sensitive -> CRITICAL (2 min, matching
#    AWS RDS/EC2-critical's own cadence). These are the metrics whose
#    "Recommended Default? = Yes" flag most directly indicates an outage
#    or failure mode in progress.
CRITICAL_METRICS = {
    "compute_instance":  {"cpu/utilization"},
    "cloudsql_instance": {"cpu/utilization", "up"},
    "cloud_run_service": {"cpu/utilizations", "request_latencies"},
}

# ── CORE tier: trend/diagnostic signals -> LOW (15 min, matching AWS's
#    EC2-Disk / Lambda-Invocations "low" precedent). Cumulative counters
#    and rarely-actioned replication/ops-count metrics: no freshness lost.
LOW_METRICS = {
    "compute_instance": {
        "cpu/usage_time", "disk/read_bytes_count", "disk/write_bytes_count",
        "disk/read_ops_count", "disk/write_ops_count", "uptime_total",
    },
    "gcs_bucket": {"network/sent_bytes_count", "total_byte_seconds"},
    "cloudsql_instance": {
        "disk/read_ops_count", "disk/write_ops_count",
        "mysql/replication/seconds_behind_master",
        "postgresql/replication/replica_byte_lag",
    },
    "cloud_run_service": {"billable_instance_time", "startup_latencies"},
}


def _service_metric_names(curated, service_key):
    _, _, _category, metrics = curated[service_key]
    return {m[0] for m in metrics}


def build_standard_metrics(curated):
    """{service_key: {metric_name,...}} for every 'core' service, minus
    whatever CRITICAL_METRICS/LOW_METRICS already claims for that service.
    A new core metric added to metric_catalog_data.py defaults into
    STANDARD (today's existing 5-min core cadence) until deliberately
    re-tiered -- never silently dropped or silently promoted."""
    out = {}
    for service_key, (_, _, category, _metrics) in curated.items():
        if category != "core":
            continue
        names = _service_metric_names(curated, service_key)
        claimed = CRITICAL_METRICS.get(service_key, set()) | LOW_METRICS.get(service_key, set())
        standard = names - claimed
        if standard:
            out[service_key] = standard
    return out


def build_extended_metrics(curated):
    """{service_key: {metric_name,...}} for every 'extended' service --
    used to move the WHOLE extended tier from 15 min to 60 min (matching
    AWS's own extended-tier interval), the direct lever on the
    (metrics enabled) x (resources) series-returned volume this module's
    docstring describes. No further per-metric split within extended:
    unlike Azure, none of these have a documented Google-side publish
    rate slower than 60 sec, so there's no freshness argument for a
    finer split -- only the cost argument for slowing the whole tier."""
    out = {}
    for service_key, (_, _, category, _metrics) in curated.items():
        if category == "extended":
            out[service_key] = _service_metric_names(curated, service_key)
    return out
