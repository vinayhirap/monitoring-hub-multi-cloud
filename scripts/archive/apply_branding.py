#!/usr/bin/env python3
"""
apply_console_url_consolidation_backend.py

Step 3 of the multi-cloud refactor plan — BACKEND HALF ONLY. Frontend
(ServiceDetail.jsx, AccountDetail.jsx, api.js) is a separate patch,
pending confirmation of those files' current local content.

WHAT THIS FIXES:
    app/aws/federation.py's resource_console_destination() only guessed
    the AWS service from the resource-ID's shape (i-/vol-/lambda/db-
    prefixes) — it had NO support for S3, ELB, or ECS at all, and those
    always fell through to a generic console-home link. Meanwhile
    frontend/src/pages/ServiceDetail.jsx had its OWN, separate, MORE
    complete mapping (awsConsoleLink/awsDeepLink — all 7 services). Two
    diverging copies of the same logic, and the backend one was the
    worse of the two.

WHAT THIS DOES:
    1. Upgrades resource_console_destination() to take an explicit
       `service` argument and dispatch on it directly (no more
       ID-shape-guessing as the primary path), covering all 7 services
       to match ServiceDetail's table. The old guessing logic is kept
       as a private fallback (_legacy_prefix_guess_destination) for any
       caller that doesn't pass `service` — so nothing regresses for
       code not yet updated.
    2. Adds service_console_list_url() for the "view all X" case (no
       specific resource selected).
    3. Updates the alerts.py console-url endpoint to pass the alert's
       actual resource_type + resource name (both already sitting in
       the `resources` table) instead of guessing from the ID string.
    4. Extends CloudProvider.get_console_url / AWSProvider.get_console_url
       (added in step 2) to accept the same service/resource_name/
       ecs_service_name parameters.
    5. Adds a new generic endpoint:
           GET /api/admin/accounts/{account_id}/console-url
               ?service=...&resource_id=...&region=...
               &resource_name=...&ecs_service_name=...
       — this is what the (forthcoming) frontend patch will call from
       ServiceDetail.jsx and AccountDetail.jsx instead of building URLs
       client-side, same pattern Alerts.jsx already uses today.

Run from the project root:
    python apply_console_url_consolidation_backend.py --dry-run
    python apply_console_url_consolidation_backend.py

Safe to re-run: detects already-applied changes and skips them. Each
edited file is backed up to <file>.bak.pre-console-consolidation before
being touched, verified with an exact occurrence count first (aborts
without changes if the anchor doesn't match exactly once — i.e. if this
file has drifted locally), and validated with py_compile after writing
(auto-reverts from the .bak on any syntax error).
"""

import argparse
import py_compile
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

FEDERATION_PATH = REPO_ROOT / "app" / "aws" / "federation.py"
ALERTS_PATH = REPO_ROOT / "app" / "api" / "alerts.py"
PROVIDER_BASE_PATH = REPO_ROOT / "app" / "providers" / "base.py"
PROVIDER_AWS_PATH = REPO_ROOT / "app" / "providers" / "aws" / "provider.py"
ADMIN_ACCOUNTS_PATH = REPO_ROOT / "app" / "api" / "admin" / "accounts.py"


def die(msg):
    print(f"\n[ABORTED] {msg}")
    print("No further files were modified.")
    sys.exit(1)


def load(path: Path) -> str:
    if not path.exists():
        die(f"Expected file not found: {path}\nRun this script from the project root.")
    return path.read_text(encoding="utf-8")


def require_exactly_one(text: str, needle: str, filename: str):
    count = text.count(needle)
    if count == 0:
        die(f"Anchor text not found in {filename} — the file has likely drifted "
            f"locally since this script was written. Aborting without changes.\n"
            f"--- missing anchor (first 200 chars) ---\n{needle[:200]}")
    if count > 1:
        die(f"Anchor text found {count} times in {filename} (expected exactly once) — "
            f"refusing to guess which one to patch.")


def patch_file(path: Path, old: str, new: str, label: str, dry_run: bool):
    text = load(path)
    rel = path.relative_to(REPO_ROOT)

    if new in text:
        print(f"SKIP (already patched): {rel} — {label}")
        return

    require_exactly_one(text, old, str(rel))

    print(f"{'[DRY RUN] would patch' if dry_run else 'PATCH'}: {rel} — {label}")
    if dry_run:
        return

    backup_path = path.with_suffix(path.suffix + ".bak.pre-console-consolidation")
    backup_path.write_text(text, encoding="utf-8")
    print(f"  Backup: {backup_path.relative_to(REPO_ROOT)}")

    new_text = text.replace(old, new)
    path.write_text(new_text, encoding="utf-8")

    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as e:
        path.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
        die(f"Syntax error after patching {rel} — reverted from backup.\n{e}")

    print(f"  OK: {rel} (compiles cleanly)")


# ── app/aws/federation.py ────────────────────────────────────────────────

FEDERATION_OLD = '''def resource_console_destination(resource: str, region: str) -> str:
    """
    Resource-type-specific AWS Console deep link.

    Mirrors the mapping in frontend/src/pages/Alerts.jsx (awsConsoleUrl) —
    keep both in sync if a new resource type is added.
    """
    region = region or "us-east-1"
    if not resource:
        return f"https://{region}.console.aws.amazon.com/console/home?region={region}"

    if resource.startswith("i-"):
        return (f"https://{region}.console.aws.amazon.com/ec2/home"
                f"?region={region}#Instances:instanceId={resource}")
    if resource.startswith("vol-"):
        return (f"https://{region}.console.aws.amazon.com/ec2/home"
                f"?region={region}#Volumes:volumeId={resource}")
    if "lambda" in resource or resource.startswith("arn:aws:lambda"):
        fn = resource.split(":")[-1]
        return (f"https://{region}.console.aws.amazon.com/lambda/home"
                f"?region={region}#/functions/{fn}")
    if resource.startswith("db-") or "rds" in resource:
        return f"https://{region}.console.aws.amazon.com/rds/home?region={region}#database:"

    return f"https://{region}.console.aws.amazon.com/console/home?region={region}"'''

FEDERATION_NEW = '''def service_console_list_url(service: str, region: str) -> str:
    """
    List-view console URL for a whole service (e.g. all EC2 instances) —
    used when no specific resource is selected yet.
    """
    region = region or "us-east-1"
    service = (service or "").lower()
    base = f"https://{region}.console.aws.amazon.com"
    return {
        "ec2":    f"{base}/ec2/home?region={region}#Instances:",
        "ebs":    f"{base}/ec2/home?region={region}#Volumes:",
        "rds":    f"{base}/rds/home?region={region}#databases:",
        "lambda": f"{base}/lambda/home?region={region}#/functions",
        "s3":     "https://s3.console.aws.amazon.com/s3/buckets",
        "elb":    f"{base}/ec2/home?region={region}#LoadBalancers:",
        "ecs":    f"{base}/ecs/home?region={region}",
    }.get(service, f"{base}/console/home?region={region}")


def resource_console_destination(service: str, resource_id: str, region: str,
                                  resource_name: str | None = None,
                                  ecs_service_name: str | None = None) -> str:
    """
    Resource-type-specific AWS Console deep link.

    `service` should be one of the resources.resource_type values
    (ec2/ebs/rds/lambda/s3/elb/ecs — case-insensitive). This is the
    single source of truth for console-link construction — the same
    mapping frontend/src/pages/ServiceDetail.jsx used to keep as its own
    separate copy (see multi-cloud-architecture-assessment.md section
    2.3); that copy is being retired in favor of calling through here.

    `resource_name` is used where the console needs a display name
    rather than an ARN/ID (e.g. ELB search-by-name). `ecs_service_name`
    enables the deeper cluster > service link for ECS when known;
    without it, ECS falls back to the cluster-level view.

    If `service` is missing/unrecognized (an older caller that hasn't
    been updated yet), falls back to the original ID-prefix-guessing
    behavior so nothing regresses for callers not yet passing `service`.
    """
    region = region or "us-east-1"
    if not resource_id:
        return service_console_list_url(service, region)

    svc = (service or "").lower()
    base = f"https://{region}.console.aws.amazon.com"

    if svc == "ec2":
        return f"{base}/ec2/home?region={region}#Instances:instanceId={resource_id}"
    if svc == "ebs":
        return f"{base}/ec2/home?region={region}#Volumes:volumeId={resource_id}"
    if svc == "rds":
        return f"{base}/rds/home?region={region}#database:id={resource_id}"
    if svc == "lambda":
        return f"{base}/lambda/home?region={region}#/functions/{resource_id}"
    if svc == "s3":
        return f"https://s3.console.aws.amazon.com/s3/buckets/{resource_id}"
    if svc == "elb":
        search_term = resource_name or resource_id
        return f"{base}/ec2/home?region={region}#LoadBalancers:search={search_term}"
    if svc == "ecs":
        cluster = resource_name or resource_id
        if ecs_service_name:
            return (f"{base}/ecs/home?region={region}"
                    f"#/clusters/{cluster}/services/{ecs_service_name}")
        return f"{base}/ecs/home?region={region}#/clusters/{cluster}"

    return _legacy_prefix_guess_destination(resource_id, region)


def _legacy_prefix_guess_destination(resource: str, region: str) -> str:
    """
    Original ID-shape-guessing dispatch, kept as a fallback for any
    caller that doesn't pass an explicit `service`. Covers only
    EC2/EBS/Lambda/RDS — identical to this file's behavior before this
    patch, no S3/ELB/ECS support on this path.
    """
    region = region or "us-east-1"
    if not resource:
        return f"https://{region}.console.aws.amazon.com/console/home?region={region}"

    if resource.startswith("i-"):
        return (f"https://{region}.console.aws.amazon.com/ec2/home"
                f"?region={region}#Instances:instanceId={resource}")
    if resource.startswith("vol-"):
        return (f"https://{region}.console.aws.amazon.com/ec2/home"
                f"?region={region}#Volumes:volumeId={resource}")
    if "lambda" in resource or resource.startswith("arn:aws:lambda"):
        fn = resource.split(":")[-1]
        return (f"https://{region}.console.aws.amazon.com/lambda/home"
                f"?region={region}#/functions/{fn}")
    if resource.startswith("db-") or "rds" in resource:
        return f"https://{region}.console.aws.amazon.com/rds/home?region={region}#database:"

    return f"https://{region}.console.aws.amazon.com/console/home?region={region}"'''


# ── app/api/alerts.py ────────────────────────────────────────────────────

ALERTS_QUERY_OLD = '''    cursor.execute("""
        SELECT
            a.resource_id                          AS resource,
            COALESCE(a.region, acc.default_region) AS region,
            acc.account_id                         AS aws_account_id,
            acc.role_arn,
            acc.external_id
        FROM alerts a
        JOIN resources r      ON r.resource_id = a.resource_id
        JOIN aws_accounts acc ON acc.id = r.aws_account_id
        WHERE a.id = %s
    """, (alert_id,))'''

ALERTS_QUERY_NEW = '''    cursor.execute("""
        SELECT
            a.resource_id                          AS resource,
            r.resource_type                        AS resource_type,
            r.name                                  AS resource_name,
            COALESCE(a.region, acc.default_region) AS region,
            acc.account_id                         AS aws_account_id,
            acc.role_arn,
            acc.external_id
        FROM alerts a
        JOIN resources r      ON r.resource_id = a.resource_id
        JOIN aws_accounts acc ON acc.id = r.aws_account_id
        WHERE a.id = %s
    """, (alert_id,))'''

ALERTS_CALL_OLD = '''    destination = resource_console_destination(row["resource"], row["region"])'''

ALERTS_CALL_NEW = '''    destination = resource_console_destination(
        row.get("resource_type"), row["resource"], row["region"],
        resource_name=row.get("resource_name"),
    )'''


# ── app/providers/base.py ────────────────────────────────────────────────

BASE_OLD = '''    @abstractmethod
    def get_console_url(self, account: dict, resource_id: str, region: str) -> str:
        """
        Return a deep link into this provider's web console for the given
        resource, scoped to the correct account/subscription/project.
        """
        raise NotImplementedError'''

BASE_NEW = '''    @abstractmethod
    def get_console_url(self, account: dict, resource_id: str, region: str,
                         service: str | None = None,
                         resource_name: str | None = None,
                         ecs_service_name: str | None = None) -> str:
        """
        Return a deep link into this provider's web console for the given
        resource, scoped to the correct account/subscription/project.

        `service` is the normalized/native resource-type key (e.g. "ec2",
        "ecs") and lets the provider dispatch precisely instead of
        guessing from `resource_id`'s shape. `resource_name` and
        `ecs_service_name` are optional extra identifiers some services
        need for a fully-specific deep link (AWS ELB search-by-name and
        ECS cluster>service, respectively) — providers that don't need
        them can ignore the arguments.
        """
        raise NotImplementedError'''


# ── app/providers/aws/provider.py ────────────────────────────────────────

AWS_PROVIDER_OLD = '''    def get_console_url(self, account: dict, resource_id: str, region: str) -> str:
        from app.aws.federation import (
            build_federated_console_url,
            resource_console_destination,
        )

        destination = resource_console_destination(resource_id, region)
        return build_federated_console_url(
            account.get("role_arn"), account.get("external_id"), destination
        )'''

AWS_PROVIDER_NEW = '''    def get_console_url(self, account: dict, resource_id: str, region: str,
                         service: str | None = None,
                         resource_name: str | None = None,
                         ecs_service_name: str | None = None) -> str:
        from app.aws.federation import (
            build_federated_console_url,
            resource_console_destination,
        )

        destination = resource_console_destination(
            service, resource_id, region,
            resource_name=resource_name, ecs_service_name=ecs_service_name,
        )
        return build_federated_console_url(
            account.get("role_arn"), account.get("external_id"), destination
        )'''


# ── app/api/admin/accounts.py ────────────────────────────────────────────

ADMIN_IMPORT_OLD = '''from fastapi import APIRouter, HTTPException, Body'''
ADMIN_IMPORT_NEW = '''from fastapi import APIRouter, HTTPException, Body, Query'''

ADMIN_NEW_ENDPOINT_ANCHOR = '''@router.post("/test-role")'''

ADMIN_NEW_ENDPOINT = '''@router.get("/{account_id}/console-url")
def get_account_console_url(
    account_id: int,
    service: str = Query(None),
    resource_id: str = Query(None),
    region: str = Query(None),
    resource_name: str = Query(None),
    ecs_service_name: str = Query(None),
):
    """
    Generic account-scoped console deep link — the single backend source
    ServiceDetail/AccountDetail call instead of building console URLs
    client-side (same pattern the Alerts page already used). Dispatches
    through the provider layer so this also works for Azure/GCP once
    those providers implement get_console_url.
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM aws_accounts WHERE id = %s AND status = 'active'", (account_id,))
    account = cursor.fetchone()
    cursor.close()
    conn.close()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found or inactive")
    if not account.get("role_arn"):
        raise HTTPException(status_code=400, detail="No AWS role configured for this account")

    region = region or account.get("default_region")

    try:
        from app.providers.registry import get_provider
        provider = get_provider(account.get("provider") or "aws")
        url = provider.get_console_url(
            account, resource_id, region,
            service=service, resource_name=resource_name,
            ecs_service_name=ecs_service_name,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not generate console link: {e}")

    return {"url": url, "account_id": account["account_id"]}


@router.post("/test-role")'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    dry = args.dry_run

    patch_file(FEDERATION_PATH, FEDERATION_OLD, FEDERATION_NEW,
               "service-aware console destination builder", dry)

    patch_file(ALERTS_PATH, ALERTS_QUERY_OLD, ALERTS_QUERY_NEW,
               "add resource_type/resource_name to console-url query", dry)
    patch_file(ALERTS_PATH, ALERTS_CALL_OLD, ALERTS_CALL_NEW,
               "pass resource_type/resource_name to destination builder", dry)

    patch_file(PROVIDER_BASE_PATH, BASE_OLD, BASE_NEW,
               "extend get_console_url interface signature", dry)
    patch_file(PROVIDER_AWS_PATH, AWS_PROVIDER_OLD, AWS_PROVIDER_NEW,
               "extend AWSProvider.get_console_url signature", dry)

    patch_file(ADMIN_ACCOUNTS_PATH, ADMIN_IMPORT_OLD, ADMIN_IMPORT_NEW,
               "import Query", dry)
    patch_file(ADMIN_ACCOUNTS_PATH, ADMIN_NEW_ENDPOINT_ANCHOR, ADMIN_NEW_ENDPOINT,
               "add GET /{account_id}/console-url endpoint", dry)

    if dry:
        print("\n--dry-run: no changes made.")
    else:
        print("\nDone. Restart uvicorn, then sanity-check with:")
        print('  curl "http://127.0.0.1:8000/api/admin/accounts/<id>/console-url?service=ec2&resource_id=i-xxxx&region=ap-south-1"')


if __name__ == "__main__":
    main()