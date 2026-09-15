# app/providers/azure/severity_tiers.py
"""
Per-metric severity tiers for Azure, mirroring the CRITICAL/STANDARD/LOW/
EXTENDED split app/collector/metrics/runner.py already hand-curates for
AWS -- same judgment call (is this metric alertable/latency-sensitive, or
a trend signal that loses nothing at a slower cadence), applied to
Azure's own metric list instead of copied verbatim.

Why this exists as metric-name allowlists (not a 5th `metric_catalog.category`
value): Azure/GCP tiering today is driven entirely by `category` ('core' /
'extended' / 'directory'), read straight out of the metric_catalog DB table
(see _enabled_azure_metrics() in metrics_collector.py). Adding CRITICAL/
STANDARD/LOW as new category values would require a DB migration and
reseeding metric_catalog before this could take effect -- out of scope for
a reviewable patch with no live DB access to test against. This module
gives the same tiering *without* touching the schema: multicloud_scheduler.py
passes one of the dicts below as `only_metric_names` to
collect_account_metrics()/collect_all_azure_accounts(), which filters the
already-enabled selection down to just this pass's metric names, in Python,
after the existing category-based DB query runs. Nothing about
account_metric_selections or metric_catalog changes.

Azure's actual cost shape (see metrics_collector.py's own docstring):
platform-metric reads are FREE up to 1,000,000 API calls/month/billing
account (Microsoft-confirmed) -- there is no per-call bill to protect at
this app's scale. So CRITICAL here is tiered for FRESHNESS, matching Azure
Monitor's real ~1-min publish rate for these metrics (see the workbook's
'Real Publish Rate' column), not for cost avoidance the way AWS's 2-min
critical tier is. LOW/SLOW-EXTENDED still exist to avoid polling
latency-insensitive services as often as latency-sensitive ones for no
freshness benefit, and to keep call volume away from the 1M/month ceiling
as more extended services get enabled (same reasoning already documented
in multicloud_scheduler.py before this patch).

Any (service, metric_name) not listed in CRITICAL_METRICS or LOW_METRICS,
but whose service is category='core' in metric_catalog_data.CURATED, falls
through to STANDARD (5 min, unchanged from today's flat core cadence).
Any 'extended' service not listed in SLOW_EXTENDED_SERVICES stays on the
existing 15-min extended cadence.
"""

# ── CORE tier: alertable / latency-sensitive -> poll near Azure Monitor's
#    real ~1-min publish rate instead of waiting for the old flat 5-min core
#    cycle. Free to do (see module docstring), and matches this app's own
#    "Recommended Default? = Yes" flags in the metric catalog for the
#    metrics that most directly indicate an outage or failure in progress.
CRITICAL_METRICS = {
    "vm":              {"Percentage CPU", "VM Availability Metric"},
    "storage_account": {"Availability"},
    "sql_database":    {"cpu_percent", "connection_failed"},
    "app_service":     {"Http5xx", "HealthCheckStatus"},
}

# ── CORE tier: trend/diagnostic signals -> no freshness lost by polling
#    slower than the 5-min standard core cadence (nobody alerts on these
#    within a 5-min window in practice, matching AWS's EC2 Disk /
#    Lambda-Invocations "low" precedent in runner.py).
LOW_METRICS = {
    "vm": {
        "Disk Read Bytes", "Disk Write Bytes",
        "Disk Read Operations/Sec", "Disk Write Operations/Sec",
        "CPU Credits Remaining", "OS Disk Queue Depth",
    },
    "storage_account": {"Ingress", "Egress", "SuccessE2ELatency"},
    "sql_database":    {"connection_successful", "deadlock", "blocked_by_firewall"},
    "app_service":     {"CpuTime", "MemoryWorkingSet", "Http4xx"},
}

# ── EXTENDED tier split: services whose real Azure-side publish rate is
#    already 5 min (not 1 min) per the workbook, or whose data changes
#    rarely enough (cert/pipeline/vault-config-adjacent signals) that
#    slowing to 60 min loses nothing a user would notice, while directly
#    reducing call volume as more extended services get enabled --
#    the same "not every extended service deserves the same cadence"
#    principle AWS applies inside its own extended tier.
SLOW_EXTENDED_SERVICES = {
    "vpn_gateway",     # TunnelAverageBandwidth etc. publish at 5 min, not 1 min
    "managed_disk",    # Composite Disk metrics publish at 5 min, not 1 min
    "data_factory",    # pipeline-run-dependent, not a fixed short interval
    "cdn_profile",     # may require Front Door's advanced-metrics tier for full 1-min set
    "key_vault",       # published only around actual vault API calls -- bursty, not steady 1-min
}


def _service_metric_names(curated, service_key):
    _, _, _category, metrics = curated[service_key]
    return {m[0] for m in metrics}


def build_standard_metrics(curated):
    """{service_key: {metric_name,...}} for every 'core' service, minus
    whatever's already claimed by CRITICAL_METRICS/LOW_METRICS for that
    service. Computed from CURATED rather than hand-duplicated so a new
    core metric added to metric_catalog_data.py defaults into STANDARD
    (today's existing 5-min behavior) until someone deliberately re-tiers
    it -- never silently dropped or silently promoted to CRITICAL."""
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


def build_extended_fast_metrics(curated):
    """{service_key: {metric_name,...}} for 'extended' services NOT in
    SLOW_EXTENDED_SERVICES -- stays on the existing 15-min extended cadence."""
    out = {}
    for service_key, (_, _, category, _metrics) in curated.items():
        if category == "extended" and service_key not in SLOW_EXTENDED_SERVICES:
            out[service_key] = _service_metric_names(curated, service_key)
    return out


def build_extended_slow_metrics(curated):
    """{service_key: {metric_name,...}} for 'extended' services IN
    SLOW_EXTENDED_SERVICES -- moved to a 60-min cadence."""
    out = {}
    for service_key, (_, _, category, _metrics) in curated.items():
        if category == "extended" and service_key in SLOW_EXTENDED_SERVICES:
            out[service_key] = _service_metric_names(curated, service_key)
    return out
