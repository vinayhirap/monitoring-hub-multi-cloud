#!/usr/bin/env python3
"""
apply_extended_service_resource_counts_fix.py

Problem
-------
On the Services page (ServiceList.jsx), tiles for the 7 "core" AWS
services (EC2, EBS, RDS, Lambda, S3, ALB, ECS) already hide themselves
when GET /api/live/resource-counts/{id} confirms zero real resources.

Every other enabled service — ACM, AWS Backup, AWS DMS, Direct Connect,
Step Functions, NLB, etc. — has NO resource collector at all, so
/api/live/resource-counts never returns a key for them. The frontend
treats a missing key as "unknown" and fails OPEN (keeps the tile
visible) regardless of whether the account actually has any of that
resource. That's why those tiles show up statically even when nothing
of that type exists in the account.

Fix
---
1. app/aws/collector_direct.py — add cheap, single describe/list-call
   collectors for acm, backup, dms, directconnect, states (Step
   Functions) and nlb. Same style/caching as the existing
   collect_ec2_instances / collect_elb / etc.

2. app/api/live_data.py — register those new collectors (plus the
   existing collect_elb for "alb") in the resource-counts map, so
   GET /api/live/resource-counts/{id} returns a real 0/N for them
   instead of omitting the key. A per-service try/except keeps a
   missing IAM permission from taking down the whole endpoint — that
   service's count just comes back None (fail-open), same as before.

3. frontend/src/pages/ServiceList.jsx — the tile-visibility filter no
   longer special-cases the 7 core services. ANY service now hides its
   tile once resource-counts confirms 0 for it; a service with no
   collector at all (key still absent) keeps failing open, since a
   missing count is "we don't know yet", not "there are none".

Net effect: a service box only stays on screen if it actually has
enabled metrics AND (a confirmed resource count > 0, or no collector
exists yet to check).

Usage:
    python apply_extended_service_resource_counts_fix.py [repo_root]

Idempotent: safe to re-run. Backs up every file it touches to
"<file>.bak.pre-extended-resource-counts-fix" (only on the first run).
Reverts all changes automatically if any patched Python file fails
py_compile.
"""
import py_compile
import shutil
import sys
from pathlib import Path

BAK_SUFFIX = ".bak.pre-extended-resource-counts-fix"


class PatchError(Exception):
    pass


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def backup(path: Path):
    bak = path.with_name(path.name + BAK_SUFFIX)
    if not bak.exists():
        shutil.copy2(path, bak)


def apply_replacements(path: Path, replacements, already_applied_marker=None):
    """
    replacements: list of (old, new, label) tuples.
    If already_applied_marker is found in the file, skip (idempotent re-run).
    Returns True if the file was changed.
    """
    text = read(path)
    if already_applied_marker and already_applied_marker in text:
        print(f"  SKIP  {path} (already patched)")
        return False

    backup(path)
    changed = 0
    for old, new, label in replacements:
        count = text.count(old)
        if count == 0:
            raise PatchError(f"{path}: pattern not found for '{label}'")
        if count > 1:
            raise PatchError(f"{path}: pattern for '{label}' matches {count} times, expected 1")
        text = text.replace(old, new, 1)
        changed += 1
    path.write_text(text, encoding="utf-8")
    print(f"  OK    {path} ({changed} edit{'s' if changed != 1 else ''})")
    return True


NEW_COLLECTORS_CODE = '''

# ── Extended services (lightweight discovery only, no CW metrics) ───────
# One cheap describe/list call each, used ONLY to answer "does this
# account have any resources of this type right now" for the Services
# page tile filter (see app/api/live_data.py resource-counts endpoint).
# Cached the same way as the core collectors above.

def collect_nlb(region=None) -> list:
    return _cached(f"nlb_{region}", lambda: _nlb_raw(region))

def _nlb_raw(region) -> list:
    try:
        elb = get_session(region).client("elbv2")
        out = [lb for lb in elb.describe_load_balancers().get("LoadBalancers", [])
               if lb.get("Type") == "network"]
        logger.info(f"NLB: {len(out)} in {region}")
        return out
    except Exception as e:
        logger.error(f"NLB [{region}]: {e}"); return []


def collect_acm_certificates(region=None) -> list:
    return _cached(f"acm_{region}", lambda: _acm_raw(region))

def _acm_raw(region) -> list:
    try:
        acm = get_session(region).client("acm")
        out = []
        for page in acm.get_paginator("list_certificates").paginate():
            out.extend(page.get("CertificateSummaryList", []))
        logger.info(f"ACM: {len(out)} certificates in {region}")
        return out
    except Exception as e:
        logger.error(f"ACM [{region}]: {e}"); return []


def collect_backup_resources(region=None) -> list:
    return _cached(f"backup_{region}", lambda: _backup_raw(region))

def _backup_raw(region) -> list:
    try:
        backup = get_session(region).client("backup")
        out = []
        for page in backup.get_paginator("list_protected_resources").paginate():
            out.extend(page.get("Results", []))
        logger.info(f"Backup: {len(out)} protected resources in {region}")
        return out
    except Exception as e:
        logger.error(f"Backup [{region}]: {e}"); return []


def collect_dms_instances(region=None) -> list:
    return _cached(f"dms_{region}", lambda: _dms_raw(region))

def _dms_raw(region) -> list:
    try:
        dms = get_session(region).client("dms")
        out = []
        for page in dms.get_paginator("describe_replication_instances").paginate():
            out.extend(page.get("ReplicationInstances", []))
        logger.info(f"DMS: {len(out)} replication instances in {region}")
        return out
    except Exception as e:
        logger.error(f"DMS [{region}]: {e}"); return []


def collect_direct_connections(region=None) -> list:
    return _cached(f"directconnect_{region}", lambda: _directconnect_raw(region))

def _directconnect_raw(region) -> list:
    try:
        dx = get_session(region).client("directconnect")
        out = dx.describe_connections().get("connections", [])
        logger.info(f"Direct Connect: {len(out)} connections in {region}")
        return out
    except Exception as e:
        logger.error(f"Direct Connect [{region}]: {e}"); return []


def collect_state_machines(region=None) -> list:
    return _cached(f"states_{region}", lambda: _states_raw(region))

def _states_raw(region) -> list:
    try:
        sfn = get_session(region).client("stepfunctions")
        out = []
        for page in sfn.get_paginator("list_state_machines").paginate():
            out.extend(page.get("stateMachines", []))
        logger.info(f"Step Functions: {len(out)} state machines in {region}")
        return out
    except Exception as e:
        logger.error(f"Step Functions [{region}]: {e}"); return []

'''


def patch_collector_direct(repo_root: Path) -> Path:
    path = repo_root / "app" / "aws" / "collector_direct.py"
    marker = "def collect_state_machines"
    anchor = (
        "# ── ECS (unchanged — not in YACE config) ─────────────────────────────────\n"
        "\n"
        "def collect_ecs_clusters(region=None) -> list:"
    )
    replacements = [
        (
            anchor,
            NEW_COLLECTORS_CODE.strip("\n") + "\n\n\n" + anchor,
            "collector_direct.py: insert extended-service collectors",
        ),
    ]
    changed = apply_replacements(path, replacements, already_applied_marker=marker)
    return path if changed else None


def patch_live_data(repo_root: Path) -> Path:
    path = repo_root / "app" / "api" / "live_data.py"
    marker = '"directconnect":  collect_direct_connections,'

    old_import = (
        "from app.aws.collector_direct import (\n"
        "    collect_ec2_instances,\n"
        "    collect_ebs_volumes,\n"
        "    collect_rds_instances,\n"
        "    collect_lambda_functions,\n"
        "    collect_s3_buckets,\n"
        "    collect_elb,\n"
        "    collect_ecs_clusters,\n"
        "    get_account_summary,\n"
        "    get_ec2_metric_series,\n"
        "    get_s3_metric_series,\n"
        "    _get_ebs_metric_series,\n"
        "    _get_lambda_metric_series,\n"
        "    _get_rds_metric_series,\n"
        "    _get_elb_metric_series,\n"
        "    _get_ecs_metric_series,\n"
        ")\n"
    )
    new_import = (
        "from app.aws.collector_direct import (\n"
        "    collect_ec2_instances,\n"
        "    collect_ebs_volumes,\n"
        "    collect_rds_instances,\n"
        "    collect_lambda_functions,\n"
        "    collect_s3_buckets,\n"
        "    collect_elb,\n"
        "    collect_ecs_clusters,\n"
        "    collect_nlb,\n"
        "    collect_acm_certificates,\n"
        "    collect_backup_resources,\n"
        "    collect_dms_instances,\n"
        "    collect_direct_connections,\n"
        "    collect_state_machines,\n"
        "    get_account_summary,\n"
        "    get_ec2_metric_series,\n"
        "    get_s3_metric_series,\n"
        "    _get_ebs_metric_series,\n"
        "    _get_lambda_metric_series,\n"
        "    _get_rds_metric_series,\n"
        "    _get_elb_metric_series,\n"
        "    _get_ecs_metric_series,\n"
        ")\n"
    )

    old_block = (
        "# ── Real-time per-service resource counts ─────────────────────\n"
        "# Used by the Services page to decide whether a tile should be shown\n"
        "# at all — dynamically, based on whether the account actually HAS any\n"
        "# resources of that type right now, instead of only whether a metric\n"
        "# is selected for it. Only covers the 7 services with a live collector\n"
        "# (same ones the /ec2, /ebs, /rds, /lambda, /s3, /elb, /ecs endpoints\n"
        "# above use) — there's no resource-level collector yet for the\n"
        "# extended (metric-catalog-only) services, so those aren't included\n"
        "# here; the frontend treats a missing key as \"unknown\" and keeps that\n"
        "# tile visible rather than hiding it on a guess.\n"
        "_CORE_RESOURCE_COLLECTORS = {\n"
        "    \"ec2\":    collect_ec2_instances,\n"
        "    \"ebs\":    collect_ebs_volumes,\n"
        "    \"rds\":    collect_rds_instances,\n"
        "    \"lambda\": collect_lambda_functions,\n"
        "    \"s3\":     collect_s3_buckets,\n"
        "    \"elb\":    collect_elb,\n"
        "    \"ecs\":    collect_ecs_clusters,\n"
        "}\n"
        "\n"
        "\n"
        "@router.get(\"/resource-counts/{account_db_id}\")\n"
        "def live_resource_counts(account_db_id: int):\n"
        "    acc    = _get_db_account(account_db_id)\n"
        "    region = acc.get(\"default_region\")\n"
        "    counts = {}\n"
        "    for svc, collector in _CORE_RESOURCE_COLLECTORS.items():\n"
        "        try:\n"
        "            counts[svc] = len(collector(region))\n"
        "        except Exception as e:\n"
        "            # Unknown, not zero — a transient AWS/permissions error\n"
        "            # shouldn't hide a tile that may well have real resources.\n"
        "            logger.warning(f\"resource-counts: {svc} failed for account {account_db_id}: {e}\")\n"
        "            counts[svc] = None\n"
        "    return counts\n"
    )
    new_block = (
        "# ── Real-time per-service resource counts ─────────────────────\n"
        "# Used by the Services page to decide whether a tile should be shown\n"
        "# at all — dynamically, based on whether the account actually HAS any\n"
        "# resources of that type right now, instead of only whether a metric\n"
        "# is selected for it. Covers every service with a live collector,\n"
        "# core (ec2/ebs/rds/lambda/s3/alb/ecs) and extended\n"
        "# (nlb/acm/backup/dms/directconnect/states) alike; the frontend\n"
        "# treats a still-missing key (a service with no collector at all)\n"
        "# as \"unknown\" and keeps that tile visible rather than hiding it on\n"
        "# a guess, but every service listed here gets a real 0/N.\n"
        "_RESOURCE_COLLECTORS = {\n"
        "    \"ec2\":            collect_ec2_instances,\n"
        "    \"ebs\":            collect_ebs_volumes,\n"
        "    \"rds\":            collect_rds_instances,\n"
        "    \"lambda\":         collect_lambda_functions,\n"
        "    \"s3\":             collect_s3_buckets,\n"
        "    \"elb\":            collect_elb,\n"
        "    \"alb\":            collect_elb,\n"
        "    \"nlb\":            collect_nlb,\n"
        "    \"ecs\":            collect_ecs_clusters,\n"
        "    \"acm\":            collect_acm_certificates,\n"
        "    \"backup\":         collect_backup_resources,\n"
        "    \"dms\":            collect_dms_instances,\n"
        "    \"directconnect\":  collect_direct_connections,\n"
        "    \"states\":         collect_state_machines,\n"
        "}\n"
        "\n"
        "\n"
        "@router.get(\"/resource-counts/{account_db_id}\")\n"
        "def live_resource_counts(account_db_id: int):\n"
        "    acc    = _get_db_account(account_db_id)\n"
        "    region = acc.get(\"default_region\")\n"
        "    counts = {}\n"
        "    for svc, collector in _RESOURCE_COLLECTORS.items():\n"
        "        try:\n"
        "            counts[svc] = len(collector(region))\n"
        "        except Exception as e:\n"
        "            # Unknown, not zero — a transient AWS/permissions error\n"
        "            # shouldn't hide a tile that may well have real resources.\n"
        "            logger.warning(f\"resource-counts: {svc} failed for account {account_db_id}: {e}\")\n"
        "            counts[svc] = None\n"
        "    return counts\n"
    )

    replacements = [
        (old_import, new_import, "live_data.py: import new collectors"),
        (old_block, new_block, "live_data.py: expand resource-counts map"),
    ]
    changed = apply_replacements(path, replacements, already_applied_marker=marker)
    return path if changed else None


def patch_service_list_jsx(repo_root: Path) -> Path:
    path = repo_root / "frontend" / "src" / "pages" / "ServiceList.jsx"
    marker = "ANY service (core or extended) is hidden"

    old_comment_and_filter = (
        "  // Dynamic, aligned with the metric selector: a service tile only shows up\n"
        "  // here if it has at least one metric enabled for THIS account — the same\n"
        "  // selection made during onboarding or later edited in Settings -> Metrics.\n"
        "  // On top of that, a CORE service tile is hidden if we have a real,\n"
        "  // confirmed-zero resource count for it. Extended services (no live\n"
        "  // collector exists for them yet) and non-AWS accounts always stay\n"
        "  // visible — clicking an extended tile opens the AWS Console directly\n"
        "  // (see openInConsole above) rather than a broken internal link.\n"
        "  const activeServices = useMemo(() => {\n"
        "    return groups\n"
        "      .filter(g => (g.metrics || []).some(m => m.enabled))\n"
        "      .filter(g => {\n"
        "        if (!resourceCounts) return true;\n"
        "        if (!CORE_AWS_SERVICES.has(g.service)) return true;\n"
        "        const count = resourceCounts[g.service];\n"
        "        return count === undefined || count === null || count > 0;\n"
        "      })\n"
        "      .map((g, i) => {\n"
        "        const resourceCount = CORE_AWS_SERVICES.has(g.service) && resourceCounts\n"
        "          ? (resourceCounts[g.service] ?? null)\n"
        "          : null;\n"
    )
    new_comment_and_filter = (
        "  // Dynamic, aligned with the metric selector: a service tile only shows up\n"
        "  // here if it has at least one metric enabled for THIS account — the same\n"
        "  // selection made during onboarding or later edited in Settings -> Metrics.\n"
        "  // On top of that, ANY service (core or extended) is hidden if we have a\n"
        "  // real, confirmed-zero resource count for it — see GET\n"
        "  // /api/live/resource-counts/{id}, which now covers both tiers. A service\n"
        "  // still missing a collector (key absent from that response) or a\n"
        "  // non-AWS account (resourceCounts never populated) fails OPEN and stays\n"
        "  // visible, since \"unknown\" is not the same as \"confirmed none\".\n"
        "  const activeServices = useMemo(() => {\n"
        "    return groups\n"
        "      .filter(g => (g.metrics || []).some(m => m.enabled))\n"
        "      .filter(g => {\n"
        "        if (!resourceCounts) return true;\n"
        "        const count = resourceCounts[g.service];\n"
        "        return count === undefined || count === null || count > 0;\n"
        "      })\n"
        "      .map((g, i) => {\n"
        "        const resourceCount = resourceCounts\n"
        "          ? (resourceCounts[g.service] ?? null)\n"
        "          : null;\n"
    )

    replacements = [
        (old_comment_and_filter, new_comment_and_filter, "ServiceList.jsx: hide any service tile on confirmed-zero count"),
    ]
    changed = apply_replacements(path, replacements, already_applied_marker=marker)
    return path if changed else None


def main():
    repo_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    print(f"Repo root: {repo_root}")

    touched_py_files = []
    any_change = False

    print("\n[1/3] app/aws/collector_direct.py — add extended-service collectors")
    p = patch_collector_direct(repo_root)
    if p:
        touched_py_files.append(p)
        any_change = True

    print("\n[2/3] app/api/live_data.py — wire extended collectors into resource-counts")
    p = patch_live_data(repo_root)
    if p:
        touched_py_files.append(p)
        any_change = True

    print("\n[3/3] frontend/src/pages/ServiceList.jsx — hide any tile on confirmed-zero count")
    if patch_service_list_jsx(repo_root):
        any_change = True

    if touched_py_files:
        print("\nCompiling patched Python files...")
        try:
            for f in touched_py_files:
                py_compile.compile(str(f), doraise=True)
            print("  OK    all patched .py files compile cleanly")
        except py_compile.PyCompileError as e:
            print(f"  FAIL  {e}\n  Reverting changes...")
            for f in touched_py_files:
                bak = f.with_name(f.name + BAK_SUFFIX)
                if bak.exists():
                    shutil.copy2(bak, f)
            print("  Reverted. No changes applied.")
            sys.exit(1)

    if any_change:
        print(
            "\nDone. Restart the backend (uvicorn/gunicorn process) for the new\n"
            "endpoints to take effect, and rebuild/reload the frontend.\n"
            "\n"
            "Note: the new collectors call ACM, AWS Backup, DMS, Direct Connect\n"
            "and Step Functions describe/list APIs. Make sure the IAM role used\n"
            "for each account has read access to those services, or those\n"
            "services' tiles will fail open (stay visible) instead of hiding —\n"
            "check backend logs for 'resource-counts: <svc> failed' warnings."
        )
    else:
        print("\nNothing to do — already patched.")


if __name__ == "__main__":
    main()
