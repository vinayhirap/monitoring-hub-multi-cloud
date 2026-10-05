#!/usr/bin/env python3
"""
scripts/generate_metric_labels.py

Generates frontend/src/utils/metricLabels.generated.js: the label shown for every metric,
keyed by the name the app STORES (metrics.metric_name / alerts.metric_name).

WHY GENERATED: labels used to be hand-typed in the frontend and drifted from the catalogue
(raw keys such as "disk_used_percent" or "bytesouttodestination" reached the UI). The seed
catalogues (app/aws|azure|gcp metric_catalog_data.py) are the source of truth for which
metrics exist, so the label for each one is derived from its OFFICIAL catalogue name here,
and tests/test_metric_labels.py fails if

  * a catalogue metric has no label,
  * a label breaks the style rules (Title Case, no underscores/slashes, no run-together words),
  * two catalogue entries that share a stored name would get different labels, or
  * the generated file is out of date.

Adding a metric to a catalogue therefore either "just works" (CamelCase / snake_case names are
split automatically) or the test tells you to add an entry to OVERRIDES below.

    python3 scripts/generate_metric_labels.py          # rewrite the file
    python3 scripts/generate_metric_labels.py --check  # exit 1 if it is out of date
"""
import importlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "frontend", "src", "utils", "metricLabels.generated.js")
OUT_PY = os.path.join(ROOT, "app", "metric_labels_generated.py")

# The tokenizer, acronym and word tables live in app/metric_labels.py so the backend (RCA reports, emails) uses the
# SAME wording as the UI. Re-exported here because tests/test_metric_labels.py reads them from this module.
sys.path.insert(0, ROOT)
from app.metric_labels import ACRONYMS, LOWER_WORDS, WORD_MAP, humanize  # noqa: E402,F401

# Exceptions where splitting the official name is not the wording an operator should read.
# Keyed by the STORED (lower-case) name. Keep this list short and explain each entry.
OVERRIDES = {
    # CloudWatch-Agent metrics: the agent's snake_case names say "used percent"; we show the concept
    "mem_used_percent": "Memory Utilization",
    "disk_used_percent": "Disk Utilization",
    # ELB: the CloudWatch names are HTTPCode_* / TargetResponseTime; the collector stores short aliases
    "errors5xx": "Target 5xx Errors",
    "httpcode_target_5xx_count": "Target 5xx Errors",
    "httpcode_target_4xx_count": "Target 4xx Errors",
    "httpcode_elb_5xx_count": "Load Balancer 5xx Errors",
    "httpcode_elb_4xx_count": "Load Balancer 4xx Errors",
    "responselatency": "Target Response Time",
    "healthyhosts": "Healthy Hosts",
    "unhealthyhosts": "Unhealthy Hosts",
    "healthyhosts_describe": "Healthy Hosts",
    "unhealthyhosts_describe": "Unhealthy Hosts",
    # EC2 status checks read better with the scope first
    "statuscheckfailed": "Status Check Failed",
    "statuscheckfailed_instance": "Instance Status Check Failed",
    "statuscheckfailed_system": "System Status Check Failed",
    # Azure's official name is "Percentage CPU"; same concept and wording as AWS/GCP
    "percentage cpu": "CPU Utilization",
    "memutilization": "Memory Utilization",
    # names the tokenizer cannot split (no separators/capitals in the official name) or that read badly
    "cachehits": "Cache Hits", "cachemisses": "Cache Misses",
    "connectedclients": "Connected Clients", "clients/connected": "Connected Clients",
    "evictedkeys": "Evicted Keys",
    "unhealthyhostcount": "Unhealthy Host Count",
    "currconnections": "Current Connections",
    "binlogdiskusage": "Binary Log Disk Usage",
    "apiserver_current_inflight_requests": "API Server Current In-Flight Requests",
    "p2sconnectioncount": "P2S Connection Count",
    "successe2elatency": "Success End-to-End Latency",
    "dtu_consumption_percent": "DTU Consumption",
    "percentagediskspaceused": "Disk Space Utilization",
    "volumeconsumedreadwriteops": "Volume Consumed Read/Write Operations",
    "metadatanotoken": "Metadata Requests Without Token",
    "httpcode_target_2xx_count": "Target 2xx Responses",
    "mysql/replication/seconds_behind_master": "MySQL Replication Lag (Seconds)",
    "postgresql/replication/replica_byte_lag": "PostgreSQL Replication Lag (Bytes)",
    "cpu/utilizations": "CPU Utilization", "memory/utilizations": "Memory Utilization",
    "stats/cpu_utilization": "Redis CPU Utilization", "stats/cache_hit_ratio": "Cache Hit Ratio",
    "stats/memory/usage_ratio": "Memory Usage Ratio", "storage/stored_bytes": "Stored Bytes",
    # names the app generates itself (not in any catalogue)
    "multivariate_anomaly": "Multivariate Anomaly",
    "synthetic_uptime": "Synthetic Check Availability",
}


def catalogue_metrics():
    """Yield (provider, service_key, official_name, stored_name)."""
    sys.path.insert(0, ROOT)
    from app.threshold_defaults import resolve_db_metric_name
    for prov, mod in (("aws", "app.aws.metric_catalog_data"), ("azure", "app.providers.azure.metric_catalog_data"),
                      ("gcp", "app.providers.gcp.metric_catalog_data")):
        curated = importlib.import_module(mod).CURATED
        for service, entry in curated.items():
            for m in entry[3]:
                yield prov, service, m[0], resolve_db_metric_name(service, m[0])


def percent_metrics():
    """Stored names of catalogue metrics whose unit is Percent (values shown with a % sign)."""
    sys.path.insert(0, ROOT)
    from app.threshold_defaults import resolve_db_metric_name
    out = set()
    for mod in ("app.aws.metric_catalog_data", "app.providers.azure.metric_catalog_data",
                "app.providers.gcp.metric_catalog_data"):
        for service, entry in importlib.import_module(mod).CURATED.items():
            for m in entry[3]:
                if str(m[1]).lower() == "percent":
                    out.add(resolve_db_metric_name(service, m[0]))
    return sorted(out)


def integer_metrics():
    """Stored names of catalogue COUNT metrics (unit Count, minus fractional gauges): shown and limited as whole numbers."""
    sys.path.insert(0, ROOT)
    from app.threshold_defaults import is_integer_metric, resolve_db_metric_name
    out = set()
    for mod in ("app.aws.metric_catalog_data", "app.providers.azure.metric_catalog_data",
                "app.providers.gcp.metric_catalog_data"):
        for service, entry in importlib.import_module(mod).CURATED.items():
            for m in entry[3]:
                name = resolve_db_metric_name(service, m[0])
                if is_integer_metric(m[1], name):
                    out.add(name)
    return sorted(out)


def metric_units():
    """stored metric name -> catalogue unit ('Bytes', 'Count', 'Seconds', ...), for report wording."""
    sys.path.insert(0, ROOT)
    from app.threshold_defaults import resolve_db_metric_name
    out = {}
    for mod in ("app.aws.metric_catalog_data", "app.providers.azure.metric_catalog_data",
                "app.providers.gcp.metric_catalog_data"):
        for service, entry in importlib.import_module(mod).CURATED.items():
            for m in entry[3]:
                out.setdefault(resolve_db_metric_name(service, m[0]), str(m[1]))
    return dict(sorted(out.items()))


def build():
    """-> (labels {stored_name: label}, conflicts [(stored, {labels})])"""
    seen, labels, conflicts = {}, {}, []
    for prov, service, official, stored in catalogue_metrics():
        label = OVERRIDES.get(stored) or OVERRIDES.get(official.lower()) or humanize(official)
        seen.setdefault(stored, {})[label] = (prov, service, official)
    for stored, opts in sorted(seen.items()):
        if len(opts) > 1:
            conflicts.append((stored, {k: v for k, v in opts.items()}))
        labels[stored] = sorted(opts)[0]
    for stored, label in OVERRIDES.items():          # overrides for names outside the catalogues
        labels.setdefault(stored, label)
    return dict(sorted(labels.items())), conflicts


def render(labels):
    body = ",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in labels.items())
    pct = ",\n".join(f"  {json.dumps(k)}" for k in percent_metrics())
    ints = ",\n".join(f"  {json.dumps(k)}" for k in integer_metrics())
    return ("// AUTO-GENERATED by scripts/generate_metric_labels.py from the seed metric catalogues.\n"
            "// Do not edit by hand: add an entry to OVERRIDES in that script, then re-run it.\n"
            "export const GENERATED_METRIC_LABELS = {\n" + body + ",\n};\n\n"
            "// Metrics whose catalogue unit is Percent: their values are shown with a % sign.\n"
            "export const GENERATED_PERCENT_METRICS = [\n" + pct + ",\n];\n\n"
            "// COUNT metrics (catalogue unit Count, minus fractional gauges): values and limits are whole numbers.\n"
            "export const GENERATED_INTEGER_METRICS = [\n" + ints + ",\n];\n")


def render_py(labels):
    body = "\n".join(f"    {k!r}: {v!r}," for k, v in labels.items())
    pct = "\n".join(f"    {k!r}," for k in percent_metrics())
    ints = "\n".join(f"    {k!r}," for k in integer_metrics())
    units = "\n".join(f"    {k!r}: {v!r}," for k, v in metric_units().items())
    return ("# AUTO-GENERATED by scripts/generate_metric_labels.py from the seed metric catalogues.\n"
            "# Do not edit by hand: add an entry to OVERRIDES in that script, then re-run it.\n"
            "LABELS = {\n" + body + "\n}\n\n"
            "# Metrics whose catalogue unit is Percent: their values are shown with a % sign.\n"
            "PERCENT_METRICS = (\n" + pct + "\n)\n\n"
            "# COUNT metrics (catalogue unit Count, minus fractional gauges): values and limits are whole numbers.\n"
            "INTEGER_METRICS = (\n" + ints + "\n)\n\n"
            "# Catalogue unit per stored metric name (report wording).\n"
            "UNITS = {\n" + units + "\n}\n")


if __name__ == "__main__":
    labels, conflicts = build()
    text, text_py = render(labels), render_py(labels)
    if "--check" in sys.argv:
        cur = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
        cur_py = open(OUT_PY, encoding="utf-8").read() if os.path.exists(OUT_PY) else ""
        sys.exit(0 if cur == text and cur_py == text_py and not conflicts else 1)
    open(OUT, "w", encoding="utf-8").write(text)
    open(OUT_PY, "w", encoding="utf-8").write(text_py)
    print(f"wrote {len(labels)} labels to {os.path.relpath(OUT, ROOT)} and {os.path.relpath(OUT_PY, ROOT)}; {len(conflicts)} conflicts")
