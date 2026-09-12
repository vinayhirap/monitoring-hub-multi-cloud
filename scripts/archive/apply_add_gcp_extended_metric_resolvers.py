#!/usr/bin/env python3
"""
apply_add_gcp_extended_metric_resolvers.py
===========================================
Closes (most of) the Section 3 "GCP extended tier -- not started" gap
from the Sep 6-10 2026 handover.

WHAT THE REAL GAP TURNED OUT TO BE
-----------------------------------
Re-reading app/providers/gcp/discovery.py and metrics_collector.py
before writing anything (same "verify before building" step the AWS
extended-tier work took) showed GCP's extended tier was NOT starting
from zero the way AWS's was:
  - discovery.py already has dedicated discoverers writing real
    `resources` rows for all 12 extended services (gke_cluster,
    gke_node, cloudfunctions_function, pubsub_topic,
    pubsub_subscription, cloud_lb, redis_instance, bigquery_project,
    spanner_instance, firestore_database, nat_gateway,
    gce_persistent_disk).
  - metrics_collector.py already fetches metric values for ALL of
    them fleet-wide via list_time_series() -- no extra API calls
    needed, unlike AWS's per-resource GetMetricData.
  - The actual gap: every one of those returned time series was being
    logged as "no resource resolver for GCP service '<x>' yet" and
    silently dropped, because _RESOLVERS only covered the 4 core
    services. Resource resolution -- not discovery, not collection --
    was the missing piece.

WHAT THIS SHIPS
----------------
  1. app/providers/gcp/metrics_extended.py -- resolver functions for
     10 of the 12 extended services, reconstructing discovery.py's
     exact resource_id path from each time series' Cloud Monitoring
     resource.labels.
  2. One small wiring insertion into the EXISTING _RESOLVERS dict in
     app/providers/gcp/metrics_collector.py (merges in the 10 new
     resolvers -- no other logic in that file changes).

NOT COVERED, ON PURPOSE (2 of 12 services, + 2 of bigquery_project's
4 metrics) -- see metrics_extended.py's docstring for the full
reasoning on each:
  - gke_node:            Cloud Monitoring's k8s_node resource has no
                          node-pool label; discovery.py's rows are
                          per-pool. Needs a node->pool lookup this
                          pass doesn't build.
  - gce_persistent_disk:  compute.googleapis.com/instance/disk/*
                          metrics are reported against the VM
                          (gce_instance), with the disk identified by
                          a metric label, not a resource label --
                          there's no disk-shaped monitored resource to
                          resolve from. This is the same category of
                          problem as the EC2 disk multi-mount-point
                          item elsewhere in Section 3.
  - bigquery_project's query/count, query/execution_times,
    slots/allocated_for_project: published against the project-level
    bigquery_project resource with no dataset_id at all -- can never
    attribute to one of this app's per-dataset rows. Only
    storage/stored_bytes (bigquery_dataset resource, has dataset_id)
    resolves.

CONFIDENCE / WHAT'S BEEN VERIFIED VS. NOT
-------------------------------------------
Every monitored-resource-type/label pairing was checked against
Google's published Cloud Monitoring monitored-resource-type reference
(cloud.google.com/monitoring/api/resources), not guessed. Offline
verification done as part of building this (see this script's
_selftest()): all 10 resolvers produce the exact resource_id string
discovery.py writes, for both a positive (labels present) and negative
(labels missing -> None, no guessing) case, including the cloud_lb
regional-then-global fallback.

NOT verified: an actual live GCP project. No credentials were
available in this environment. Recommended rollout: apply on dev,
enable one of the 10 now-resolvable extended services in
Settings -> Metrics to Monitor for a project you know has real
resources of that type, wait one collection cycle, and confirm:
  a) no new "no resource resolver" warnings for that service in the
     logs, and
  b) a chart actually populates with real data
before trusting this end to end.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_add_gcp_extended_metric_resolvers.py --dry-run
    python3 apply_add_gcp_extended_metric_resolvers.py --apply
    sudo systemctl restart monitoring-hub
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

RESOLVERS_OLD = '''_RESOLVERS = {
    "compute_instance": _resolve_compute_instance,
    "gcs_bucket": _resolve_gcs_bucket,
    "cloudsql_instance": _resolve_cloudsql_instance,
    "cloud_run_service": _resolve_cloud_run_service,
}'''

RESOLVERS_NEW = '''_RESOLVERS = {
    "compute_instance": _resolve_compute_instance,
    "gcs_bucket": _resolve_gcs_bucket,
    "cloudsql_instance": _resolve_cloudsql_instance,
    "cloud_run_service": _resolve_cloud_run_service,
}

# Extended-tier resolvers (gke_cluster, cloudfunctions_function,
# pubsub_topic, pubsub_subscription, cloud_lb, redis_instance,
# bigquery_project, spanner_instance, firestore_database, nat_gateway)
# -- see app/providers/gcp/metrics_extended.py for the confidence
# breakdown, including the 2 of 12 extended services (gke_node,
# gce_persistent_disk) deliberately left unresolved there.
from app.providers.gcp.metrics_extended import EXTENDED_RESOLVERS
_RESOLVERS.update(EXTENDED_RESOLVERS)'''

DOCSTRING_OLD = '''Services with no resolver (the "extended" tier) are skipped and counted,
not guessed at.
"""'''

DOCSTRING_NEW = '''Services with no resolver are skipped and counted, not guessed at. As
of app/providers/gcp/metrics_extended.py, that's down to 2 of the 12
extended-tier services (gke_node, gce_persistent_disk) plus 2 of
bigquery_project's 4 metrics -- see that module's docstring for why
each is a genuine resource-modeling gap rather than a missing lookup.
"""'''

DONE_MARKER = "EXTENDED_RESOLVERS"

NEW_MODULE_FILES = [
    "app/providers/gcp/metrics_extended.py",
]


def die(msg):
    print(f"\n[ABORT] {msg}", file=sys.stderr)
    sys.exit(1)


def find_repo_root():
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(cur, "app", "auth", "security.py")) and \
           os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            die("Could not locate the monitoring-hub-multi-cloud repo root.")
        cur = parent


def backup(path):
    bpath = path + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(path, bpath)
    return bpath


def _selftest(repo_root):
    """
    Offline checks that don't need GCP credentials or a live DB:
      1. metrics_extended.py imports and has exactly 10 resolvers.
      2. Each resolver reconstructs discovery.py's exact resource_id
         format given synthetic Cloud Monitoring labels, and returns
         None (not a wrong guess) when required labels are missing.
      3. cloud_lb's regional-then-global fallback behaves correctly.
      4. gke_node and gce_persistent_disk are confirmed absent (an
         accidental resolver for either would be worse than none --
         see module docstring for why they can't be done correctly
         with resource labels alone).
    """
    sys.path.insert(0, repo_root)
    import importlib.util

    ext_path = os.path.join(repo_root, "app/providers/gcp/metrics_extended.py")
    spec = importlib.util.spec_from_file_location("gcp_metrics_extended_selftest", ext_path)
    ext = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ext)

    if len(ext.EXTENDED_RESOLVERS) != 10:
        die(f"Expected 10 extended resolvers, found {len(ext.EXTENDED_RESOLVERS)}.")

    if "gke_node" in ext.EXTENDED_RESOLVERS or "gce_persistent_disk" in ext.EXTENDED_RESOLVERS:
        die("gke_node / gce_persistent_disk should NOT have resolvers (documented gap) -- "
            "found one. Did someone add a guessed implementation?")

    pid = "selftest-proj"
    cases = [
        ("gke_cluster", {"location": "asia-south1-a", "cluster_name": "prod"},
         f"projects/{pid}/locations/asia-south1-a/clusters/prod"),
        ("cloudfunctions_function", {"region": "asia-south1", "function_name": "fn1"},
         f"projects/{pid}/locations/asia-south1/functions/fn1"),
        ("pubsub_topic", {"topic_id": "orders"}, f"projects/{pid}/topics/orders"),
        ("pubsub_subscription", {"subscription_id": "orders-sub"},
         f"projects/{pid}/subscriptions/orders-sub"),
        ("redis_instance", {"region": "asia-south1", "instance_id": "cache1"},
         f"projects/{pid}/locations/asia-south1/instances/cache1"),
        ("bigquery_project", {"dataset_id": "analytics"}, f"projects/{pid}/datasets/analytics"),
        ("spanner_instance", {"instance_id": "spanner1"}, f"projects/{pid}/instances/spanner1"),
        ("firestore_database", {"database": "(default)"}, f"projects/{pid}/databases/(default)"),
        ("nat_gateway", {"region": "asia-south1", "router_id": "rtr1", "gateway_name": "nat1"},
         f"projects/{pid}/regions/asia-south1/routers/rtr1/nats/nat1"),
    ]
    for svc, labels, expected in cases:
        fake_map = {expected: 42}
        got = ext.EXTENDED_RESOLVERS[svc](pid, labels, fake_map, {})
        if got != 42:
            die(f"{svc}: expected resolve to hit {expected!r}, got {got!r}")
        if ext.EXTENDED_RESOLVERS[svc](pid, {}, fake_map, {}) is not None:
            die(f"{svc}: missing labels should resolve to None (no guessing)")

    lb = ext.EXTENDED_RESOLVERS["cloud_lb"]
    gmap = {f"projects/{pid}/global/forwardingRules/lb1": 7}
    if lb(pid, {"forwarding_rule_name": "lb1"}, gmap, {}) != 7:
        die("cloud_lb: global fallback failed")
    rmap = {f"projects/{pid}/regions/asia-south1/forwardingRules/lb2": 8}
    if lb(pid, {"forwarding_rule_name": "lb2", "region": "asia-south1"}, rmap, {}) != 8:
        die("cloud_lb: regional resolve failed")

    print("[selftest] OK -- 10/10 resolvers registered and verified, "
          "gke_node/gce_persistent_disk correctly absent.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="(default; kept for backward compatibility)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    repo_root = find_repo_root()
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    _selftest(repo_root)

    for rel in NEW_MODULE_FILES:
        if not os.path.exists(os.path.join(repo_root, rel)):
            die(f"{rel} not found -- this script expects it to already be present "
                f"(copy it alongside this script) before wiring it in.")

    coll_path = os.path.join(repo_root, "app/providers/gcp/metrics_collector.py")
    with open(coll_path, "r", encoding="utf-8") as fh:
        coll_content = fh.read()

    if DONE_MARKER in coll_content:
        print("\napp/providers/gcp/metrics_collector.py already patched. Nothing to do.")
        return

    if RESOLVERS_OLD not in coll_content:
        die("app/providers/gcp/metrics_collector.py: _RESOLVERS dict doesn't match what "
            "this script expects. File may have changed since this script was written.")

    new_coll_content = coll_content.replace(RESOLVERS_OLD, RESOLVERS_NEW, 1)
    if DOCSTRING_OLD in new_coll_content:
        new_coll_content = new_coll_content.replace(DOCSTRING_OLD, DOCSTRING_NEW, 1)

    print(f"\nFile patch plan:")
    print(f"  app/providers/gcp/metrics_extended.py:   new file, already present")
    print(f"  app/providers/gcp/metrics_collector.py:  "
          f"OK ({len(new_coll_content) - len(coll_content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(coll_path)
    with open(coll_path, "w", encoding="utf-8") as fh:
        fh.write(new_coll_content)
    print("Patched app/providers/gcp/metrics_collector.py")

    print("""
[Manual follow-up -- REQUIRED]

  A) Restart:
       sudo systemctl restart monitoring-hub

  B) Watch the next GCP collection cycle for the services this project
     actually uses -- confirm the "no resource resolver" warning is
     GONE for those 10 services (it will still appear, correctly, for
     gke_node / gce_persistent_disk, and for bigquery_project's
     project-scoped metrics):
       sudo journalctl -u monitoring-hub -f | grep -i "gcp\\|resolver"

  C) Enable one of the 10 now-resolvable services in
     Settings -> Metrics to Monitor for a GCP project you know has
     real resources of that type, wait one cycle, confirm a chart
     actually populates -- the real end-to-end proof.

  D) Review, commit, push:
       git status
       git add app/providers/gcp/metrics_extended.py \\
               app/providers/gcp/metrics_collector.py \\
               apply_add_gcp_extended_metric_resolvers.py
       git commit -m "feat(gcp): resolve 10 of 12 extended-tier services to resource rows for metric collection; document 2 services (gke_node, gce_persistent_disk) + 2 bigquery_project metrics as genuine resource-modeling gaps, not oversights"
       git push origin main
""")


if __name__ == "__main__":
    main()
