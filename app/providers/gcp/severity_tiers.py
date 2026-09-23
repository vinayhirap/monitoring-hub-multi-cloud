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
# Polling-model audit (2026-09-23): only availability stays critical
# (2 min, evaluated every tick). CPU/latency are 5-min signals -- polling
# them every 2 min multiplied billed time series without faster alerting.
CRITICAL_METRICS = {
    "cloudsql_instance": {"up"},
}

LOW_METRICS = {
    "compute_instance": {
        "disk/read_bytes_count", "disk/write_bytes_count",
        "disk/read_ops_count", "disk/write_ops_count",
    },
    "gcs_bucket": {"network/sent_bytes_count", "total_byte_seconds",
                   "total_bytes", "object_count"},
    "cloudsql_instance": {
        "disk/read_ops_count", "disk/write_ops_count",
        "mysql/replication/seconds_behind_master",
        "postgresql/replication/replica_byte_lag",
    },
    "cloud_run_service": {"billable_instance_time", "startup_latencies"},
}

# Removed from collection: cpu/usage_time duplicates cpu/utilization, and
# uptime_total duplicates uptime. Skipped even if still enabled in an
# account's saved selection (billed per time series returned).
REMOVED_METRICS = {
    "compute_instance": {"cpu/usage_time", "uptime_total"},
}

# Extended-service incident signals polled with the 5-min standard pass.
EXTENDED_STANDARD_METRICS = {
    "pubsub_subscription": {"oldest_unacked_message_age", "num_undelivered_messages"},
    "nat_gateway":         {"nat_allocation_failed"},
    "redis_instance":      {"stats/memory/usage_ratio"},
}

# Extended-service failure/saturation signals polled with the 15-min pass.
EXTENDED_LOW_METRICS = {
    "gke_cluster": {"restart_count", "memory/limit_utilization", "cpu/limit_utilization"},
    "cloud_lb":    {"backend_latencies", "total_latencies"},
}

TIER_INTERVAL_SECONDS = {"critical": 120, "standard": 300, "low": 900, "extended": 3600}


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
        names = _service_metric_names(curated, service_key) - REMOVED_METRICS.get(service_key, set())
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
            names = (_service_metric_names(curated, service_key)
                     - EXTENDED_STANDARD_METRICS.get(service_key, set())
                     - EXTENDED_LOW_METRICS.get(service_key, set()))
            if names:
                out[service_key] = names
    return out


def _merge(*maps):
    out = {}
    for m in maps:
        for k, v in m.items():
            out.setdefault(k, set()).update(v)
    return out


def build_standard_pass_metrics(curated):
    return _merge(build_standard_metrics(curated), EXTENDED_STANDARD_METRICS)


def build_low_pass_metrics():
    return _merge(LOW_METRICS, EXTENDED_LOW_METRICS)


def metric_tiers(curated):
    """{(service, metric_name): tier} for every collected curated metric."""
    out = {}
    for tier, mapping in (
        ("standard", build_standard_pass_metrics(curated)),
        ("low", build_low_pass_metrics()),
        ("extended", build_extended_metrics(curated)),
        ("critical", CRITICAL_METRICS),
    ):
        for service, names in mapping.items():
            for n in names:
                out[(service, n)] = tier
    return out
