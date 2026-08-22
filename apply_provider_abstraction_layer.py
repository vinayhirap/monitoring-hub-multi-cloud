#!/usr/bin/env python3
"""
apply_provider_abstraction_layer.py

Step 2 of the multi-cloud refactor plan (see multi-cloud-architecture-
assessment.md, section 6, item 2), plus a real bug fix discovered while
building it.

WHAT THIS ADDS (new files only — nothing existing is modified except the
one bug fix below):
    app/providers/__init__.py       - registers built-in providers on import
    app/providers/base.py           - CloudProvider ABC (the interface)
    app/providers/registry.py       - get_provider("aws") -> AWSProvider()
    app/providers/aws/__init__.py   - registers AWSProvider
    app/providers/aws/provider.py   - AWSProvider: thin wrapper around your
                                       EXISTING app/aws/* and
                                       app/collector/discovery/* modules.
                                       No AWS logic is duplicated or
                                       reimplemented here — every method
                                       just calls the real function that
                                       already does the work.

This is additive and inert: nothing in the running app imports
app.providers yet except the one fix below, so AWS behavior is unchanged
until a future step (onboarding/discovery/console-url endpoints) is
migrated to call through the provider layer instead of app.aws.* directly.

WHAT THIS FIXES (real bug, not multi-cloud scope creep):
    app/api/admin/accounts.py's POST /{account_id}/discover endpoint (the
    "Discover Now" button) imports `discover_aurogov_ec2` from
    app.collector.discovery_ec2 — a function that does not exist anywhere
    in the codebase. That endpoint throws ImportError -> 500 every time
    it's called. The scheduler's own automatic discovery (every 15 min)
    uses the real, live path: app.collector.discovery.runner.run_discovery().
    This patch points the button at the same real path, routed through
    the new AWSProvider so it's also the first real caller proving the
    provider layer is wired correctly.

    Known limitation carried over unchanged (not introduced by this
    patch): run_discovery() discovers ALL active accounts, not just the
    one whose "Discover Now" button was clicked — that was already true
    of the dead discover_ec2()/discover_aurogov_ec2() functions it
    replaces. Scoping discovery to a single account is a separate,
    future improvement, not fixed here.

Run from the project root:
    python apply_provider_abstraction_layer.py --dry-run
    python apply_provider_abstraction_layer.py

Safe to re-run: new files are skipped if already present (use --force to
overwrite). The accounts.py edit is backed up to
app/api/admin/accounts.py.bak.pre-provider-layer before changing anything,
verified with an exact occurrence count first (aborts without touching
anything if the anchor text doesn't match exactly once — i.e. if your
local file has already diverged from what this script expects), and
validated with py_compile after writing (auto-reverts from the .bak on
any syntax error).
"""

import argparse
import py_compile
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def die(msg):
    print(f"\n[ABORTED] {msg}")
    print("No files were modified.")
    sys.exit(1)


# ── New files for the provider abstraction layer ────────────────────────

NEW_FILES = {
    "app/providers/__init__.py": '''"""
Importing this package registers all built-in cloud providers with the
registry. Call app.providers.registry.get_provider("aws") after this
import to get a working AWSProvider instance.
"""
from app.providers import aws  # noqa: F401  (import registers AWSProvider)
''',

    "app/providers/base.py": '''"""
CloudProvider — the common interface every cloud adapter implements.

This mirrors the capability list from the multi-cloud architecture plan
(authenticate/validate, discover, get metrics, console URLs, etc.) but
only includes methods that have a concrete, currently-real AWS
implementation to wrap. Azure/GCP providers implement the same interface
in later steps; methods a given provider can't support should raise
NotImplementedError with a clear message rather than silently no-op or
fake a result (per the project's "do not fake support" principle).

Return values are plain dicts, matching the rest of this codebase's
convention (mysql.connector dictionary=True cursors) rather than
introducing a new dataclass style.
"""
from abc import ABC, abstractmethod


class CloudProvider(ABC):
    """Base interface for a cloud provider adapter (AWS, Azure, GCP, ...)."""

    #: short lowercase identifier, e.g. "aws" — must match the `provider`
    #: column value used in aws_accounts / metric_catalog.
    name: str

    @abstractmethod
    def validate_credentials(self, account: dict) -> dict:
        """
        Verify the credentials stored for `account` actually work.
        Returns a dict describing what was verified (e.g. the identity
        assumed). Raises on failure — callers turn that into an HTTP error.
        """
        raise NotImplementedError

    @abstractmethod
    def get_console_url(self, account: dict, resource_id: str, region: str) -> str:
        """
        Return a deep link into this provider's web console for the given
        resource, scoped to the correct account/subscription/project.
        """
        raise NotImplementedError

    @abstractmethod
    def discover_resources(self) -> None:
        """
        Run resource discovery for all active accounts of this provider.
        Side-effecting: writes/updates rows in the `resources` table, same
        contract as the existing scheduler-driven discovery.
        """
        raise NotImplementedError

    @abstractmethod
    def get_metric_catalog(self) -> dict:
        """
        Return this provider's metric catalog in the existing
        {service_key: (display_name, namespace, category, [metrics...])}
        shape used by app.aws.metric_catalog_data.CURATED.
        """
        raise NotImplementedError
''',

    "app/providers/registry.py": '''"""
Simple name -> provider-class registry. Each provider package (e.g.
app.providers.aws) calls register() on import; app.providers/__init__.py
imports all of them so the registry is populated as soon as anyone does
`import app.providers`.
"""
from app.providers.base import CloudProvider

_REGISTRY: dict[str, type[CloudProvider]] = {}


def register(name: str, provider_cls: type[CloudProvider]) -> None:
    _REGISTRY[name] = provider_cls


def get_provider(name: str) -> CloudProvider:
    """Return a new instance of the provider registered under `name`."""
    try:
        provider_cls = _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"No provider registered for '{name}'. "
            f"Registered providers: {sorted(_REGISTRY.keys())}"
        )
    return provider_cls()


def available_providers() -> list[str]:
    return sorted(_REGISTRY.keys())
''',

    "app/providers/aws/__init__.py": '''from app.providers.registry import register
from app.providers.aws.provider import AWSProvider

register("aws", AWSProvider)
''',

    "app/providers/aws/provider.py": '''"""
AWSProvider — CloudProvider implementation for AWS.

Deliberately a THIN WRAPPER: every method below delegates to the real,
existing AWS logic (app.aws.sts, app.aws.federation,
app.collector.discovery.runner, app.aws.metric_catalog_data). Nothing is
reimplemented here, so this file changing behavior for AWS is not
possible by construction — it just gives that existing logic a common
name other code (and eventually Azure/GCP) can call through.
"""
from app.providers.base import CloudProvider


class AWSProvider(CloudProvider):
    name = "aws"

    def validate_credentials(self, account: dict) -> dict:
        from app.aws.sts import assume_role

        role_arn = (account.get("role_arn") or "").strip()
        external_id = account.get("external_id")
        if not role_arn or not role_arn.startswith("arn:aws:"):
            raise ValueError("Valid IAM Role ARN required")

        session = assume_role(role_arn, external_id)
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        return {
            "status": "success",
            "assumed_account": identity["Account"],
            "assumed_arn": identity["Arn"],
        }

    def get_console_url(self, account: dict, resource_id: str, region: str) -> str:
        from app.aws.federation import (
            build_federated_console_url,
            resource_console_destination,
        )

        destination = resource_console_destination(resource_id, region)
        return build_federated_console_url(
            account.get("role_arn"), account.get("external_id"), destination
        )

    def discover_resources(self) -> None:
        # The real, live discovery path — same one the scheduler calls
        # every 15 minutes. NOT app.collector.discovery_ec2, which is
        # dead code (see the module docstring on that file).
        from app.collector.discovery.runner import run_discovery

        run_discovery()

    def get_metric_catalog(self) -> dict:
        from app.aws.metric_catalog_data import CURATED

        return CURATED
''',
}


# ── Bug fix: admin/accounts.py discover_account() broken import ─────────

ACCOUNTS_PY = REPO_ROOT / "app" / "api" / "admin" / "accounts.py"

ACCOUNTS_OLD = '''    try:
        from app.collector.discovery_ec2 import discover_aurogov_ec2
        discover_aurogov_ec2()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery failed: {str(e)}")'''

ACCOUNTS_NEW = '''    try:
        # Was: app.collector.discovery_ec2.discover_aurogov_ec2 — that
        # function does not exist anywhere in the codebase; this endpoint
        # threw ImportError -> 500 on every click. Fixed to go through
        # the real, live discovery path (the same one the scheduler calls
        # every 15 minutes), routed via the provider layer.
        from app.providers.registry import get_provider
        get_provider("aws").discover_resources()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Discovery failed: {str(e)}")'''


def require_exactly_one(text: str, needle: str, filename: str):
    count = text.count(needle)
    if count == 0:
        die(f"Anchor text not found in {filename} — the file has likely drifted "
            f"since this script was written (same thing that happened with the "
            f"DB migration). Aborting without changes so nothing gets corrupted.\n"
            f"--- missing anchor ---\n{needle}")
    if count > 1:
        die(f"Anchor text found {count} times in {filename} (expected exactly once) — "
            f"refusing to guess which one to patch.")


def write_new_files(dry_run: bool, force: bool):
    for rel_path, content in NEW_FILES.items():
        path = REPO_ROOT / rel_path
        if path.exists() and not force:
            print(f"SKIP (already exists): {rel_path}")
            continue

        print(f"{'[DRY RUN] would write' if dry_run else 'WRITE'}: {rel_path}")
        if dry_run:
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as e:
            path.unlink(missing_ok=True)
            die(f"Syntax error in generated file {rel_path}, removed it:\n{e}")

        print(f"  OK: {rel_path} (compiles cleanly)")


def patch_accounts_py(dry_run: bool):
    if not ACCOUNTS_PY.exists():
        die(f"Expected file not found: {ACCOUNTS_PY}\n"
            f"Run this script from the project root.")

    text = ACCOUNTS_PY.read_text(encoding="utf-8")

    if ACCOUNTS_NEW in text:
        print("SKIP (already patched): app/api/admin/accounts.py")
        return

    require_exactly_one(text, ACCOUNTS_OLD, "app/api/admin/accounts.py")

    print(f"{'[DRY RUN] would patch' if dry_run else 'PATCH'}: app/api/admin/accounts.py "
          f"(discover_account() broken import)")
    if dry_run:
        return

    backup_path = ACCOUNTS_PY.with_suffix(ACCOUNTS_PY.suffix + ".bak.pre-provider-layer")
    backup_path.write_text(text, encoding="utf-8")
    print(f"  Backup: {backup_path}")

    new_text = text.replace(ACCOUNTS_OLD, ACCOUNTS_NEW)
    ACCOUNTS_PY.write_text(new_text, encoding="utf-8")

    try:
        py_compile.compile(str(ACCOUNTS_PY), doraise=True)
    except py_compile.PyCompileError as e:
        ACCOUNTS_PY.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
        die(f"Syntax error after patching accounts.py — reverted from backup.\n{e}")

    print("  OK: app/api/admin/accounts.py (compiles cleanly)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true",
                         help="Overwrite provider-layer files even if they already exist")
    args = parser.parse_args()

    print("=== New files: app/providers/ ===")
    write_new_files(args.dry_run, args.force)

    print("\n=== Bug fix: app/api/admin/accounts.py ===")
    patch_accounts_py(args.dry_run)

    if args.dry_run:
        print("\n--dry-run: no changes made.")
    else:
        print("\nDone. Restart uvicorn to pick up the new app/providers package "
              "and verify with: python -c \"from app.providers.registry import "
              "get_provider; print(get_provider('aws').name)\"")


if __name__ == "__main__":
    main()
