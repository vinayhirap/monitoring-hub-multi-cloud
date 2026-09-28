#!/usr/bin/env python3
"""
fix_onboarding_autodetect_parity.py
========================================
Monitoring Hub -- Phase 3 of the provider-consistency audit: onboarding
auto-detect parity for Azure/GCP.

DEPENDS ON: fix_azure_extended_resource_discovery.py and
fix_gcp_extended_service_detection.py already applied (this script checks
for and requires their functions to exist before patching).

BACKGROUND
----------
The original audit's Finding 2 was "AWS auto-detects real services during
onboarding and pre-selects sensible metrics; Azure/GCP customers configure
everything by hand." Investigating properly (rather than assuming a big
new feature was needed) turned up something better: the REAL detection
logic already exists and is already wired up, in full, for all three
providers -- app/api/admin/accounts.py's add_account() has a complete,
working elif branch for both "azure" and "gcp" that calls
discover_account_resources() and auto-enables whatever it finds, exactly
mirroring the AWS branch. It has been sitting there unreachable, because
the ONLY thing gating it is frontend/src/pages/AccountOnboarding.jsx's
`useAutoDetect = provider === "aws" && ...` -- hardcoded to AWS only, so
Azure/GCP submissions always send an explicit (static default) metric
selection instead of ever leaving it to trigger server-side detection.

So this fix is much smaller than "build onboarding auto-detect for two
more clouds" -- it's "stop blocking the auto-detect logic that already
works." The one real gap that needed new code: the wizard's Test
Connection preview step (before final submit) has no equivalent of AWS's
`detected_services` in the response for Azure/GCP, so there was nothing
for the frontend to show or to gate `useAutoDetect` on in the first place.

FIX
---
1. Refactors both Phase 1 functions (discover_extended_via_resource_graph
   for Azure, discover_extended_via_asset_inventory for GCP) into a pure
   "scan" half with no database writes, and a thin "write" half that's
   exactly what those functions already did (same return shape, same
   behavior in the real discovery cycle -- this is a non-behavior-changing
   refactor for the existing code path). Adds one new pure function per
   provider (detect_extended_service_keys) for read-only use.

2. Extends test_azure_credentials / test_gcp_credentials to call the new
   pure detection function and return detected_services, best-effort --
   mirroring test_role's own comment: never fails the credential-test
   response itself if detection has a permissions gap or transient error.

3. Frontend: populates detectedServices from the Azure/GCP test-connection
   responses (previously discarded), and widens useAutoDetect from
   `provider === "aws"` to all three providers -- the exact same
   condition and submit-time behavior AWS already uses, now reachable for
   Azure/GCP too.

TESTED (mocked, no live Azure/GCP account available): the scan/write
refactor produces IDENTICAL results to the pre-refactor Phase 1 functions
across the same test cases already used for those (re-run against the
refactored code), plus new tests for the pure detect-only functions
returning the same service-key set without touching the cursor's write
path. NOT tested: the actual HTTP request/response cycle through FastAPI,
or a live browser exercising the onboarding wizard -- verify those on the
dev server.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_onboarding_autodetect_parity.py --dry-run
    python3 fix_onboarding_autodetect_parity.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

# ─────────────────────────── Azure discovery.py ───────────────────────────

AZURE_OLD_FUNC = '''def discover_extended_via_resource_graph(cred, sub_id, account_id, cursor) -> dict:
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

AZURE_NEW_FUNC = '''def _scan_resource_graph(cred, sub_id, cursor):
    """
    Pure detection half of the Resource Graph sweep -- makes NO database
    writes. Shared by discover_extended_via_resource_graph (the real
    discovery cycle, which upserts these into `resources`) and
    detect_extended_service_keys (the onboarding wizard's Test Connection
    preview, which has no account_id yet to write against).

    Returns (matches, unrecognized): matches is a list of
    {"service_key", "id", "name", "tags", "location", "category"} dicts;
    unrecognized is the set of lowercased ARM types with no catalog entry.
    """
    namespace_map = _load_azure_namespace_map(cursor)
    if not namespace_map:
        logger.warning("Azure extended discovery: metric_catalog has no azure rows -- "
                        "run scripts/seed_multicloud_metric_catalog.py first. Skipping.")
        return [], set()

    client = ResourceGraphClient(cred)
    query = "Resources | project id, type, name, tags, location"

    matches = []
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
            matches.append({
                "service_key": service_key, "id": r.get("id"),
                "name": r.get("name") or r.get("id"), "tags": r.get("tags") or {},
                "location": r.get("location"), "category": _normalize_category(arm_type),
            })

        skip_token = getattr(response, "skip_token", None)
        if not skip_token or not rows:
            break

    return matches, unrecognized


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
    matches, unrecognized = _scan_resource_graph(cred, sub_id, cursor)

    for m in matches:
        _upsert_resource(
            cursor, account_id, m["service_key"], m["id"], m["name"],
            m["tags"], m["location"], m["category"],
        )

    if unrecognized:
        logger.info(
            f"Azure extended discovery: {len(unrecognized)} resource type(s) with no "
            f"metric_catalog entry (not monitorable until reviewed/added): "
            f"{sorted(unrecognized)[:20]}"
        )

    return {"matched": len(matches), "unrecognized_types": sorted(unrecognized)[:20]}


def detect_extended_service_keys(cred, sub_id, cursor) -> set:
    """
    Read-only variant for the onboarding wizard's Test Connection preview,
    where no account_id exists yet to write resource rows against. Same
    detection as discover_extended_via_resource_graph, just the set of
    matched service keys -- nothing written to the database.
    """
    matches, _ = _scan_resource_graph(cred, sub_id, cursor)
    return {m["service_key"] for m in matches}
'''

# ─────────────────────────── GCP discovery.py ───────────────────────────

GCP_OLD_FUNC = '''def discover_extended_via_asset_inventory(creds, project_id, account_id, cursor) -> dict:
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

GCP_NEW_FUNC = '''def _scan_asset_inventory(creds, project_id, cursor):
    """
    Pure detection half of the Asset Inventory sweep -- makes NO database
    writes and does NOT call enable_metrics_for_services. Shared by
    discover_extended_via_asset_inventory (the real discovery cycle) and
    detect_extended_service_keys (the onboarding wizard's Test Connection
    preview, which has no account_id yet).

    Returns (matches, unrecognized): matches is a list of
    {"service_key", "id", "name", "tags", "location"} dicts.
    """
    exact_map, domain_map = _load_gcp_namespace_maps(cursor)
    if not exact_map and not domain_map:
        logger.warning("GCP extended discovery: metric_catalog has no gcp rows -- "
                        "run scripts/seed_multicloud_metric_catalog.py first. Skipping.")
        return [], set()

    client = asset_v1.AssetServiceClient(credentials=creds)
    request = asset_v1.ListAssetsRequest(
        parent=f"projects/{project_id}",
        content_type=asset_v1.ContentType.RESOURCE,
    )

    matches = []
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
        matches.append({"service_key": service_key, "id": asset.name, "name": name,
                         "tags": tags, "location": location})

    return matches, unrecognized


def discover_extended_via_asset_inventory(creds, project_id, account_id, cursor) -> dict:
    """
    One generic Cloud Asset Inventory query enumerates EVERY resource in
    the project, any type. Matches get a `resources` row AND are handed
    straight to enable_metrics_for_services() -- this is what actually
    closes GCP's gap: auto-enable was the missing piece, not resource
    inventory (collection already works fleet-wide once enabled).

    An asset type matching nothing in metric_catalog is logged (capped,
    deduplicated) and left alone -- not silently made to look monitorable.

    Returns {"matched": int, "unrecognized_types": sorted list capped at 20}.
    """
    matches, unrecognized = _scan_asset_inventory(creds, project_id, cursor)

    matched_service_keys = set()
    for m in matches:
        _upsert_resource(cursor, account_id, m["service_key"], m["id"], m["name"],
                          m["tags"], m["location"], "other")
        matched_service_keys.add(m["service_key"])

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

    return {"matched": len(matches), "unrecognized_types": sorted(unrecognized)[:20]}


def detect_extended_service_keys(creds, project_id, cursor) -> set:
    """
    Read-only variant for the onboarding wizard's Test Connection preview,
    where no account_id exists yet to write resource rows against, and
    nothing should be auto-enabled before the account is even saved. Same
    detection as discover_extended_via_asset_inventory, just the set of
    matched service keys.
    """
    matches, _ = _scan_asset_inventory(creds, project_id, cursor)
    return {m["service_key"] for m in matches}
'''

# ─────────────────────────── accounts.py ───────────────────────────

ACCOUNTS_AZURE_OLD = '''    try:
        result = get_provider("azure").validate_credentials({
            "tenant_id": tenant_id, "client_id": client_id,
            "subscription_id": subscription_id, "client_secret": client_secret,
        })
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Azure credential validation failed: {e}")'''

ACCOUNTS_AZURE_NEW = '''    try:
        result = get_provider("azure").validate_credentials({
            "tenant_id": tenant_id, "client_id": client_id,
            "subscription_id": subscription_id, "client_secret": client_secret,
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Azure credential validation failed: {e}")

    # Best-effort service detection for the onboarding wizard preview, same
    # rationale as test_role's AWS equivalent above: never fails the
    # credential-test response itself if detection hits a permissions gap
    # (Resource Graph is a separate RBAC surface from the per-service Reader
    # roles already needed for the 19 curated discovery functions).
    detected_services = []
    try:
        from azure.identity import ClientSecretCredential
        from app.providers.azure.discovery import detect_extended_service_keys

        cred = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
        conn = get_connection(); cur = conn.cursor()
        try:
            detected_services = sorted(detect_extended_service_keys(cred, subscription_id, cur))
        finally:
            cur.close(); conn.close()
    except Exception as e:
        logger.warning(f"test-azure-credentials service detection skipped: {e}")

    result["detected_services"] = detected_services
    return result'''

ACCOUNTS_GCP_OLD = '''    try:
        result = get_provider("gcp").validate_credentials({
            "project_id": project_id, "service_account_key": service_account_key,
        })
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"GCP credential validation failed: {e}")'''

ACCOUNTS_GCP_NEW = '''    try:
        result = get_provider("gcp").validate_credentials({
            "project_id": project_id, "service_account_key": service_account_key,
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"GCP credential validation failed: {e}")

    # Best-effort service detection for the onboarding wizard preview, same
    # rationale as test_role's AWS equivalent above: never fails the
    # credential-test response itself if detection hits a permissions gap
    # (Cloud Asset Inventory needs its own roles/cloudasset.viewer grant,
    # separate from the per-service Viewer roles already needed for the 16
    # curated discovery functions).
    detected_services = []
    try:
        import json as _json
        from google.oauth2 import service_account as gcp_service_account
        from app.providers.gcp.discovery import detect_extended_service_keys

        info = _json.loads(service_account_key)
        creds = gcp_service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform.read-only"]
        )
        conn = get_connection(); cur = conn.cursor()
        try:
            detected_services = sorted(detect_extended_service_keys(creds, project_id, cur))
        finally:
            cur.close(); conn.close()
    except Exception as e:
        logger.warning(f"test-gcp-credentials service detection skipped: {e}")

    result["detected_services"] = detected_services
    return result'''

# ─────────────────────────── frontend ───────────────────────────

FRONTEND_TEST_OLD = '''      } else if (provider === "azure") {
        const r = await testAzureCredentials({
          tenant_id: form.tenant_id.trim(),
          subscription_id: form.subscription_id.trim(),
          client_id: form.client_id.trim(),
          client_secret: form.client_secret.trim(),
        });
        setTestStatus("success");
        setTestMsg(`Verified — ${r.resource_groups_visible} resource group(s) visible`);
      } else if (provider === "gcp") {
        const r = await testGcpCredentials({
          project_id: form.project_id.trim(),
          service_account_key: form.service_account_key.trim(),
        });
        setTestStatus("success");
        setTestMsg(`Verified — project "${r.project_display_name || form.project_id}"`);
      }'''

FRONTEND_TEST_NEW = '''      } else if (provider === "azure") {
        const r = await testAzureCredentials({
          tenant_id: form.tenant_id.trim(),
          subscription_id: form.subscription_id.trim(),
          client_id: form.client_id.trim(),
          client_secret: form.client_secret.trim(),
        });
        setTestStatus("success");
        setDetectedServices(r.detected_services || []);
        setTestMsg(
          r.detected_services && r.detected_services.length
            ? `Verified — ${r.resource_groups_visible} resource group(s) visible, ${r.detected_services.length} service type(s) detected`
            : `Verified — ${r.resource_groups_visible} resource group(s) visible (no extra service types detected yet; ` +
              `defaults will be applied instead)`
        );
      } else if (provider === "gcp") {
        const r = await testGcpCredentials({
          project_id: form.project_id.trim(),
          service_account_key: form.service_account_key.trim(),
        });
        setTestStatus("success");
        setDetectedServices(r.detected_services || []);
        setTestMsg(
          r.detected_services && r.detected_services.length
            ? `Verified — project "${r.project_display_name || form.project_id}", ${r.detected_services.length} service type(s) detected`
            : `Verified — project "${r.project_display_name || form.project_id}" (no extra service types detected yet; ` +
              `defaults will be applied instead)`
        );
      }'''

FRONTEND_AUTODETECT_OLD = '''    const useAutoDetect = provider === "aws" && detectedServices.length > 0 && !metricsEdited;'''

FRONTEND_AUTODETECT_NEW = '''    // Same condition for all three providers now -- AWS, Azure and GCP all
    // have a real, working server-side auto-detect path in add_account()
    // (see app/api/admin/accounts.py); this was previously gated to AWS
    // only here on the frontend, which is what made the Azure/GCP branches
    // of that backend logic unreachable despite being fully implemented.
    const useAutoDetect = detectedServices.length > 0 && !metricsEdited;'''


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


def prepare_patch(path, label, old, new, done_marker):
    """Returns (new_content_or_None, note). None means nothing to do."""
    if not os.path.exists(path):
        die(f"{label} not found at {path}.")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    if done_marker in content:
        return None, f"{label} already patched -- skipping."
    n = content.count(old)
    if n != 1:
        die(f"{label}: expected exactly 1 match for the expected old content, found {n}. "
            f"This usually means fix_azure_extended_resource_discovery.py / "
            f"fix_gcp_extended_service_detection.py haven't been applied yet -- "
            f"this script depends on both.")
    return content.replace(old, new, 1), f"{label}: OK ({len(new) - len(old):+d} bytes)"


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

    azure_disc_path = os.path.join(repo_root, "app", "providers", "azure", "discovery.py")
    gcp_disc_path = os.path.join(repo_root, "app", "providers", "gcp", "discovery.py")
    accounts_path = os.path.join(repo_root, "app", "api", "admin", "accounts.py")
    frontend_path = os.path.join(repo_root, "frontend", "src", "pages", "AccountOnboarding.jsx")

    for label, path in [("app/providers/azure/discovery.py", azure_disc_path),
                         ("app/providers/gcp/discovery.py", gcp_disc_path),
                         ("app/api/admin/accounts.py", accounts_path),
                         ("frontend/src/pages/AccountOnboarding.jsx", frontend_path)]:
        if not os.path.exists(path):
            die(f"{label} not found at {path}.")

    with open(azure_disc_path) as fh:
        azure_check = fh.read()
    if "discover_extended_via_resource_graph" not in azure_check:
        die("app/providers/azure/discovery.py does not have discover_extended_via_resource_graph -- "
            "run fix_azure_extended_resource_discovery.py --apply first.")

    with open(gcp_disc_path) as fh:
        gcp_check = fh.read()
    if "discover_extended_via_asset_inventory" not in gcp_check:
        die("app/providers/gcp/discovery.py does not have discover_extended_via_asset_inventory -- "
            "run fix_gcp_extended_service_detection.py --apply first.")

    patches = []
    for path, label, old, new, marker in [
        (azure_disc_path, "app/providers/azure/discovery.py (scan/write refactor)",
         AZURE_OLD_FUNC, AZURE_NEW_FUNC, "detect_extended_service_keys"),
        (gcp_disc_path, "app/providers/gcp/discovery.py (scan/write refactor)",
         GCP_OLD_FUNC, GCP_NEW_FUNC, "detect_extended_service_keys"),
    ]:
        result, note = prepare_patch(path, label, old, new, marker)
        patches.append((path, label, result, note))

    with open(accounts_path) as fh:
        accounts_content = fh.read()
    accounts_new_content = accounts_content
    accounts_notes = []
    for old, new, marker, label in [
        (ACCOUNTS_AZURE_OLD, ACCOUNTS_AZURE_NEW, "detect_extended_service_keys(cred, subscription_id",
         "accounts.py (Azure test-connection detection)"),
        (ACCOUNTS_GCP_OLD, ACCOUNTS_GCP_NEW, "detect_extended_service_keys(creds, project_id",
         "accounts.py (GCP test-connection detection)"),
    ]:
        if marker in accounts_new_content:
            accounts_notes.append(f"{label} already patched -- skipping.")
            continue
        n = accounts_new_content.count(old)
        if n != 1:
            die(f"{label}: expected exactly 1 match, found {n}.")
        accounts_new_content = accounts_new_content.replace(old, new, 1)
        accounts_notes.append(f"{label}: OK")
    accounts_changed = accounts_new_content != accounts_content

    with open(frontend_path) as fh:
        frontend_content = fh.read()
    frontend_new_content = frontend_content
    frontend_notes = []
    for old, new, marker, label in [
        (FRONTEND_TEST_OLD, FRONTEND_TEST_NEW, "service type(s) detected",
         "AccountOnboarding.jsx (Test Connection detectedServices)"),
        (FRONTEND_AUTODETECT_OLD, FRONTEND_AUTODETECT_NEW, "Same condition for all three providers",
         "AccountOnboarding.jsx (useAutoDetect widened)"),
    ]:
        if marker in frontend_new_content:
            frontend_notes.append(f"{label} already patched -- skipping.")
            continue
        n = frontend_new_content.count(old)
        if n != 1:
            die(f"{label}: expected exactly 1 match, found {n}.")
        frontend_new_content = frontend_new_content.replace(old, new, 1)
        frontend_notes.append(f"{label}: OK")
    frontend_changed = frontend_new_content != frontend_content

    print("\nPatch plan:")
    for path, label, result, note in patches:
        print(f"  {note}")
    for note in accounts_notes:
        print(f"  {note}")
    for note in frontend_notes:
        print(f"  {note}")

    anything_to_do = any(r is not None for _, _, r, _ in patches) or accounts_changed or frontend_changed
    if not anything_to_do:
        print("\nNothing to do -- everything this script would add is already present.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    for path, label, result, note in patches:
        if result is None:
            continue
        backup(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(result)
        print(f"Patched {label}")

    if accounts_changed:
        backup(accounts_path)
        with open(accounts_path, "w", encoding="utf-8") as fh:
            fh.write(accounts_new_content)
        print("Patched app/api/admin/accounts.py")

    if frontend_changed:
        backup(frontend_path)
        with open(frontend_path, "w", encoding="utf-8") as fh:
            fh.write(frontend_new_content)
        print("Patched frontend/src/pages/AccountOnboarding.jsx")

    print("""
[Manual follow-up]

  A) Backend: no new dependency, no restart strictly required for the
     python changes to take effect on the NEXT request, but restart is
     cleanest since this touches an imported module's function set:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 40 --no-pager

  B) Frontend: this repo's frontend is a built React app -- rebuild and
     redeploy the static bundle the same way you already do for other
     frontend changes (check deploy/deploy.sh or deploy/update.sh's
     frontend build step for the exact command used in this project).

  C) Verify in the browser: start adding a new Azure or GCP account,
     fill in real credentials, click "Test Connection" -- you should now
     see a "N service type(s) detected" message (previously this text
     never appeared for non-AWS providers). Submit the account and check
     the app log for "Azure discovery for <name>: {...}" or "GCP
     discovery for <name>: {...}" with real counts, and confirm
     Settings -> Metrics for the new account shows more than just the
     bare default template.

  D) Review, commit, push:
       git status
       git diff app/providers/azure/discovery.py app/providers/gcp/discovery.py \\
                app/api/admin/accounts.py frontend/src/pages/AccountOnboarding.jsx
       git add app/providers/azure/discovery.py app/providers/gcp/discovery.py \\
               app/api/admin/accounts.py frontend/src/pages/AccountOnboarding.jsx \\
               fix_onboarding_autodetect_parity.py
       git commit -m "feat(onboarding): Azure/GCP auto-detect parity with AWS -- backend logic already existed, was unreachable from the frontend"
       git push origin main

  This completes Phase 3 (onboarding auto-detect parity) of the
  multi-cloud provider-consistency audit.
""")


if __name__ == "__main__":
    main()
