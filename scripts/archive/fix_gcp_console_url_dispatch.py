#!/usr/bin/env python3
"""
fix_gcp_console_url_dispatch.py
====================================
Monitoring Hub -- Phase 4 of the provider-consistency audit: GCP
console-link coverage.

IMPORTANT CORRECTION -- this is NOT the fairness gap it first looked like
---------------------------------------------------------------------------
Before writing anything: checked AWS's own dispatcher
(app/aws/federation.py::resource_console_destination). It only covers 7
of AWS's ~41 curated services with a real resource-level deep link --
everything else falls back to a generic service-list or console-home
page. That's an existing, deliberate, already-accepted design in this
codebase (the docstring even calls it "the single source of truth"),
not something specific to Azure/GCP being shortchanged. Azure's own
get_console_url is fully generic only because ARM resource IDs happen to
map directly into a portal URL path -- architectural luck, not something
that was built for Azure and withheld from GCP.

So GCP's shallowness (3 of 16 curated services get a real link: compute
VMs, GCS buckets, Cloud SQL -- everything else, including every resource
Phase 1's new Cloud Asset Inventory sweep can now discover, falls through
to the generic project dashboard) isn't "unfair" relative to AWS. It's
just genuinely worth improving on its own, especially now that Phase 1
means far more GCP resource types actually show up in the UI to click on.

FIX
---
Extends app/providers/gcp/provider.py's get_console_url with real,
resource-level deep links for the 7 more curated services where the
Cloud Console URL pattern is well-documented and stable enough to be
confident about without a live project to verify against: GKE clusters,
Cloud Functions, Pub/Sub topics and subscriptions, Cloud Run services,
Memorystore Redis instances, Cloud Spanner instances, and Persistent
Disks. That takes real coverage from 3 to 10 of the 16 curated types.

Deliberately NOT guessing links for: GKE node pools (needs the parent
cluster name, not available at this call site -- constructing a wrong
URL is worse than a generic fallback), Cloud Load Balancing (the URL
structure genuinely differs by LB type -- HTTP(S) vs TCP vs internal --
and guessing wrong risks sending someone to a broken page), Cloud NAT
(NAT gateways are sub-resources of a Cloud Router whose name isn't
tracked in `resources`), and Firestore (the default database's URL-safe
name isn't reliably "(default)" across all Firestore modes). Those get a
real service-LIST page instead of the fully generic project dashboard --
still an improvement, without pretending precision this doesn't have.
BigQuery gets the project-level BigQuery console (that catalog entry is
project-scoped, not a single resource, by design).

Directory-tier and any as-yet-uncataloged resource type discovered by
Phase 1's generic sweep still falls through to the project dashboard --
same as before this fix, and same as AWS's own behavior for its 34
extended-tier services. Per this project's "do not fake support"
principle, an honest generic fallback beats a guessed-and-possibly-wrong
specific one; the four "list page" cases above are the middle ground
where a real, safe improvement exists without needing to guess.

TESTED: every URL branch exercised with representative inputs, confirming
correct dispatch by service key and correct fallback behavior for both
the "no case matches" and "matches but list-only" paths. NOT tested:
whether these URLs are still exactly correct in Google's current Cloud
Console (their console UI does change over time) -- only verifiable by
clicking through on the dev server.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_gcp_console_url_dispatch.py --dry-run
    python3 fix_gcp_console_url_dispatch.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

OLD_BLOCK = '''        if service == "compute_instance":
            zone = region or ""
            return (f"https://console.cloud.google.com/compute/instancesDetail/"
                    f"zones/{zone}/instances/{name}?project={project_id}")
        if service == "gcs_bucket":
            return f"https://console.cloud.google.com/storage/browser/{name}?project={project_id}"
        if service == "cloudsql_instance":
            return f"https://console.cloud.google.com/sql/instances/{name}/overview?project={project_id}"
        return f"https://console.cloud.google.com/home/dashboard?project={project_id}"'''

NEW_BLOCK = '''        if service == "compute_instance":
            zone = region or ""
            return (f"https://console.cloud.google.com/compute/instancesDetail/"
                    f"zones/{zone}/instances/{name}?project={project_id}")
        if service == "gcs_bucket":
            return f"https://console.cloud.google.com/storage/browser/{name}?project={project_id}"
        if service == "cloudsql_instance":
            return f"https://console.cloud.google.com/sql/instances/{name}/overview?project={project_id}"
        if service == "gke_cluster":
            return (f"https://console.cloud.google.com/kubernetes/clusters/details/"
                    f"{region}/{name}/details?project={project_id}")
        if service == "cloudfunctions_function":
            return (f"https://console.cloud.google.com/functions/details/"
                    f"{region}/{name}?project={project_id}")
        if service == "pubsub_topic":
            return f"https://console.cloud.google.com/cloudpubsub/topic/detail/{name}?project={project_id}"
        if service == "pubsub_subscription":
            return f"https://console.cloud.google.com/cloudpubsub/subscription/detail/{name}?project={project_id}"
        if service == "cloud_run_service":
            return (f"https://console.cloud.google.com/run/detail/"
                    f"{region}/{name}/metrics?project={project_id}")
        if service == "redis_instance":
            return (f"https://console.cloud.google.com/memorystore/redis/locations/"
                    f"{region}/instances/{name}/details/overview?project={project_id}")
        if service == "spanner_instance":
            return f"https://console.cloud.google.com/spanner/instances/{name}/details/databases?project={project_id}"
        if service == "gce_persistent_disk":
            zone = region or ""
            return (f"https://console.cloud.google.com/compute/disksDetail/"
                    f"zones/{zone}/disks/{name}?project={project_id}")
        if service == "bigquery_project":
            # This catalog entry is project-scoped by design (BigQuery
            # datasets/tables aren't tracked as individual `resources` rows)
            # -- the BigQuery console itself, not a guess at a specific table.
            return f"https://console.cloud.google.com/bigquery?project={project_id}"

        # Real deep links deliberately NOT attempted here -- constructing
        # one would require data this call site doesn't have (GKE node
        # pools need their parent cluster name) or the URL genuinely
        # varies by a sub-type this app doesn't track (Cloud Load
        # Balancing differs for HTTP(S)/TCP/internal; Cloud NAT is a
        # sub-resource of an untracked Cloud Router; Firestore's default
        # database name isn't reliably URL-safe across all modes). A
        # service-LIST page is still a real improvement over the fully
        # generic project dashboard below, without pretending precision
        # this doesn't have -- see this project's "do not fake support"
        # principle (app/providers/base.py's module docstring).
        if service == "gke_node":
            return f"https://console.cloud.google.com/kubernetes/list/overview?project={project_id}"
        if service == "cloud_lb":
            return f"https://console.cloud.google.com/net-services/loadbalancing/list/loadBalancers?project={project_id}"
        if service == "nat_gateway":
            return f"https://console.cloud.google.com/net-services/nat/list?project={project_id}"
        if service == "firestore_database":
            return f"https://console.cloud.google.com/firestore/databases?project={project_id}"

        return f"https://console.cloud.google.com/home/dashboard?project={project_id}"'''


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


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply_ = args.apply and not args.dry_run

    repo_root = find_repo_root()
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    provider_path = os.path.join(repo_root, "app", "providers", "gcp", "provider.py")
    if not os.path.exists(provider_path):
        die(f"app/providers/gcp/provider.py not found at {provider_path}.")

    with open(provider_path, "r", encoding="utf-8") as fh:
        content = fh.read()

    if "gke_cluster" in content and "get_console_url" in content:
        print("app/providers/gcp/provider.py already has the extended console-URL dispatch -- nothing to do.")
        return

    n = content.count(OLD_BLOCK)
    if n != 1:
        die(f"Expected exactly 1 match for the current get_console_url dispatch block, found {n}. "
            f"File may differ from what this script expects.")

    new_content = content.replace(OLD_BLOCK, NEW_BLOCK, 1)

    print(f"\nPatch matched expected content exactly: app/providers/gcp/provider.py "
          f"({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    backup(provider_path)
    with open(provider_path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("Patched app/providers/gcp/provider.py")

    print("""
[Manual follow-up]

  A) No new dependency. No restart strictly required for this to take
     effect on the next request, but restart is cleanest:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 20 --no-pager

  B) Verify in the browser: open a GCP account's dashboard, find a
     resource of one of the 7 newly-covered types (GKE cluster, Cloud
     Function, Pub/Sub topic/subscription, Cloud Run service, Memorystore
     Redis, Cloud Spanner, or a Persistent Disk) and click through to the
     console -- it should land on that specific resource's page, not the
     generic project dashboard. These URL patterns are well-documented
     but NOT live-verified against a real project from this session --
     if Google's console has since changed a URL shape, flag it and it's
     a quick one-line fix in this same function.

  C) Review, commit, push:
       git status
       git diff app/providers/gcp/provider.py
       git add app/providers/gcp/provider.py fix_gcp_console_url_dispatch.py
       git commit -m "feat(gcp): extend console-link dispatch from 3 to 10 real deep links, honest list-page fallback for 4 more"
       git push origin main

  This completes Phase 4 (console-URL dispatch) of the multi-cloud
  provider-consistency audit. Remaining: Phase 5 (alert-cadence audit).
""")


if __name__ == "__main__":
    main()
