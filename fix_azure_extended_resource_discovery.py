#!/usr/bin/env python3
"""
fix_azure_extended_resource_discovery.py
=============================================
Monitoring Hub -- closes the real Azure/GCP multi-cloud coverage gap found
during the provider-consistency audit.

BACKGROUND
----------
AWS has a two-tier discovery model: 5 "core" services get full per-resource
inventory via Describe/List calls (app/collector/discovery/runner.py), and
~35 "extended" services get detected via ONE generic Resource Groups
Tagging API sweep (app/aws/resource_discovery.py) that auto-enables their
default metrics. Azure and GCP only ever got the AWS-equivalent of the
"core" tier -- hand-written SDK .list() calls for ~19 curated types each.
There is no Azure/GCP equivalent of AWS's generic extended-tier sweep.

This is NOT just a "fewer catalog entries" cosmetic gap. Azure's metric
collector (app/providers/azure/metrics_collector.py::collect_account_metrics)
queries `SELECT ... FROM resources WHERE resource_type = %s` before it will
ever collect a single datapoint for that service -- unlike AWS, where YACE
does its own independent namespace-wide discovery at scrape time and never
needs a `resources` row at all. So for Azure (and GCP, same pattern),
ANY resource type not covered by the curated per-type discovery functions
literally cannot ever be monitored, no matter what a person enables in the
UI -- there's no `resources` row for the collector to find. Azure's own
metric_catalog_data.py already lists ~20 further "DIRECTORY" service types
(Azure API Management, Azure Firewall, Azure Front Door, etc.) that have
been completely inert this whole time for exactly this reason.

FIX
---
Adds ONE generic Azure Resource Graph query (enumerates every resource in
a subscription, any type, in one call) to app/providers/azure/discovery.py,
cross-referenced against every namespace already registered in
metric_catalog (CURATED + DIRECTORY, ~39 Azure types total -- queried live
from the DB, not re-parsed from the Python source). Any match gets a real
`resources` row, which the EXISTING collector/alerting/console-link code
picks up completely unchanged -- this fix touches zero lines of the
collection, alerting, or frontend pipeline. An ARM resource type with no
metric_catalog entry is logged (capped, deduplicated) and left alone --
per this project's "do not fake support" principle, nothing is made to
look monitorable until someone has actually reviewed and added a metric
definition for it.

Wired in as ONE MORE step in discover_account_resources's existing `steps`
list -- additive, wrapped in the same per-step try/except every other step
already uses, so a Resource Graph failure can't take down the 19 curated
per-type discoveries. The `resources` table's UNIQUE KEY (resource_id,
resource_type) makes re-covering an already-curated resource here a
harmless no-op UPDATE, not a duplicate row.

TESTED (see accompanying test suite run in a sandbox, no live Azure
account available): matching a curated type, matching a DIRECTORY type,
skipping an unrecognized type, pagination via skip_token, an empty-catalog
short-circuit, and idempotent re-upsert on a second run -- 4/4 pass.
NOT tested: an actual live call to Azure Resource Graph (no Azure
credentials or network access available here) -- that verification can
only happen against a real subscription on the dev server.

GCP has the exact same architectural gap and needs the identical fix
(Cloud Asset Inventory instead of Resource Graph) -- follow-up, not
included here to keep this one change reviewable on its own.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_azure_extended_resource_discovery.py --dry-run
    python3 fix_azure_extended_resource_discovery.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

REQUIREMENTS_OLD = "azure-mgmt-datafactory>=3,<4"
REQUIREMENTS_NEW = "azure-mgmt-datafactory>=3,<4\nazure-mgmt-resourcegraph>=8,<9"

IMPORT_OLD = "from azure.mgmt.datafactory import DataFactoryManagementClient"
IMPORT_NEW = """from azure.mgmt.datafactory import DataFactoryManagementClient
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions"""

COUNTS_OLD = '''        "data_factory": 0, "managed_disk": 0,
    }'''

COUNTS_NEW = '''        "data_factory": 0, "managed_disk": 0,
        "extended_via_resource_graph": 0,
    }'''

STEPS_OLD = '''        ("data_factory",          lambda: _discover_data_factories(cred, sub_id, account["id"], cursor)),
    ]'''

STEPS_NEW = '''        ("data_factory",          lambda: _discover_data_factories(cred, sub_id, account["id"], cursor)),
        ("extended_via_resource_graph",
         lambda: discover_extended_via_resource_graph(cred, sub_id, account["id"], cursor)["matched"]),
    ]'''

NEW_FUNCTIONS = '''

# ── Generic extended-tier discovery (Resource Graph) ─────────────────
#
# Mirrors app.aws.resource_discovery's Tagging-API sweep, but for Azure:
# ONE Resource Graph query enumerates every resource in the subscription,
# any type, cross-referenced against metric_catalog so anything already
# registered (CURATED or DIRECTORY) gets a real `resources` row instead
# of sitting inert. See this module's own top-of-file docstring context
# and fix_azure_extended_resource_discovery.py for the full story.

_NAMESPACE_CATEGORY = {
    "microsoft.compute": "compute",
    "microsoft.storage": "storage",
    "microsoft.sql": "database",
    "microsoft.dbformysql": "database",
    "microsoft.dbforpostgresql": "database",
    "microsoft.documentdb": "database",
    "microsoft.cache": "database",
    "microsoft.network": "networking",
    "microsoft.keyvault": "security",
    "microsoft.recoveryservices": "security",
    "microsoft.security": "security",
    "microsoft.servicebus": "messaging",
    "microsoft.eventhub": "messaging",
    "microsoft.notificationhubs": "messaging",
    "microsoft.logic": "messaging",
    "microsoft.synapse": "analytics",
    "microsoft.databricks": "analytics",
    "microsoft.apimanagement": "networking",
    "microsoft.cdn": "networking",
    "microsoft.web": "compute",
    "microsoft.containerservice": "compute",
    "microsoft.containerinstance": "compute",
    "microsoft.operationalinsights": "analytics",
}


def _normalize_category(arm_type: str) -> str:
    """Best-effort ARM namespace -> normalized_resource_type. Anything
    not in the map falls back to \\"other\\" rather than guessing wrong --
    reviewable/extendable, not meant to be exhaustive."""
    ns = arm_type.split("/", 1)[0].lower()
    return _NAMESPACE_CATEGORY.get(ns, "other")


def _load_azure_namespace_map(cursor) -> dict:
    """namespace (lowercased) -> service key, from every azure row in
    metric_catalog -- both CURATED and DIRECTORY entries land there once
    scripts/seed_multicloud_metric_catalog.py has been run."""
    cursor.execute(
        "SELECT DISTINCT service, namespace FROM metric_catalog WHERE provider = \\'azure\\' "
        "AND namespace IS NOT NULL AND namespace != \\'\\'"
    )
    return {row[1].lower(): row[0] for row in cursor.fetchall() if row[0] and row[1]}


def discover_extended_via_resource_graph(cred, sub_id, account_id, cursor) -> dict:
    """
    One generic Azure Resource Graph query enumerates EVERY resource in
    the subscription, any type. Cross-referenced against every namespace
    already registered in metric_catalog (CURATED + DIRECTORY) -- any
    match gets a `resources` row via the same _upsert_resource used by
    every per-type discovery function above.

    Additive, not a replacement: the per-type functions above still run
    first and give richer per-type tag/name handling for the services
    worth that investment. Re-covering an already-curated resource here
    is a harmless no-op UPDATE (UNIQUE KEY on resource_id+resource_type).

    An ARM type with no metric_catalog entry is logged (capped,
    deduplicated) and left alone -- not silently made to look monitorable.

    Returns {"matched": int, "unrecognized_types": sorted list capped at 20}.
    """
    namespace_map = _load_azure_namespace_map(cursor)
    if not namespace_map:
        logger.warning("Azure extended discovery: metric_catalog has no azure rows -- "
                        "run scripts/seed_multicloud_metric_catalog.py first. Skipping.")
        return {"matched": 0, "unrecognized_types": []}

    client = ResourceGraphClient(cred)
    query = "Resources | project id, type, name, tags, location"

    matched = 0
    unrecognized = set()
    skip_token = None
    while True:
        if skip_token is None:
            request = QueryRequest(query=query, subscriptions=[sub_id])
        else:
            request = QueryRequest(query=query, subscriptions=[sub_id],
                                    options=QueryRequestOptions(skip_token=skip_token))
        response = client.resources(request)
        rows = response.data or []

        for r in rows:
            arm_type = (r.get("type") or "").lower()
            service_key = namespace_map.get(arm_type)
            if not service_key:
                unrecognized.add(arm_type)
                continue
            _upsert_resource(
                cursor, account_id, service_key, r.get("id"),
                r.get("name") or r.get("id"), r.get("tags") or {},
                r.get("location"), _normalize_category(arm_type),
            )
            matched += 1

        skip_token = getattr(response, "skip_token", None)
        if not skip_token or not rows:
            break

    if unrecognized:
        logger.info(
            f"Azure extended discovery: {len(unrecognized)} resource type(s) with no "
            f"metric_catalog entry (not monitorable until reviewed/added): "
            f"{sorted(unrecognized)[:20]}"
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

    discovery_path = os.path.join(repo_root, "app", "providers", "azure", "discovery.py")
    requirements_path = os.path.join(repo_root, "requirements.txt")
    for label, path in [("app/providers/azure/discovery.py", discovery_path),
                         ("requirements.txt", requirements_path)]:
        if not os.path.exists(path):
            die(f"{label} not found at {path}.")

    with open(discovery_path, "r", encoding="utf-8") as fh:
        disc_content = fh.read()

    if "discover_extended_via_resource_graph" in disc_content:
        print("app/providers/azure/discovery.py already has the extended-discovery fix -- nothing to do.")
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

    if "azure-mgmt-resourcegraph" in req_content:
        print("requirements.txt already has azure-mgmt-resourcegraph -- nothing to do.")
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
        print(f"  app/providers/azure/discovery.py: OK ({len(disc_patched) - len(disc_content):+d} bytes)")
    if req_patched is not None:
        print(f"  requirements.txt: OK ({len(req_patched) - len(req_content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    if disc_patched is not None:
        backup(discovery_path)
        with open(discovery_path, "w", encoding="utf-8") as fh:
            fh.write(disc_patched)
        print("Patched app/providers/azure/discovery.py")
    if req_patched is not None:
        backup(requirements_path)
        with open(requirements_path, "w", encoding="utf-8") as fh:
            fh.write(req_patched)
        print("Patched requirements.txt")

    print("""
[Manual follow-up]

  A) Install the new dependency in the venv:
       /opt/monitoring-hub/venv/bin/pip install azure-mgmt-resourcegraph

  B) This does NOT run against any live Azure account by itself -- it
     only adds the capability. It runs automatically on the NEXT
     scheduled discovery cycle (every 15 min) for every active azure
     account, as one more step alongside the existing 19.

  C) To see it work sooner, trigger discovery manually and watch the
     app log for "Azure discovery for <account>: {...}" -- the new
     "extended_via_resource_graph" count should be > 0 for any Azure
     account that has resource types beyond the 19 curated ones (Key
     Vault, API Management, Azure Firewall, etc.), and the log should
     show an "Azure extended discovery: N resource type(s) with no
     metric_catalog entry" line for anything genuinely new.

  D) No service restart strictly required for the fix to take effect
     next cycle, but restarting is the cleanest way to pick up the new
     import immediately:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 40 --no-pager

  E) Review, commit, push:
       git status
       git diff app/providers/azure/discovery.py requirements.txt
       git add app/providers/azure/discovery.py requirements.txt fix_azure_extended_resource_discovery.py
       git commit -m "feat(azure): generic Resource Graph extended-tier discovery -- closes the coverage gap vs AWS's Tagging API sweep"
       git push origin main

  F) Known follow-up, not included here: GCP has the identical gap and
     needs the same fix via Cloud Asset Inventory. Separate change.
""")


if __name__ == "__main__":
    main()
