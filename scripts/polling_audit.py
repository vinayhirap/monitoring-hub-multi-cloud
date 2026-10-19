#!/usr/bin/env python3
"""
READ-ONLY polling audit: every scheduled metric on AWS, Azure and GCP, the interval it is polled at today,
and whether that interval fits what the metric is for.  Touches no database and calls no cloud API; it reads
the real catalogs and tier maps, so the numbers cannot drift from what the collectors do.

    python3 scripts/polling_audit.py                       # summary + writes docs/polling_inventory.csv
    python3 scripts/polling_audit.py --counts '{"ec2":40}' # resource counts -> modelled cost of the demotions
    python3 scripts/polling_audit.py --check               # exit 1 if any PROMOTION below is not applied

The two lists below are the audit's findings (docs/POLLING_AUDIT_2026-10-19.md):

  PROMOTIONS  failure / saturation signals that were polled slower than their siblings. Applied in code.
              --check proves each one still exists in the catalog and is at the intended interval.
  DEMOTIONS   pure traffic-volume meters polled every 5 min. NOT applied: the saving is a few cents a month
              at today's fleet. Listed so a fleet that grows can switch them on knowingly.
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collector import polling_model as pm  # noqa: E402
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
AWS_PRICE_PER_1000 = 0.01


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
    out = []
    for prov, svc, cw, db, tier, sec, src in rows:
        out.append({"provider": prov, "service": svc, "metric": cw, "db_metric": db, "tier": tier,
                    "interval_s": sec, "source": src, "evaluated_every_2min": (svc, db.lower()) in p1,
                    "static_only": is_static_only_metric(db)})
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--counts", default="{}", help='JSON {"ec2": 40, "elb": 3, ...} resources per service')
    ap.add_argument("--csv", default=os.path.join(os.path.dirname(__file__), "..", "docs", "polling_inventory.csv"))
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    rows = inventory()
    idx = _index(rows)
    problems = check_list("PROMOTION", PROMOTIONS, idx, "applied") + check_list("DEMOTION", DEMOTIONS, idx, "slower")
    if a.check:
        print("\n".join(problems) or "polling audit: every promotion is applied and every demotion is a real slow-down")
        sys.exit(1 if problems else 0)

    with open(a.csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
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

    counts = json.loads(a.counts)
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
