#!/usr/bin/env python3
"""
fix_gcp_extended_service_detection.py
==========================================
Monitoring Hub -- GCP half of the provider-consistency audit's Phase 1.

IMPORTANT CORRECTION vs the Azure fix that shipped earlier this session
------------------------------------------------------------------------
The Azure fix's docstring predicted "GCP has the identical gap." That
turned out to be wrong in an important way, caught by reading GCP's actual
collector before writing this: app/providers/gcp/metrics_collector.py
calls Cloud Monitoring's list_time_series with a project-wide filter --
ONE call returns that metric for EVERY resource of that type in the whole
project (see that file's own "Efficiency" docstring section). It never
queries the `resources` MySQL table at all. So GCP's metric collection is
NOT blocked by missing `resources` rows the way Azure's was -- GCP is
architecturally closer to AWS's YACE model here, not to Azure's.

GCP's REAL gap is narrower: app/providers/gcp/provider.py's
discover_resources() only ever auto-enables metrics for the 16 curated
service keys (`detected = {k for k, v in counts.items() if v}`, where
`counts` only has those 16 keys to begin with). Any of the ~12 further
DIRECTORY-tier GCP services already sitting in metric_catalog_data.py
(Cloud DNS, Secret Manager, Cloud KMS, Cloud Armor, etc.) never get
auto-enabled no matter how much a project actually uses them -- a person
would have to know these exist and manually flip them on in Settings.
Once enabled, though, collection already works today with zero further
changes (confirmed above) -- unlike Azure, where enabling alone wasn't
enough.

FIX
---
Adds ONE generic Cloud Asset Inventory query (asset_v1.AssetServiceClient.
list_assets -- enumerates every resource in a project, any type, in one
paginated call) to app/providers/gcp/discovery.py, cross-referenced
against metric_catalog (provider='gcp', queried live from the DB).
Matching strategy, deliberately conservative given how inconsistently the
namespace strings in metric_catalog_data.py were authored (some are clean
service-domain prefixes, at least one DIRECTORY entry is a full specific
metric-type path, not a domain):
  1. Exact case-insensitive match against the full namespace -- covers
     every well-formed CURATED entry correctly.
  2. Domain-only fallback (the part before the first "/") -- but ONLY for
     domains that map to exactly one metric_catalog service key. A domain
     shared by multiple entries (e.g. compute.googleapis.com, which has
     both compute_instance and gce_persistent_disk) is left ambiguous on
     purpose rather than guessed at -- per this project's "do not fake
     support" principle, a wrong guess (mislabeling a VM as a disk) is
     worse than not matching it at all.

Real service keys that match get BOTH: (a) a `resources` row via the
existing _upsert_resource, for cross-provider consistency and any future
resource-level dashboard/console-link work, and (b) passed directly to
enable_metrics_for_services() -- this second part is what actually closes
GCP's real bug, since it's the auto-enable step (not resource discovery)
that was the gap here.

Wired in as one more step in discover_account_resources's existing
`steps` list, same additive/fail-open pattern as every other step.

TESTED (mocked, no live GCP project available): exact-match, domain-only
fallback for an unambiguous domain, ambiguous-domain correctly skipped,
unrecognized asset type skipped + logged, pagination, idempotent
re-upsert, and the direct enable_metrics_for_services call receiving the
correct real service-key set (not a fake bucket key like the Azure fix's
`counts` dict pattern would produce). NOT tested: an actual live call to
Cloud Asset Inventory -- no GCP credentials or network access available
here; that verification can only happen against a real project.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_gcp_extended_service_detection.py --dry-run
    python3 fix_gcp_extended_service_detection.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

REQUIREMENTS_OLD = "google-cloud-firestore>=2.17,<3"
REQUIREMENTS_NEW = "google-cloud-firestore>=2.17,<3\ngoogle-cloud-asset>=3,<4"

IMPORT_OLD = "from googleapiclient.discovery import build as gapi_build"
IMPORT_NEW = """from googleapiclient.discovery import build as gapi_build
from google.cloud import asset_v1"""

COUNTS_OLD = '''        "spanner_instance": 0, "firestore_database": 0, "nat_gateway": 0, "gce_persistent_disk": 0,
    }'''

COUNTS_NEW = '''        "spanner_instance": 0, "firestore_database": 0, "nat_gateway": 0, "gce_persistent_disk": 0,
        "extended_via_asset_inventory": 0,
    }'''

STEPS_OLD = '''        ("gce_persistent_disk",      lambda: _discover_persistent_disks(creds, project_id, account["id"], cursor)),
    ]'''

STEPS_NEW = '''        ("gce_persistent_disk",      lambda: _discover_persistent_disks(creds, project_id, account["id"], cursor)),
        ("extended_via_asset_inventory",
         lambda: discover_extended_via_asset_inventory(creds, project_id, account["id"], cursor)["matched"]),
    ]'''

NEW_FUNCTIONS = '''

# ── Generic extended-tier detection (Cloud Asset Inventory) ──────────
#
# See fix_gcp_extended_service_detection.py for the full story on why
# this is shaped differently from the Azure equivalent: GCP's metric
# collector already works fleet-wide without `resources` rows, so the
# real gap here is auto-enable, not collection.

def _load_gcp_namespace_maps(cursor) -> tuple[dict, dict]:
    """
    Returns (exact_map, domain_map):
      exact_map:  full namespace (lowercased) -> service key
      domain_map: service-domain prefix (before first "/") -> service key,
                  ONLY for domains that map to exactly one service key
                  (ambiguous domains are deliberately excluded).
    """
    cursor.execute(
        "SELECT DISTINCT service, namespace FROM metric_catalog WHERE provider = \\'gcp\\' "
        "AND namespace IS NOT NULL AND namespace != \\'\\'"
    )
    rows = [(r[0], r[1]) for r in cursor.fetchall() if r[0] and r[1]]

    exact_map = {ns.lower(): service for service, ns in rows}

    domain_candidates = {}
    for service, ns in rows:
        domain = ns.split("/", 1)[0].lower()
        domain_candidates.setdefault(domain, set()).add(service)
    domain_map = {d: next(iter(s)) for d, s in domain_candidates.items() if len(s) == 1}

    return exact_map, domain_map


def _match_asset_type(asset_type: str, exact_map: dict, domain_map: dict):
    at = (asset_type or "").lower()
    if at in exact_map:
        return exact_map[at]
    domain = at.split("/", 1)[0]
    return domain_map.get(domain)


def discover_extended_via_asset_inventory(creds, project_id, account_id, cursor) -> dict:
    """
    One generic Cloud Asset Inventory query enumerates EVERY resource in
    the project, any type. Matches (see _match_asset_type) get a
    `resources` row AND are collected into a real service-key set that's
    handed straight to enable_metrics_for_services() -- this is what
    actually closes GCP's gap: auto-enable was the missing piece, not
    resource inventory (collection already works fleet-wide once enabled).

    An asset type matching nothing in metric_catalog is logged (capped,
    deduplicated) and left alone -- not silently made to look monitorable.

    Returns {"matched": int, "unrecognized_types": sorted list capped at 20}.
    """
    exact_map, domain_map = _load_gcp_namespace_maps(cursor)
    if not exact_map and not domain_map:
        logger.warning("GCP extended discovery: metric_catalog has no gcp rows -- "
                        "run scripts/seed_multicloud_metric_catalog.py first. Skipping.")
        return {"matched": 0, "unrecognized_types": []}

    client = asset_v1.AssetServiceClient(credentials=creds)
    request = asset_v1.ListAssetsRequest(
        parent=f"projects/{project_id}",
        content_type=asset_v1.ContentType.RESOURCE,
    )

    matched = 0
    matched_service_keys = set()
    unrecognized = set()
    for asset in client.list_assets(request=request):
        service_key = _match_asset_type(asset.asset_type, exact_map, domain_map)
        if not service_key:
            unrecognized.add((asset.asset_type or "").lower())
            continue

        resource_data = {}
        location = None
        try:
            resource_data = dict(asset.resource.data) if asset.resource else {}
            location = asset.resource.location if asset.resource else None
        except Exception:
            pass  # best-effort -- some asset types don't populate resource.data the same way

        name = resource_data.get("name") or asset.name
        tags = resource_data.get("labels") or {}

        _upsert_resource(cursor, account_id, service_key, asset.name, name, tags, location, "other")
        matched += 1
        matched_service_keys.add(service_key)

    if unrecognized:
        logger.info(
            f"GCP extended discovery: {len(unrecognized)} asset type(s) with no "
            f"metric_catalog entry (not monitorable until reviewed/added): "
            f"{sorted(unrecognized)[:20]}"
        )

    if matched_service_keys:
        from app.api.metric_catalog import enable_metrics_for_services
        result = enable_metrics_for_services(account_id, matched_service_keys, provider="gcp", source="discovered")
        if result["added"]:
            logger.info(
                f"GCP extended discovery: auto-enabled {result['added']} metric(s) "
                f"across services={result['services']}"
            )

    return {"matched": matched, "unrecognized_types": sorted(unrecognized)[:20]}
'''


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

    discovery_path = os.path.join(repo_root, "app", "providers", "gcp", "discovery.py")
    requirements_path = os.path.join(repo_root, "requirements.txt")
    for label, path in [("app/providers/gcp/discovery.py", discovery_path),
                         ("requirements.txt", requirements_path)]:
        if not os.path.exists(path):
            die(f"{label} not found at {path}.")

    with open(discovery_path, "r", encoding="utf-8") as fh:
        disc_content = fh.read()

    if "discover_extended_via_asset_inventory" in disc_content:
        print("app/providers/gcp/discovery.py already has the extended-discovery fix -- nothing to do.")
        disc_patched = None
    else:
        for old, label in [(IMPORT_OLD, "import"), (COUNTS_OLD, "counts dict"), (STEPS_OLD, "steps list")]:
            n = disc_content.count(old)
            if n != 1:
                die(f"discovery.py ({label}): expected exactly 1 match, found {n}. "
                    f"File may differ from what this script expects.")
        disc_patched = disc_content.replace(IMPORT_OLD, IMPORT_NEW, 1)
        disc_patched = disc_patched.replace(COUNTS_OLD, COUNTS_NEW, 1)
        disc_patched = disc_patched.replace(STEPS_OLD, STEPS_NEW, 1)
        disc_patched = disc_patched.rstrip("\n") + "\n" + NEW_FUNCTIONS

    with open(requirements_path, "r", encoding="utf-8") as fh:
        req_content = fh.read()

    if "google-cloud-asset" in req_content:
        print("requirements.txt already has google-cloud-asset -- nothing to do.")
        req_patched = None
    else:
        n = req_content.count(REQUIREMENTS_OLD)
        if n != 1:
            die(f"requirements.txt: expected exactly 1 match, found {n}.")
        req_patched = req_content.replace(REQUIREMENTS_OLD, REQUIREMENTS_NEW, 1)

    if disc_patched is None and req_patched is None:
        print("\nNothing to do -- everything this script would add is already present.")
        return

    print("\nAll patches matched expected content exactly:")
    if disc_patched is not None:
        print(f"  app/providers/gcp/discovery.py: OK ({len(disc_patched) - len(disc_content):+d} bytes)")
    if req_patched is not None:
        print(f"  requirements.txt: OK ({len(req_patched) - len(req_content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    if disc_patched is not None:
        backup(discovery_path)
        with open(discovery_path, "w", encoding="utf-8") as fh:
            fh.write(disc_patched)
        print("Patched app/providers/gcp/discovery.py")
    if req_patched is not None:
        backup(requirements_path)
        with open(requirements_path, "w", encoding="utf-8") as fh:
            fh.write(req_patched)
        print("Patched requirements.txt")

    print("""
[Manual follow-up]

  A) Install the new dependency in the venv:
       /opt/monitoring-hub/venv/bin/pip install google-cloud-asset

  B) The service account used for each GCP account needs the
     'roles/cloudasset.viewer' IAM role (or broader Viewer) on the
     project -- Cloud Asset Inventory is a separate API/permission from
     the per-service read roles already granted for the 16 curated
     discovery functions. If that role is missing, this new step will
     fail gracefully (logged, fails open) but silently detect nothing.

  C) This does NOT run against any live GCP project by itself -- it
     only adds the capability. It runs automatically on the NEXT
     scheduled discovery cycle (every 15 min) for every active gcp
     account, as one more step alongside the existing 16.

  D) To see it work sooner, trigger discovery manually and watch the
     app log for "GCP discovery for <account>: {...}" -- the new
     "extended_via_asset_inventory" count should be > 0 for any GCP
     project that has resource types beyond the 16 curated ones (Secret
     Manager, Cloud DNS, Cloud KMS, Cloud Armor, etc. -- whichever of
     the DIRECTORY-tier services in metric_catalog_data.py are
     genuinely in use), and you should see an "auto-enabled N metric(s)"
     log line the first time each one is found.

  E) No service restart strictly required for the fix to take effect
     next cycle, but restarting is the cleanest way to pick up the new
     import immediately:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 40 --no-pager

  F) Review, commit, push:
       git status
       git diff app/providers/gcp/discovery.py requirements.txt
       git add app/providers/gcp/discovery.py requirements.txt fix_gcp_extended_service_detection.py
       git commit -m "feat(gcp): generic Cloud Asset Inventory extended-service auto-enable -- closes the coverage gap vs curated 16"
       git push origin main

  This completes Phase 1 (generic discovery) of the multi-cloud
  provider-consistency audit for all three clouds.
""")


if __name__ == "__main__":
    main()
