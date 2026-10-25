#!/usr/bin/env python3
"""
READ-ONLY polling audit: every scheduled metric on AWS, Azure and GCP, the interval it is polled at today,
and whether that interval fits what the metric is for.  Touches no database and calls no cloud API; it reads
the real catalogs and tier maps, so the numbers cannot drift from what the collectors do.

    python3 scripts/polling_audit.py                       # summary + writes docs/polling_inventory.csv
    python3 scripts/polling_audit.py --counts '{"ec2":40}' # resource counts -> modelled cost of the demotions
    python3 scripts/polling_audit.py --check               # exit 1 on any promotion/guardrail failure (see GUARDRAILS)
    python3 scripts/polling_audit.py --check --strict      # ...and also on KNOWN_OPEN findings (runtime tier still wrong)
    python3 scripts/polling_audit.py --report              # per metric: paid/free, cost units per resource per day

The two lists below are the audit's findings (docs/POLLING_AUDIT_2026-10-19.md):

  PROMOTIONS  failure / saturation signals that were polled slower than their siblings. Applied in code.
              --check proves each one still exists in the catalog and is at the intended interval.
  DEMOTIONS   pure traffic-volume meters polled every 5 min. NOT applied: the saving is a few cents a month
              at today's fleet. Listed so a fleet that grows can switch them on knowingly.

GUARDRAILS (Phase B1, all run by --check; none touches app code, the database or a cloud API):

  publish_interval   a metric is never polled faster than the cloud publishes it (PUBLISH_TABLE, one source per entry)
  daily_fast_tier    a daily publisher is never on the critical or standard tier (also checks polling_model's
                     AWS_EXTENDED_PERIOD_OVERRIDES agree with this table)
  p1_set             every key evaluated on the 2-min tick resolves to a catalog metric polled at <= 120 s that the
                     cloud publishes at <= 120 s, and every critical-tier metric is in the P1 set
  env_invariants     with ENV_POLLING on: effective_interval never < base, wafv2 never slowed, base >= 24 h never
                     slowed, PROD (and any unplaced label) unchanged, NON-PROD intervals per the POLL_NONPROD_* settings, polling_model.base_interval_seconds()
                     agrees with the catalog tier
  staleness          STALE_MIN / EVAL_WINDOW exceed the interval for every base and every env-slowed interval
  period_vs_publish  GetMetricData Period is never finer than the native publish interval (timestamp shift bug class)
  expiry_static      expiry metrics stay static-only
  classified         every scheduled metric has a PUBLISH_TABLE entry; no entry is dead
  tier_map_in_catalog every name in a tier allowlist / AWS override exists in the curated catalog (no phantom polls)

Evidence levels in PUBLISH_TABLE: verified (vendor page read 2026-10-19), secondary (third-party mirror of the vendor
list), repo (taken from a comment in this repo's own code, not re-checked), unverified. Only the first three can FAIL a
rule; an unverified entry that would violate a rule is printed as a WARNING and counted, never guessed into a failure.
"""
import argparse
import contextlib
import csv
import json
import math
import os
import sys
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collector import polling_model as pm  # noqa: E402
from app.collector import env_polling as ep  # noqa: E402
from app.aws.metric_catalog_data import CURATED as AWS  # noqa: E402
from app.providers.azure.metric_catalog_data import CURATED as AZ  # noqa: E402
from app.providers.gcp.metric_catalog_data import CURATED as GCP  # noqa: E402
from app.providers.azure import severity_tiers as az  # noqa: E402
from app.providers.gcp import severity_tiers as gcp  # noqa: E402
from app.threshold_defaults import resolve_db_metric_name, is_static_only_metric  # noqa: E402

LOW = 900
STANDARD = 300

# (provider, service, metric) -> interval it SHOULD be polled at (seconds)
PROMOTIONS = {
    ("aws", "apigateway", "Latency"): LOW,
    ("aws", "apigateway", "IntegrationLatency"): LOW,
    ("aws", "elasticache", "ReplicationLag"): LOW,
    ("aws", "transitgateway", "PacketDropCountBlackhole"): LOW,
    ("aws", "transitgateway", "PacketDropCountNoRoute"): LOW,
    ("aws", "documentdb", "CPUUtilization"): LOW,
    ("aws", "documentdb", "FreeableMemory"): LOW,
    ("aws", "neptune", "CPUUtilization"): LOW,
    ("aws", "redshift", "CPUUtilization"): LOW,
    ("aws", "opensearch", "CPUUtilization"): LOW,
    ("aws", "opensearch", "FreeStorageSpace"): LOW,
    ("aws", "memorydb", "CPUUtilization"): LOW,
    ("aws", "dms", "CDCLatencySource"): LOW,
    ("aws", "dms", "CDCLatencyTarget"): LOW,
    ("aws", "cloudfront", "5xxErrorRate"): LOW,
    ("aws", "cloudfront", "TotalErrorRate"): LOW,
    ("azure", "aks_cluster", "kube_node_status_condition"): STANDARD,
    ("gcp", "gke_node", "cpu/allocatable_utilization"): LOW,
    ("gcp", "gke_node", "memory/allocatable_utilization"): LOW,
    ("gcp", "gke_node", "ephemeral_storage/used_bytes"): LOW,
    ("gcp", "spanner_instance", "instance/cpu/utilization"): LOW,
    ("gcp", "spanner_instance", "api/request_latencies"): LOW,
    ("gcp", "redis_instance", "stats/cpu_utilization"): LOW,
    ("gcp", "nat_gateway", "dropped_sent_packets_count"): LOW,
    ("gcp", "nat_gateway", "port_usage"): LOW,
}

# Traffic-volume meters on the 5-min tier that no incident depends on. Not applied.
DEMOTIONS = {
    ("aws", "elb", "RequestCount"): STANDARD,   # critical tier, but its default threshold is a placeholder: anomaly-only, never above WARNING
    ("aws", "ec2", "NetworkIn"): LOW,
    ("aws", "ec2", "NetworkOut"): LOW,
    ("aws", "elb", "ActiveConnectionCount"): LOW,
    ("aws", "elb", "NewConnectionCount"): LOW,
    ("aws", "elb", "ActiveFlowCount"): LOW,
    ("aws", "elb", "NewFlowCount"): LOW,
    ("aws", "elb", "ProcessedBytes"): LOW,
    ("azure", "vm", "Network In Total"): LOW,
    ("azure", "vm", "Network Out Total"): LOW,
    ("azure", "storage_account", "Transactions"): LOW,
    ("gcp", "compute_instance", "network/received_bytes_count"): LOW,
    ("gcp", "compute_instance", "network/sent_bytes_count"): LOW,
    ("gcp", "gcs_bucket", "api/request_count"): LOW,
    ("gcp", "cloud_run_service", "request_count"): LOW,
    ("gcp", "cloud_run_service", "instance_count"): LOW,
}

# $ per 1,000 metrics requested (CloudWatch GetMetricData list price, US East). Azure platform-metric reads
# are inside Microsoft's free allowance (see azure/severity_tiers.py) and GCP is not modelled.
AWS_PRICE_PER_1000 = 0.01   # NOT re-confirmed on the bill in Phase B1. Used only by the pre-existing DEMOTIONS/PROMOTIONS $ lines below;
#                             the new B1 cost report is in units (metrics requested / calls / series), never dollars.


# ── Publish-interval table (guardrail 1) ────────────────────────────────────────────────────────────────────
# How often the CLOUD publishes a new datapoint, in seconds. (provider, service, metric-or-"*", seconds, status, source).
# Lookup: exact metric first, then the service-wide "*" entry. A scheduled metric with neither is a `classified` failure.
#   verified    vendor page read on 2026-10-19 (URL in the source column)
#   secondary   third-party mirror of the vendor's metric list (the vendor page was not reachable that way)
#   repo        taken from a comment in this repo's own code (file named in the source column); NOT re-checked
#   unverified  could not be confirmed; the figure is the best guess and is used for WARNINGS only, never to fail
V, SEC, REPO, UNV = "verified", "secondary", "repo", "unverified"
FAIL_STATUSES = (V, SEC, REPO)
DAY = 86400
_PM = "app/collector/polling_model.py"
_AZT = "app/providers/azure/severity_tiers.py"
_UNV_AWS = "unverified: assumed standard CloudWatch 1-min publish for this service; check the service's CloudWatch metrics page"
_UNV_AZ = "unverified: assumed Azure Monitor platform-metric 1-min grain; check the service's supported-metrics page"
_UNV_GCP = "unverified: assumed Cloud Monitoring 60 s sampling; check the service's metrics page"

PUBLISH_TABLE = [
    # AWS -- core
    ("aws", "ec2", "*", 300, REPO, _PM + " AWS_CORE_METRICS: EC2 basic monitoring publishes 5-min points"),
    ("aws", "ec2", "mem_used_percent", 60, REPO, _PM + " CWAGENT_*: CWAgent default publish = 60 s"),
    ("aws", "ec2", "disk_used_percent", 60, REPO, _PM + " CWAGENT_*: CWAgent default publish = 60 s"),
    ("aws", "ec2", "statuscheckfailed", 60, REPO, _PM + " DESCRIBE_POLL_SECONDS: Describe API, live state, polled every 60 s"),
    ("aws", "ebs", "*", 300, REPO, _PM + " AWS_CORE_METRICS: all standard EBS metrics publish on the 5-min basic cadence"),
    ("aws", "rds", "*", 60, REPO, _PM + " AWS_CORE_METRICS: RDS is a 1-min source"),
    ("aws", "elb", "*", 60, REPO, _PM + " AWS_CORE_METRICS: ALB is a 1-min source (published only while traffic flows)"),
    ("aws", "elb", "healthyhosts_describe", 60, REPO, _PM + " DESCRIBE_POLL_SECONDS: Describe API, polled every 60 s"),
    ("aws", "elb", "unhealthyhosts_describe", 60, REPO, _PM + " DESCRIBE_POLL_SECONDS: Describe API, polled every 60 s"),
    ("aws", "elb", "ActiveFlowCount", 60, UNV, "unverified: NLB (AWS/NetworkELB); the repo only documents ALB as 1-min"),
    ("aws", "elb", "NewFlowCount", 60, UNV, "unverified: NLB (AWS/NetworkELB); the repo only documents ALB as 1-min"),
    ("aws", "elb", "ProcessedBytes", 60, UNV, "unverified: NLB (AWS/NetworkELB); the repo only documents ALB as 1-min"),
    ("aws", "elb", "TCP_Client_Reset_Count", 60, UNV, "unverified: NLB (AWS/NetworkELB); the repo only documents ALB as 1-min"),
    ("aws", "lambda", "*", 60, REPO, _PM + " AWS_CORE_METRICS: Lambda is a 1-min source"),
    # AWS -- daily publishers
    ("aws", "s3", "BucketSizeBytes", DAY, REPO, _PM + " AWS_EXTENDED_PERIOD_OVERRIDES: AWS-confirmed daily-only publish (2026-09-12 audit)"),
    ("aws", "s3", "NumberOfObjects", DAY, REPO, _PM + " AWS_EXTENDED_PERIOD_OVERRIDES: AWS-confirmed daily-only publish (2026-09-12 audit)"),
    ("aws", "s3", "*", 60, REPO, _PM + " AWS_EXTENDED_PERIOD_OVERRIDES comment: S3 request metrics stay 1-min-native"),
    ("aws", "certificatemanager", "*", DAY, REPO, _PM + " AWS_EXTENDED_PERIOD_OVERRIDES: DaysToExpiry is a computed daily countdown"),
    ("aws", "kms", "*", DAY, REPO, _PM + " AWS_EXTENDED_PERIOD_OVERRIDES: SecondsUntilKeyMaterialExpiration is a computed daily countdown"),
    # AWS -- WAF (the doc URL is quoted in polling_model.py)
    ("aws", "wafv2", "*", 60, REPO, _PM + " WAF block: AWS WAF publishes AWS/WAFV2 metrics once a minute, nonzero values only "
                                         "(https://docs.aws.amazon.com/waf/latest/developerguide/waf-metrics.html)"),
    # AWS -- extended services, none of these figures re-checked
    ("aws", "apigateway", "*", 60, UNV, _UNV_AWS),
    ("aws", "autoscaling", "*", 60, UNV, _UNV_AWS + " (group metrics need to be enabled on the group)"),
    ("aws", "backup", "*", 300, UNV, "unverified: believed 5-min; activity-driven per " + _PM),
    ("aws", "cloudfront", "*", 60, UNV, _UNV_AWS + " (activity-driven per " + _PM + ")"),
    ("aws", "cognito", "*", 300, UNV, "unverified: believed 5-min"),
    ("aws", "dax", "*", 60, UNV, _UNV_AWS),
    ("aws", "directconnect", "*", 300, UNV, "unverified: believed 5-min"),
    ("aws", "dms", "*", 60, UNV, _UNV_AWS),
    ("aws", "documentdb", "*", 60, UNV, _UNV_AWS),
    ("aws", "dynamodb", "*", 60, UNV, _UNV_AWS),
    ("aws", "ecs", "*", 60, UNV, _UNV_AWS + " (Container Insights for Running/PendingTaskCount)"),
    ("aws", "efs", "*", 60, UNV, _UNV_AWS),
    ("aws", "efs", "StorageBytes", 900, UNV, "unverified: believed 15-min"),
    ("aws", "eks", "*", 60, UNV, _UNV_AWS + " (Container Insights)"),
    ("aws", "elasticache", "*", 60, UNV, _UNV_AWS),
    ("aws", "events", "*", 60, UNV, _UNV_AWS),
    ("aws", "firehose", "*", 60, UNV, _UNV_AWS),
    ("aws", "globalaccelerator", "*", 60, UNV, _UNV_AWS),
    ("aws", "kinesis", "*", 60, UNV, _UNV_AWS),
    ("aws", "logs", "*", 60, UNV, _UNV_AWS + " (activity-driven per " + _PM + ")"),
    ("aws", "memorydb", "*", 60, UNV, _UNV_AWS),
    ("aws", "msk", "*", 60, UNV, _UNV_AWS + " (DEFAULT monitoring level)"),
    ("aws", "natgateway", "*", 60, UNV, _UNV_AWS),
    ("aws", "neptune", "*", 60, UNV, _UNV_AWS),
    ("aws", "opensearch", "*", 60, UNV, _UNV_AWS),
    ("aws", "redshift", "*", 60, UNV, _UNV_AWS),
    ("aws", "route53", "*", 60, UNV, _UNV_AWS),
    ("aws", "sns", "*", 60, UNV, _UNV_AWS),
    ("aws", "sqs", "*", 60, UNV, _UNV_AWS),
    ("aws", "states", "*", 60, UNV, _UNV_AWS),
    ("aws", "transitgateway", "*", 60, UNV, _UNV_AWS),
    ("aws", "vpn", "*", 300, UNV, "unverified: believed 5-min"),
    # Azure
    ("azure", "vm", "VM Availability Metric", 60, REPO, _AZT + " CRITICAL_METRICS: Azure Monitor ~1-min publish for availability/error signals"),
    ("azure", "storage_account", "Availability", 60, REPO, _AZT + " CRITICAL_METRICS: Azure Monitor ~1-min publish for availability/error signals"),
    ("azure", "sql_database", "connection_failed", 60, REPO, _AZT + " CRITICAL_METRICS: Azure Monitor ~1-min publish for availability/error signals"),
    ("azure", "app_service", "Http5xx", 60, REPO, _AZT + " CRITICAL_METRICS: Azure Monitor ~1-min publish for availability/error signals"),
    ("azure", "app_service", "HealthCheckStatus", 60, REPO, _AZT + " CRITICAL_METRICS: Azure Monitor ~1-min publish for availability/error signals"),
    ("azure", "storage_account", "UsedCapacity", 3600, V, "https://learn.microsoft.com/en-ca/azure/azure-monitor/reference/supported-metrics/"
                                                         "microsoft-storage-storageaccounts-metrics (UsedCapacity, time grain PT1H only); "
                                                         "also the comment in " + _AZT),
    ("azure", "vpn_gateway", "*", 300, REPO, _AZT + " SLOW_EXTENDED_SERVICES: publishes at 5 min, not 1 min"),
    ("azure", "managed_disk", "*", 300, REPO, _AZT + " SLOW_EXTENDED_SERVICES: Composite Disk metrics publish at 5 min, not 1 min"),
    ("azure", "cosmosdb_account", "DataUsage", 300, UNV, "unverified: believed PT5M"),
] + [("azure", svc, "*", 60, UNV, _UNV_AZ) for svc in (
    "aks_cluster", "app_service", "application_gateway", "cdn_profile", "container_instance", "cosmosdb_account",
    "data_factory", "eventhub_namespace", "function_app", "key_vault", "load_balancer", "redis_cache",
    "service_bus_namespace", "sql_database", "storage_account", "vm", "vmss")] + [
    # GCP
    ("gcp", "gcs_bucket", "total_bytes", DAY, V, "https://cloud.google.com/storage/docs/getting-bucket-size: bucket size is measured once a day; "
                                                  "Cloud Monitoring repeats the value at each sampling interval"),
    ("gcp", "gcs_bucket", "object_count", DAY, V, "https://cloud.google.com/storage/docs/getting-bucket-size: measured once a day"),
    ("gcp", "gcs_bucket", "total_byte_seconds", DAY, V, "https://cloud.google.com/storage/docs/getting-bucket-size: measured once a day"),
    ("gcp", "bigquery_project", "storage/stored_bytes", 1800, SEC, "bigquery.googleapis.com/storage/stored_bytes 'Sampled every 1800 seconds' "
                                                                  "(mirror of Google's list: docs.opsramp.com google-bigquery-int)"),
    ("gcp", "cloudsql_instance", "up", 60, UNV, "unverified: Google's Cloud SQL metrics page lists Cloud SQL metrics at 60 s but the page extract "
                                                "could not be tied to the `up` row (https://cloud.google.com/sql/docs/mysql/admin-api/metrics)"),
] + [("gcp", svc, "*", 60, UNV, _UNV_GCP) for svc in (
    "bigquery_project", "cloud_lb", "cloud_run_service", "cloudfunctions_function", "cloudsql_instance",
    "compute_instance", "firestore_database", "gce_persistent_disk", "gcs_bucket", "gke_cluster", "gke_node",
    "nat_gateway", "pubsub_subscription", "pubsub_topic", "redis_instance", "spanner_instance")]


def _build_publish(table):
    exact, svc = {}, {}
    for prov, service, metric, sec, status, source in table:
        if status not in (V, SEC, REPO, UNV):
            raise ValueError(f"PUBLISH_TABLE: bad status {status!r} for {(prov, service, metric)}")
        if status in FAIL_STATUSES and not source.strip():
            raise ValueError(f"PUBLISH_TABLE: {(prov, service, metric)} is {status} but has no source")
        d, key = (svc, (prov, service)) if metric == "*" else (exact, (prov, service, metric))
        if key in d:
            raise ValueError(f"PUBLISH_TABLE: duplicate entry {key}")
        d[key] = (int(sec), status, source)
    return exact, svc


_PUBLISH_EXACT, _PUBLISH_SVC = _build_publish(PUBLISH_TABLE)


def publish_for(provider, service, metric):
    """(seconds, status, source) or None when the metric is not in PUBLISH_TABLE."""
    return _PUBLISH_EXACT.get((provider, service, metric)) or _PUBLISH_SVC.get((provider, service))


# Metrics a cloud query never bills: derived locally (never sent) or never sent because only an account-level series exists.
def _never_sent(provider, service, metric):
    return provider == "aws" and ((service, metric) in pm.AWS_EXTENDED_DERIVED or (service, metric) in pm.AWS_EXTENDED_UNSUPPORTED)


AZURE_BATCH = 50      # app/providers/azure/metrics_collector.py _BATCH_SIZE: one call covers up to 50 resources and ALL metrics of a service+tier


def billing(provider, service, metric, source, interval_s):
    """(billing class, billed unit, units per resource per day, note) for one scheduled metric.
    Units, never dollars: prices change and are confirmed on the bill, not hard-coded here."""
    polls = 86400.0 / interval_s
    if source.startswith("describe"):
        return "free", "none", 0.0, "Describe API (free); about %d calls/day per account+region, not per metric" % (86400 // pm.DESCRIBE_POLL_SECONDS)
    if _never_sent(provider, service, metric):
        return "free", "none", 0.0, "never sent to CloudWatch (derived locally or only published at account level)"
    if provider == "aws":
        note = ""
        if (service, metric) in pm.AWS_SEARCH_METRICS:
            note = "SEARCH expression: billed per matching series, the collector counts 1 (a floor), real units may be higher"
        return "paid", "metrics_requested", polls, note
    if provider == "azure":
        return ("paid_after_free_tier", "api_calls", polls / AZURE_BATCH,
                "amortised over a full batch of %d resources; one call per service+tier pass is shared by every metric of that "
                "service in the tier, so do not sum this column per metric; worst case (1 resource) = %.0f calls/day" % (AZURE_BATCH, polls))
    return ("paid_after_free_tier", "series_returned", polls,
            "ListTimeSeries is billed per series returned; modelled as 1 series per resource per poll")


def inventory():
    rows = []
    for m in pm.AWS_CORE_METRICS:
        rows.append(("aws", m.resource_type, m.cw_name, m.db_name, m.tier, pm.TIER_SECONDS[m.tier], "core"))
    rows.append(("aws", "ec2", "mem_used_percent", "mem_used_percent", pm.CWAGENT_MEM_TIER,
                 pm.TIER_SECONDS[pm.CWAGENT_MEM_TIER], "cwagent"))
    rows.append(("aws", "ec2", "disk_used_percent", "disk_used_percent", pm.CWAGENT_DISK_TIER,
                 pm.TIER_SECONDS[pm.CWAGENT_DISK_TIER], "cwagent"))
    for rt, db in sorted(pm.AWS_DESCRIBE_METRICS):
        rows.append(("aws", rt, db, db, "describe", pm.DESCRIBE_POLL_SECONDS, "describe API (free)"))
    for svc, (_d, _ns, cat, metrics) in AWS.items():
        if cat != "extended" or svc == "nlb":
            continue
        for (name, _u, _s, _dflt, _desc) in metrics:
            t = pm.aws_extended_tier(svc, name)
            rows.append(("aws", svc, name, resolve_db_metric_name(svc, name), t, pm.TIER_SECONDS[t], "extended"))
    for prov, cur, mod in (("azure", AZ, az), ("gcp", GCP, gcp)):
        for (svc, metric), t in sorted(mod.metric_tiers(cur).items()):
            rows.append((prov, svc, metric, metric.lower(), t, mod.TIER_INTERVAL_SECONDS[t], "catalog"))
    p1 = pm.p1_metric_keys()
    gates = {(m.resource_type, m.cw_name): m.gate for m in pm.AWS_CORE_METRICS}
    out = []
    for prov, svc, cw, db, tier, sec, src in rows:
        pub = publish_for(prov, svc, cw)
        klass, unit, units_day, note = billing(prov, svc, cw, src, sec)
        gate = (gates.get((svc, cw)) or "") if prov == "aws" else ""
        if gate:
            note = (note + "; " if note else "") + f"only resources passing the '{gate}' gate are queried"
        out.append({"provider": prov, "service": svc, "metric": cw, "db_metric": db, "tier": tier,
                    "interval_s": sec, "source": src, "evaluated_every_2min": (svc, db.lower()) in p1,
                    "static_only": is_static_only_metric(db),
                    "publish_s": pub[0] if pub else None, "publish_status": pub[1] if pub else "unclassified",
                    "billing": klass, "billed_unit": unit, "units_per_resource_day": round(units_day, 2), "cost_note": note})
    return out


def _index(rows):
    return {(r["provider"], r["service"], r["metric"]): r for r in rows}


def check_list(name, wanted, idx, direction):
    """direction 'faster' = wanted interval must be < today's; 'applied' = must equal wanted."""
    problems = []
    for key, target in wanted.items():
        r = idx.get(key)
        if r is None:
            problems.append(f"{name}: {key} is not in the catalog")
        elif direction == "applied" and r["interval_s"] != target:
            problems.append(f"{name}: {key} is polled every {r['interval_s']}s, expected {target}s")
        elif direction == "slower" and r["interval_s"] >= target:
            problems.append(f"{name}: {key} is already {r['interval_s']}s, moving to {target}s would not slow it")
    return problems



# ── Guardrails ──────────────────────────────────────────────────────────────────────────────────────────────
Problem = namedtuple("Problem", "rule provider service metric msg")

# Findings on the REAL catalog that need a runtime (app code) change, which this phase must not make. They are reported
# on every run and fail only under --strict. Removing a line is how a later phase closes the finding; a line that no
# longer reproduces is printed as a NOTE so the list cannot rot silently.
_GCS_DAILY = ("polled every 900 s but Google measures it once a day and repeats the value at every sampling interval: 95 of 96 "
              "paid ListTimeSeries polls a day return the same point. Fix in app/providers/gcp/severity_tiers.py (needs a daily tier; "
              "GCP has no slow_extended tier today).")
KNOWN_OPEN = {
    ("publish_interval", "azure", "storage_account", "UsedCapacity"):
        "tier low polls every 900 s but Azure publishes UsedCapacity at PT1H only: 3 of every 4 polls return the same hourly point. "
        "Fix in app/providers/azure/severity_tiers.py (move it to a 3600 s tier).",
    ("publish_interval", "gcp", "gcs_bucket", "total_bytes"): "total_bytes " + _GCS_DAILY,
    ("publish_interval", "gcp", "gcs_bucket", "object_count"): "object_count " + _GCS_DAILY,
    ("publish_interval", "gcp", "gcs_bucket", "total_byte_seconds"): "total_byte_seconds " + _GCS_DAILY,
}

FAST_TIERS = ("critical", "standard")
P1_MAX_SECONDS = 120


@contextlib.contextmanager
def envvars(**kv):
    """Temporarily set (or, with None, remove) environment variables; restores them on exit."""
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def inventory_waf_fast():
    """The same inventory with WAF_FAST_POLLING_ENABLED=true (WAF metrics move to the 5-min / 15-min tiers)."""
    global_cache = pm._BASE_OVERRIDES_CACHE
    try:
        with envvars(WAF_FAST_POLLING_ENABLED="true"):
            return inventory()
    finally:
        pm._BASE_OVERRIDES_CACHE = global_cache


def _p(rule, r, msg):
    return Problem(rule, r["provider"], r["service"], r["metric"], msg)


def _fmt(sec):
    return f"{sec}s" if sec < 3600 else f"{sec / 3600:g}h"


def check_publish_interval(rows):
    """Guardrail 1. -> (problems, warnings). Tier interval < publish interval = polls that cannot return new data."""
    problems, warnings = [], []
    for r in rows:
        pub = r.get("publish_s")
        if pub is None or r["interval_s"] >= pub:
            continue
        msg = (f"polled every {_fmt(r['interval_s'])} (tier {r['tier']}) but the cloud publishes every {_fmt(pub)} "
               f"[{r['publish_status']}]")
        (problems if r["publish_status"] in FAIL_STATUSES else warnings).append(_p("publish_interval", r, msg))
    return problems, warnings


def check_daily_fast_tier(rows, period_overrides=None):
    """Guardrail 2. A daily publisher (>= 24 h) on the critical or standard tier, plus table/override disagreement."""
    problems, warnings = [], []
    overrides = pm.AWS_EXTENDED_PERIOD_OVERRIDES if period_overrides is None else period_overrides
    for r in rows:
        pub = r.get("publish_s")
        if pub is not None and pub >= DAY and r["tier"] in FAST_TIERS:
            msg = f"daily publisher on the {r['tier']} tier ({_fmt(r['interval_s'])}) [{r['publish_status']}]"
            (problems if r["publish_status"] in FAIL_STATUSES else warnings).append(_p("daily_fast_tier", r, msg))
    idx = {(r["provider"], r["service"], r["metric"]): r for r in rows}
    for (svc, metric), period in sorted(overrides.items()):
        r = idx.get(("aws", svc, metric))
        if r is None:
            problems.append(Problem("daily_fast_tier", "aws", svc, metric, "in AWS_EXTENDED_PERIOD_OVERRIDES but not a scheduled metric"))
        elif period >= DAY and (r.get("publish_s") or 0) < DAY:
            problems.append(_p("daily_fast_tier", r, f"polling_model queries it at Period={period}s but the audit table says it publishes "
                                                     f"every {_fmt(r.get('publish_s') or 0)}: one of the two is wrong"))
    return problems, warnings


def check_p1_set(rows, p1_keys=None):
    """Guardrail 3. Every P1 key is a real metric polled and published at <= 2 min; every critical-tier metric is in the P1 set."""
    problems, warnings = [], []
    p1 = pm.p1_metric_keys() if p1_keys is None else p1_keys
    by_key = {}
    for r in rows:
        by_key.setdefault((r["service"], r["db_metric"].lower()), []).append(r)
    for key in sorted(p1):
        hits = by_key.get(key)
        if not hits:
            problems.append(Problem("p1_set", "?", key[0], key[1], "P1 key matches no scheduled metric (a typo here silently drops the 2-min evaluation)"))
            continue
        for r in hits:
            if r["interval_s"] > P1_MAX_SECONDS:
                problems.append(_p("p1_set", r, f"evaluated every 2 min but only polled every {_fmt(r['interval_s'])}"))
            pub = r.get("publish_s")
            if pub is not None and pub > P1_MAX_SECONDS:
                (problems if r["publish_status"] in FAIL_STATUSES else warnings).append(
                    _p("p1_set", r, f"P1 metric published every {_fmt(pub)} [{r['publish_status']}]: a 2-min evaluation buys nothing"))
            elif r["publish_status"] == UNV:
                warnings.append(_p("p1_set", r, "P1 metric whose publish interval is unverified"))
    for r in rows:
        if r["tier"] == "critical" and (r["service"], r["db_metric"].lower()) not in p1:
            problems.append(_p("p1_set", r, "critical-tier metric is not in the P1 set: polled every 2 min but never evaluated on the 2-min tick"))
    return problems, warnings


def _sql_extra_minutes(seconds, crit, env, di, dci):
    """Mirror of the per-metric arithmetic in app/alert_rules.py _env_poll_extra_sql at 7c72413. The audit cross-checks it
    against env_polling.extra_window_minutes so the SQL the evaluator runs and the collector cannot drift apart."""
    if seconds >= ep.NEVER_SLOW_AT_OR_ABOVE_SECONDS:
        return 0
    if env == "nonprod":
        return int(math.ceil(max(0, (dci if crit else di) - seconds) / 60.0))
    return 0


_ENVS = ("prod", "nonprod", None, "", " NONPROD ", "Non-Prod", " DEV ", "Uat", "unknown", "ignore", "junk")
_ENV_SETTINGS = [
    {},
    {"POLL_NONPROD_INTERVAL_SECONDS": "0", "POLL_NONPROD_CRITICAL_INTERVAL_SECONDS": "0"},
    {"POLL_NONPROD_INTERVAL_SECONDS": "999999", "POLL_NONPROD_CRITICAL_INTERVAL_SECONDS": "999999"},
    {"POLL_NONPROD_INTERVAL_SECONDS": "120", "POLL_NONPROD_CRITICAL_INTERVAL_SECONDS": "120"},
    {"POLL_NONPROD_NONCRITICAL_MODE": "off"},
    {"POLL_NONPROD_INTERVAL_SECONDS": "junk", "POLL_NONPROD_NONCRITICAL_MODE": "junk"},
    {"POLL_DEV_INTERVAL_SECONDS": "1800", "POLL_DEV_CRITICAL_INTERVAL_SECONDS": "300"},      # legacy fallback names
    {"POLL_DEV_NONCRITICAL_MODE": "off"},
]
# The legacy POLL_DEV_* names are cleared too, so an operator's real .env cannot leak into a scenario.
_ENV_KEYS = ("POLL_NONPROD_INTERVAL_SECONDS", "POLL_NONPROD_CRITICAL_INTERVAL_SECONDS", "POLL_NONPROD_NONCRITICAL_MODE",
             "POLL_DEV_INTERVAL_SECONDS", "POLL_DEV_CRITICAL_INTERVAL_SECONDS", "POLL_DEV_NONCRITICAL_MODE")


def check_env_invariants(rows, ep_mod=None, p1_keys=None):
    """Guardrail 4, flag ON (force_enabled=True), over every environment label, every base interval in the real catalog and
    extreme settings of the POLL_* overrides. `ep_mod` is injectable so a test can prove a broken policy is caught."""
    e = ep if ep_mod is None else ep_mod
    p1 = pm.p1_metric_keys() if p1_keys is None else p1_keys
    problems = []
    aws_rows = [r for r in rows if r["provider"] == "aws" and not r["source"].startswith("describe")]
    bases = sorted({r["interval_s"] for r in aws_rows})
    seen = set()

    def bad(msg, key):
        if key not in seen:
            seen.add(key)
            problems.append(Problem("env_invariants", "aws", "-", "-", msg))

    for settings in _ENV_SETTINGS:
        with envvars(**{k: settings.get(k) for k in _ENV_KEYS}):
            mode_off = e.nonprod_noncritical_mode() == "off"
            di, dci = e.nonprod_interval_seconds(), e.nonprod_critical_interval_seconds()
            for env in _ENVS:
                norm = e.normalize_env(env)
                for crit in (True, False):
                    for rtype in ("ec2", "wafv2", None):
                        for base in bases:
                            eff = e.effective_interval(env, base, crit, rtype, True)
                            ctx = f"env={env!r} base={base} critical={crit} type={rtype} settings={settings or 'defaults'}"
                            if eff is None:
                                if not (norm == "nonprod" and not crit and mode_off and rtype != "wafv2" and base < DAY):
                                    bad(f"effective_interval is None (not collected) where it must be collected: {ctx}", ("none", norm, crit, rtype))
                                continue
                            if eff < base:
                                bad(f"effective_interval {eff} < base {base}: {ctx}", ("lt", norm, crit, rtype, base))
                            if rtype == "wafv2" and eff != base:
                                bad(f"wafv2 was slowed to {eff} (base {base}): {ctx}", ("waf", norm, crit, base))
                            if base >= DAY and eff != base:
                                bad(f"a base of {base} s (>= 24 h) was slowed to {eff}: {ctx}", ("daily", norm, crit, rtype))
                            if norm == "prod" and eff != base:
                                bad(f"PROD environment slowed to {eff} (base {base}): {ctx}", ("prod", crit, rtype, base))
                            if rtype != "wafv2" and base < DAY and norm == "nonprod" and crit and eff != max(base, dci):
                                bad(f"NON-PROD critical-class interval {eff}, expected {max(base, dci)}: {ctx}", ("npcrit", base, dci))
                            if rtype != "wafv2" and base < DAY and norm == "nonprod" and not crit and eff != max(base, di):
                                bad(f"NON-PROD non-critical interval {eff}, expected {max(base, di)}: {ctx}", ("nonprod", base, di))
                            extra = e.extra_window_minutes(env, base, crit, rtype, True)
                            if extra < 0 or (eff == base and extra != 0):
                                bad(f"extra_window_minutes {extra} inconsistent with effective {eff} (base {base}): {ctx}", ("extra", norm, crit, base))
                            if rtype is None and eff != base:
                                sqlx = _sql_extra_minutes(base, crit, norm, di, dci)
                                if sqlx != extra:
                                    bad(f"alert_rules SQL formula adds {sqlx} min but env_polling.extra_window_minutes says {extra}: {ctx}", ("sql", norm, crit, base))
    # real P1 keys: PROD critical-class unchanged, NON-PROD never faster than base
    for (svc, name) in sorted(p1):
        for base in bases:
            if e.is_critical_metric(svc, name, p1) and e.effective_interval("prod", base, True, svc, True) != base:
                bad(f"PROD critical-class metric ({svc}, {name}) is slowed at base {base}", ("p1prod", svc, name, base))
    return problems


def check_base_interval_agrees(rows):
    """polling_model.base_interval_seconds() is what the collectors, evaluator and API ask; it must equal the catalog tier."""
    problems = []
    saved = pm._BASE_OVERRIDES_CACHE
    pm._BASE_OVERRIDES_CACHE = None
    try:
        for r in rows:
            if r["provider"] != "aws":
                continue
            got = pm.base_interval_seconds("aws", r["service"], r["db_metric"])
            want = None if r["source"].startswith("describe") else r["interval_s"]
            if got != want:
                problems.append(_p("env_invariants", r, f"polling_model.base_interval_seconds says {got}, the catalog tier says {want}"))
    finally:
        pm._BASE_OVERRIDES_CACHE = saved
    return problems


def _table_value(table, seconds):
    """Same lookup as app/alert_rules.py _interval_value: exact key, else the nearest key."""
    return int(table.get(seconds, table[min(table, key=lambda k: abs(k - seconds))]))


def check_staleness(rows, ep_mod=None, stale=None, window=None):
    """Guardrail 5. For every base interval and every environment-slowed interval the staleness limit and the evaluation
    window (table value for the BASE interval + the extra minutes the slowing adds) must exceed the interval, otherwise a
    healthy metric would look stale between two polls."""
    e = ep if ep_mod is None else ep_mod
    stale = pm.STALE_MIN_BY_INTERVAL if stale is None else stale
    window = pm.EVAL_WINDOW_MIN_BY_INTERVAL if window is None else window
    problems = []
    bases = sorted({r["interval_s"] for r in rows})
    for name, table in (("STALE_MIN_BY_INTERVAL", stale), ("EVAL_WINDOW_MIN_BY_INTERVAL", window)):
        for base in bases:
            have = _table_value(table, base)
            if have * 60 <= base:
                problems.append(Problem("staleness", "-", "-", f"{name}[{base}]",
                                        f"{name} gives {have} min for a {base} s interval: a healthy metric looks stale between polls"))
        # env-slowed intervals (AWS only: env polling never touches Azure/GCP)
        aws_bases = sorted({r["interval_s"] for r in rows if r["provider"] == "aws" and not r["source"].startswith("describe")})
        for settings in _ENV_SETTINGS:
            with envvars(**{k: settings.get(k) for k in _ENV_KEYS}):
                for env in ("nonprod",):
                    for crit in (True, False):
                        for base in aws_bases:
                            eff = e.effective_interval(env, base, crit, None, True)
                            if eff is None or eff <= base:
                                continue
                            total_min = _table_value(table, base) + e.extra_window_minutes(env, base, crit, None, True)
                            if total_min * 60 <= eff:
                                problems.append(Problem("staleness", "aws", "-", f"{name}[{base}]",
                                                        f"{env} {'critical' if crit else 'non-critical'} slowed {base}->{eff} s but "
                                                        f"{name} allows only {total_min} min ({settings or 'defaults'})"))
    return problems


def check_period_vs_publish(rows):
    """GetMetricData Period finer than the native publish interval shifts reported timestamps (the 2026-09-29 bug class in
    polling_model.py). Core and extended AWS metrics only; the CWAgent and Describe rows carry no Period here."""
    problems, warnings = [], []
    core = {(m.resource_type, m.cw_name): m.period_sec for m in pm.AWS_CORE_METRICS}
    for r in rows:
        pub = r.get("publish_s")
        if r["provider"] != "aws" or pub is None or r["source"] not in ("core", "extended"):
            continue
        period = core.get((r["service"], r["metric"])) if r["source"] == "core" else pm.aws_extended_period_sec(r["service"], r["metric"])
        if period is not None and period < pub:
            msg = f"queried at Period={period}s, finer than the native {_fmt(pub)} publish [{r['publish_status']}]: timestamps shift"
            (problems if r["publish_status"] in FAIL_STATUSES else warnings).append(_p("period_vs_publish", r, msg))
    return problems, warnings


def check_expiry_static(rows):
    """Certificate/key expiry: the absolute value is the signal, so it must stay static-only (never auto-tuned)."""
    return [_p("expiry_static", r, "expiry metric is not static-only: a dynamic baseline would learn the countdown as normal")
            for r in rows if "expir" in r["metric"].lower() and not r["static_only"]]


def check_tier_maps_in_catalog(az_curated=None, az_tiers=None, gcp_curated=None, gcp_tiers=None,
                               aws_curated=None, aws_overrides=None):
    """Every name in a tier allowlist (Azure/GCP CRITICAL/LOW/EXTENDED_* maps, AWS per-metric overrides) must be a metric in
    the curated catalog. metric_tiers() builds a scheduled row for every name in those maps, so a misspelt name is a phantom
    row: a paid poll of a series that does not exist, and (for a CRITICAL name) a P1 key that never evaluates."""
    azc, gcc = (AZ if az_curated is None else az_curated), (GCP if gcp_curated is None else gcp_curated)
    azt = az.metric_tiers(azc) if az_tiers is None else az_tiers
    gct = gcp.metric_tiers(gcc) if gcp_tiers is None else gcp_tiers
    awc = AWS if aws_curated is None else aws_curated
    awo = pm.AWS_EXTENDED_TIER_OVERRIDES if aws_overrides is None else aws_overrides
    problems = []
    for prov, cur, tiers in (("azure", azc, azt), ("gcp", gcc, gct)):
        for (svc, metric), tier in sorted(tiers.items()):
            names = {m[0] for m in cur[svc][3]} if svc in cur else set()
            if metric not in names:
                problems.append(Problem("tier_map_in_catalog", prov, svc, metric,
                                        f"named in a {tier} tier allowlist but not in the curated catalog (typo or removed metric)"))
    ext = {(svc, m[0]) for svc, (_d, _n, cat, ms) in awc.items() if cat == "extended" for m in ms}
    for (svc, metric), tier in sorted(awo.items()):
        if (svc, metric) not in ext:
            problems.append(Problem("tier_map_in_catalog", "aws", svc, metric,
                                    f"in AWS_EXTENDED_TIER_OVERRIDES ({tier}) but not an extended catalog metric: the override does nothing"))
    return problems


def check_classified(rows, table=None):
    """Every scheduled metric has a PUBLISH_TABLE entry, and no entry is dead (drifted from the catalog)."""
    problems = []
    for r in rows:
        if r.get("publish_s") is None:
            problems.append(_p("classified", r, "no PUBLISH_TABLE entry: add one with its source, or mark it unverified"))
    if table is None:
        table = PUBLISH_TABLE
    have_exact = {(r["provider"], r["service"], r["metric"]) for r in rows}
    have_svc = {(r["provider"], r["service"]) for r in rows}
    for prov, svc, metric, _s, _st, _src in table:
        if metric == "*" and (prov, svc) not in have_svc:
            problems.append(Problem("classified", prov, svc, "*", "dead PUBLISH_TABLE entry: no scheduled metric in this service"))
        elif metric != "*" and (prov, svc, metric) not in have_exact:
            problems.append(Problem("classified", prov, svc, metric, "dead PUBLISH_TABLE entry: no such scheduled metric"))
    return problems


def run_guardrails(rows=None, waf_rows=None):
    """-> (problems, warnings). Runs every guardrail over the real catalog, with the WAF fast-polling flag off and on."""
    rows = inventory() if rows is None else rows
    waf_rows = inventory_waf_fast() if waf_rows is None else waf_rows
    problems, warnings = [], []
    for label, rs in (("", rows), ("[WAF_FAST_POLLING_ENABLED=true] ", waf_rows)):
        for fn in (check_publish_interval, check_daily_fast_tier, check_p1_set, check_period_vs_publish):
            p, w = fn(rs)
            problems += [q._replace(msg=label + q.msg) if label else q for q in p]
            warnings += [q._replace(msg=label + q.msg) if label else q for q in w]
        for fn in (check_expiry_static, check_classified, check_staleness):
            p = fn(rs)
            problems += [q._replace(msg=label + q.msg) if label else q for q in p]
    problems += check_env_invariants(rows) + check_base_interval_agrees(rows) + check_tier_maps_in_catalog()
    seen, uniq = set(), []
    for q in problems:                      # the WAF-on pass repeats findings that do not depend on the flag
        k = (q.rule, q.provider, q.service, q.metric, q.msg.replace("[WAF_FAST_POLLING_ENABLED=true] ", ""))
        if k not in seen:
            seen.add(k)
            uniq.append(q)
    wseen, wuniq = set(), []
    for q in warnings:
        k = (q.rule, q.provider, q.service, q.metric, q.msg.replace("[WAF_FAST_POLLING_ENABLED=true] ", ""))
        if k not in wseen:
            wseen.add(k)
            wuniq.append(q)
    return uniq, wuniq


def split_known(problems, known=None):
    """-> (new problems, known-open problems, stale known keys that no longer reproduce)."""
    known = KNOWN_OPEN if known is None else known
    new = [q for q in problems if (q.rule, q.provider, q.service, q.metric) not in known]
    old = [q for q in problems if (q.rule, q.provider, q.service, q.metric) in known]
    live = {(q.rule, q.provider, q.service, q.metric) for q in old}
    gone = sorted(k for k in known if k not in live)
    return new, old, gone


def fmt_problem(q):
    where = "/".join(x for x in (q.provider, q.service, q.metric) if x and x != "-")
    return f"GUARDRAIL {q.rule}: {where}: {q.msg}"


def evidence_summary(rows):
    by = {}
    for r in rows:
        by[r["publish_status"]] = by.get(r["publish_status"], 0) + 1
    return ", ".join(f"{by.get(k, 0)} {k}" for k in (V, SEC, REPO, UNV, "unclassified") if by.get(k, 0))


def print_report(rows, counts, full):
    """Guardrail 6: paid or free, and cost units per resource per day, per metric (full) and per service."""
    print("\nCost units per resource per day (units: AWS metrics requested, Azure API calls, GCP series returned; no prices):")
    svc = {}
    for r in rows:
        k = (r["provider"], r["service"])
        d = svc.setdefault(k, {"n": 0, "paid": 0, "units": 0.0})
        d["n"] += 1
        d["paid"] += r["billing"] != "free"
        d["units"] += r["units_per_resource_day"]
    for (prov, name), d in sorted(svc.items()):
        n = counts.get(name, 0) if prov == "aws" else 0
        extra = f"  -> {d['units'] * n:9.0f} units/day for {n} resources" if n else ""
        shared = "  (Azure: per-metric figures share one call per batch, do not sum)" if prov == "azure" and d["paid"] else ""
        print(f"  {prov:6} {name:24} {d['n']:3} metrics, {d['paid']:3} paid, {d['units']:8.1f} units/resource/day{extra}{shared}")
    if full:
        print("\nPer metric:")
        print(f"  {'provider':6} {'service':22} {'metric':42} {'tier':13} {'every':>6}  {'billing':21} {'units/res/day':>13}  publish")
        for r in rows:
            pub = "?" if r["publish_s"] is None else _fmt(r["publish_s"])
            print(f"  {r['provider']:6} {r['service']:22} {r['metric'][:42]:42} {r['tier']:13} {_fmt(r['interval_s']):>6}  "
                  f"{r['billing']:21} {r['units_per_resource_day']:13.1f}  {pub} [{r['publish_status']}]")
    if counts:
        print("\n(elb counts every ALB and NLB metric for every load balancer, so it is an upper bound.)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--counts", default="{}", help='JSON {"ec2": 40, "elb": 3, ...} resources per service')
    ap.add_argument("--csv", default=os.path.join(os.path.dirname(__file__), "..", "docs", "polling_inventory.csv"))
    ap.add_argument("--check", action="store_true", help="exit 1 on any promotion or guardrail failure")
    ap.add_argument("--strict", action="store_true", help="with --check: KNOWN_OPEN findings (runtime tier still wrong) fail too")
    ap.add_argument("--report", action="store_true", help="print the per-metric paid/free and cost-unit table")
    a = ap.parse_args()

    rows = inventory()
    idx = _index(rows)
    problems = check_list("PROMOTION", PROMOTIONS, idx, "applied") + check_list("DEMOTION", DEMOTIONS, idx, "slower")
    gproblems, gwarnings = run_guardrails(rows)
    new, known, gone = split_known(gproblems)

    def guard_lines():
        out = [fmt_problem(q) for q in new]
        out += [("KNOWN OPEN " if not a.strict else "") + fmt_problem(q) + "\n    -> " + KNOWN_OPEN[(q.rule, q.provider, q.service, q.metric)]
                for q in known]
        out += [f"NOTE KNOWN_OPEN entry no longer reproduces, remove it: {k}" for k in gone]
        out += [f"WARNING (unverified evidence) {fmt_problem(q)}" for q in gwarnings]
        return out

    if a.check:
        fail = problems + [fmt_problem(q) for q in new] + ([fmt_problem(q) for q in known] if a.strict else [])
        if not problems:
            print("polling audit: every promotion is applied and every demotion is a real slow-down")
        print("\n".join(problems + guard_lines()))
        print(f"guardrails: {len(new)} failing, {len(known)} known open, {len(gwarnings)} warnings; "
              f"publish evidence over {len(rows)} metrics: {evidence_summary(rows)}")
        sys.exit(1 if fail else 0)
    if a.report:
        print_report(rows, json.loads(a.counts), full=True)
        return

    with open(a.csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), lineterminator="\n")   # LF on every OS (the repo copy is LF)
        w.writeheader()
        w.writerows(rows)

    print(f"{len(rows)} scheduled metrics -> {os.path.relpath(a.csv)}")
    by = {}
    for r in rows:
        by.setdefault((r["provider"], r["tier"], r["interval_s"]), 0)
        by[(r["provider"], r["tier"], r["interval_s"])] += 1
    for (prov, tier, sec), n in sorted(by.items()):
        print(f"  {prov:6} {tier:14} every {sec:>6}s  {n:4} metrics")
    if problems:
        print("\nPROBLEMS:\n  " + "\n  ".join(problems))
    glines = guard_lines()
    print(f"\nGuardrails: {len(new)} failing, {len(known)} known open, {len(gwarnings)} warnings "
          f"(publish evidence: {evidence_summary(rows)})")
    if glines:
        print("  " + "\n  ".join(glines))

    counts = json.loads(a.counts)
    print_report(rows, counts, full=False)
    print("\nModelled AWS cost of the demotions (NOT applied), per resource and for --counts:")
    for key, target in DEMOTIONS.items():
        if key[0] != "aws" or key not in idx:
            continue
        cur = idx[key]["interval_s"]
        saved = 86400 / cur - 86400 / target
        n = counts.get(key[1], 0)
        print(f"  {key[1]:8} {key[2]:24} saves {saved:5.0f} billed metrics/day/resource"
              f" -> {n} resources: ${saved * n * 30 / 1000 * AWS_PRICE_PER_1000:6.2f}/month")
    print("\nCost of the applied AWS promotions, per resource (they add polls, they do not save):")
    for key, target in PROMOTIONS.items():
        if key[0] != "aws" or key not in idx:
            continue
        # idx already reflects the applied state, so show the cost vs the old interval from the audit table
        old = {"cloudfront": 86400}.get(key[1], 3600)
        added = 86400 / target - 86400 / old
        print(f"  {key[1]:14} {key[2]:28} +{added:4.0f} billed metrics/day/resource"
              f" = ${added * 30 / 1000 * AWS_PRICE_PER_1000:.4f}/month/resource")


if __name__ == "__main__":
    main()
