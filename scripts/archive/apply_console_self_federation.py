#!/usr/bin/env python3
"""
apply_console_self_federation.py

Fixes: "Couldn't open AWS console: No AWS role configured for this account"
for AWS accounts that ARE the server's own AWS account (e.g. AuroGov
Mumbai, account 924922671984 — the same account the EC2 server runs in).

Root cause
----------
app/aws/federation.py's build_federated_console_url() always required a
cross-account role_arn to be pre-created and saved on the account, even
when the target account is the server's own account. In that case no
cross-account AssumeRole is needed at all — AWS lets you mint a
session-scoped, read-only sign-in token directly from your own identity
via STS GetFederationToken. This patch adds that path as an automatic
fallback:

  1. role_arn is set on the account          -> AssumeRole (cross-account,
                                                  unavoidable — AWS security
                                                  boundary, not a design
                                                  choice; same one-time
                                                  field onboarding already
                                                  collects)
  2. role_arn empty AND target account id ==
     the server's own AWS account id          -> auto self-federation via
                                                  GetFederationToken.
                                                  Zero config, zero IAM
                                                  role to create.
  3. role_arn empty AND target account id !=
     server's own account                     -> still errors (nothing
                                                  else is possible without
                                                  a role to assume)

Server's own account id is discovered live via sts.get_caller_identity()
against the process's existing credentials (the EC2 instance role) and
cached in-memory for 5 minutes.

Files touched:
  - app/aws/sts.py                 (+ get_own_account_id, + get_self_federation_session)
  - app/aws/federation.py          (build_federated_console_url gains self-federation branch + NoConsoleCredentialsError)
  - app/api/alerts.py              (drop hard role_arn gate, catch NoConsoleCredentialsError -> 400)
  - app/api/admin/accounts.py      (same, for the account/service-detail console endpoint)
  - app/providers/aws/provider.py  (pass target_account_id through)

Usage:
    python apply_console_self_federation.py --dry-run
    python apply_console_self_federation.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-console-self-federation"


class PatchError(Exception):
    pass


# Each entry: (relative_path, [(old, new), ...])
PATCHES = []

# ─────────────────────────────────────────────────────────────────────────
# 1. app/aws/sts.py — add get_own_account_id() + get_self_federation_session()
# ─────────────────────────────────────────────────────────────────────────
STS_OLD = '''import boto3
from botocore.exceptions import ClientError

def assume_role(role_arn: str, external_id: str | None = None):'''

STS_NEW = '''import time

import boto3
from botocore.exceptions import ClientError

# In-memory cache for the server's own AWS account id (discovered via
# STS GetCallerIdentity against whatever credentials the process already
# has — the EC2 instance role in production). Avoids hammering STS on
# every console-link click.
_own_account_cache = {"account_id": None, "checked_at": 0.0}
_OWN_ACCOUNT_CACHE_TTL_SECONDS = 300


def get_own_account_id() -> str | None:
    """
    Returns the AWS account id the server's OWN credentials belong to,
    or None if it can't be determined (e.g. no credentials available).
    Used to detect the "target account IS the server's own account" case,
    where no cross-account role is needed at all.
    """
    now = time.time()
    cached = _own_account_cache["account_id"]
    if cached and (now - _own_account_cache["checked_at"]) < _OWN_ACCOUNT_CACHE_TTL_SECONDS:
        return cached
    try:
        identity = boto3.client("sts").get_caller_identity()
        account_id = identity["Account"]
        _own_account_cache["account_id"] = account_id
        _own_account_cache["checked_at"] = now
        return account_id
    except ClientError:
        return None


def get_self_federation_session():
    """
    Mints a session-scoped, read-only credential set via STS
    GetFederationToken using the server's OWN identity — no
    cross-account AssumeRole, no pre-created target role. Only valid
    when the target AWS account IS the server's own account (see
    get_own_account_id()); for any other account this still requires a
    role_arn, which is a hard AWS security boundary, not a gap in this
    function.
    """
    sts = boto3.client("sts")
    response = sts.get_federation_token(
        Name="monitoring-hub-self",
        PolicyArns=[{"arn": "arn:aws:iam::aws:policy/ReadOnlyAccess"}],
        DurationSeconds=3600,
    )
    credentials = response["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )


def assume_role(role_arn: str, external_id: str | None = None):'''

PATCHES.append(("app/aws/sts.py", [(STS_OLD, STS_NEW)]))

# ─────────────────────────────────────────────────────────────────────────
# 2. app/aws/federation.py — self-federation branch + NoConsoleCredentialsError
# ─────────────────────────────────────────────────────────────────────────
FED_IMPORT_OLD = '''import json
import logging
import urllib.parse

import requests

from app.aws.sts import assume_role

logger = logging.getLogger(__name__)

FEDERATION_ENDPOINT = "https://signin.aws.amazon.com/federation"
ISSUER = "monitoring-hub"
SESSION_DURATION_SECONDS = 3600  # must be <= the assumed role's max session duration'''

FED_IMPORT_NEW = '''import json
import logging
import urllib.parse

import requests

from app.aws.sts import assume_role, get_own_account_id, get_self_federation_session

logger = logging.getLogger(__name__)

FEDERATION_ENDPOINT = "https://signin.aws.amazon.com/federation"
ISSUER = "monitoring-hub"
SESSION_DURATION_SECONDS = 3600  # must be <= the assumed role's max session duration


class NoConsoleCredentialsError(ValueError):
    """
    Raised when we have no way to obtain console credentials for the
    target account: no role_arn is configured AND the target account is
    not the server's own account. Callers should surface this as a 400
    (config problem), distinct from other exceptions in this module
    which mean the credential path was found but the AWS call itself
    failed (500/502).
    """
    pass'''

PATCHES.append(("app/aws/federation.py", [(FED_IMPORT_OLD, FED_IMPORT_NEW)]))

FED_BUILD_OLD = '''def build_federated_console_url(role_arn: str, external_id: str | None,
                                 destination: str) -> str:
    """
    Assumes `role_arn` (the alert's own AWS account), exchanges the temporary
    credentials for a sign-in token, and returns a login URL that drops the
    user directly onto `destination` inside the CORRECT account — no
    dependence on whatever account the browser is currently signed into.
    """
    session = assume_role(role_arn, external_id)
    creds = session.get_credentials().get_frozen_credentials()'''

FED_BUILD_NEW = '''def build_federated_console_url(role_arn: str | None, external_id: str | None,
                                 destination: str,
                                 target_account_id: str | None = None) -> str:
    """
    Exchanges credentials for a sign-in token and returns a login URL that
    drops the user directly onto `destination` inside the CORRECT account —
    no dependence on whatever account the browser is currently signed into.

    Credential path is chosen automatically:
      - role_arn set                                   -> AssumeRole (cross-account)
      - role_arn empty, target_account_id == own account -> GetFederationToken
                                                             (self-federation,
                                                             zero config)
      - role_arn empty, target_account_id != own account -> NoConsoleCredentialsError
    """
    role_arn = (role_arn or "").strip()

    if role_arn:
        session = assume_role(role_arn, external_id)
    else:
        own_account_id = get_own_account_id()
        if target_account_id and own_account_id and str(target_account_id) == str(own_account_id):
            logger.info(
                "Console link for account %s uses self-federation (server's own account, no role_arn needed)",
                target_account_id,
            )
            session = get_self_federation_session()
        else:
            raise NoConsoleCredentialsError(
                "No AWS role configured for this account, and it is not "
                "the server's own AWS account, so no automatic credential "
                "path is available. Set an IAM Role ARN for this account "
                "in Settings to enable console access."
            )

    creds = session.get_credentials().get_frozen_credentials()'''

PATCHES.append(("app/aws/federation.py", [(FED_BUILD_OLD, FED_BUILD_NEW)]))

# ─────────────────────────────────────────────────────────────────────────
# 3. app/api/alerts.py — drop hard gate, pass target_account_id, catch new error
# ─────────────────────────────────────────────────────────────────────────
ALERTS_OLD = '''    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    if not row.get("role_arn"):
        raise HTTPException(status_code=400, detail="No AWS role configured for this account")

    destination = resource_console_destination(
        row.get("resource_type"), row["resource"], row["region"],
        resource_name=row.get("resource_name"),
    )

    try:
        url = build_federated_console_url(row["role_arn"], row["external_id"], destination)
    except Exception:
        logger.exception("Failed to build federated console URL for alert %s", alert_id)
        raise HTTPException(status_code=502, detail="Could not generate AWS console link")'''

ALERTS_NEW = '''    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")

    destination = resource_console_destination(
        row.get("resource_type"), row["resource"], row["region"],
        resource_name=row.get("resource_name"),
    )

    try:
        url = build_federated_console_url(
            row.get("role_arn"), row.get("external_id"), destination,
            target_account_id=row.get("aws_account_id"),
        )
    except NoConsoleCredentialsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Failed to build federated console URL for alert %s", alert_id)
        raise HTTPException(status_code=502, detail="Could not generate AWS console link")'''

PATCHES.append(("app/api/alerts.py", [(ALERTS_OLD, ALERTS_NEW)]))

ALERTS_IMPORT_OLD = '''from app.aws.federation import build_federated_console_url, resource_console_destination'''
ALERTS_IMPORT_NEW = '''from app.aws.federation import (
    build_federated_console_url,
    resource_console_destination,
    NoConsoleCredentialsError,
)'''
PATCHES.append(("app/api/alerts.py", [(ALERTS_IMPORT_OLD, ALERTS_IMPORT_NEW)]))

# ─────────────────────────────────────────────────────────────────────────
# 4. app/api/admin/accounts.py — same gate removal for the generic endpoint
# ─────────────────────────────────────────────────────────────────────────
ACCOUNTS_OLD = '''    if not account:
        raise HTTPException(status_code=404, detail="Account not found or inactive")
    if (account.get("provider") or "aws") == "aws" and not account.get("role_arn"):
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
        raise HTTPException(status_code=502, detail=f"Could not generate console link: {e}")'''

ACCOUNTS_NEW = '''    if not account:
        raise HTTPException(status_code=404, detail="Account not found or inactive")

    region = region or account.get("default_region")

    try:
        from app.providers.registry import get_provider
        from app.aws.federation import NoConsoleCredentialsError
        provider = get_provider(account.get("provider") or "aws")
        url = provider.get_console_url(
            account, resource_id, region,
            service=service, resource_name=resource_name,
            ecs_service_name=ecs_service_name,
        )
    except NoConsoleCredentialsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not generate console link: {e}")'''

PATCHES.append(("app/api/admin/accounts.py", [(ACCOUNTS_OLD, ACCOUNTS_NEW)]))

# ─────────────────────────────────────────────────────────────────────────
# 5. app/providers/aws/provider.py — pass target_account_id through
# ─────────────────────────────────────────────────────────────────────────
PROVIDER_OLD = '''        return build_federated_console_url(
            account.get("role_arn"), account.get("external_id"), destination
        )'''

PROVIDER_NEW = '''        return build_federated_console_url(
            account.get("role_arn"), account.get("external_id"), destination,
            target_account_id=account.get("account_id"),
        )'''

PATCHES.append(("app/providers/aws/provider.py", [(PROVIDER_OLD, PROVIDER_NEW)]))


def preflight(dry_run: bool):
    print("=== Pre-flight: verifying anchors match exactly ===")
    problems = []
    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        for old, _new in replacements:
            count = text.count(old)
            if count == 0:
                # Could already be applied — check if the corresponding
                # new text is already present (idempotency).
                problems.append(f"{rel_path}: anchor not found (0 matches) — see below")
            elif count > 1:
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1")
            else:
                print(f"  OK  {rel_path}: anchor matched exactly once")

    if problems:
        print("\n".join(problems))
        # Distinguish "already applied" from "genuinely broken"
        already_applied = all(
            (REPO_ROOT / rel).read_text(encoding="utf-8").count(new) >= 1
            for rel, repls in PATCHES
            for _old, new in repls
            for rel in [rel]
        )
        if already_applied:
            print("\nAll target text already present — patch appears to be already applied. Nothing to do.")
            sys.exit(0)
        raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")
    print("Pre-flight OK.\n")


def apply_patches(dry_run: bool):
    changed_files = []
    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        original_text = text
        for old, new in replacements:
            if old not in text:
                raise PatchError(f"{rel_path}: expected anchor vanished mid-patch — aborting")
            text = text.replace(old, new, 1)

        if text == original_text:
            continue

        if dry_run:
            print(f"[DRY RUN] would patch: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(text, encoding="utf-8")
            print(f"PATCHED: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)
    return changed_files


def validate_python_syntax(changed_files):
    print("\n=== Validating Python syntax (py_compile) ===")
    for f in changed_files:
        if f.suffix != ".py":
            continue
        try:
            py_compile.compile(str(f), doraise=True)
            print(f"  OK  {f.relative_to(REPO_ROOT)}")
        except py_compile.PyCompileError as e:
            raise PatchError(f"SYNTAX ERROR after patching {f}: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        preflight(args.dry_run)
        changed = apply_patches(args.dry_run)
        if not args.dry_run:
            validate_python_syntax(changed)
            print(f"\n=== Done. {len(changed)} file(s) patched. ===")
            print("Restart uvicorn (full restart, not --reload) for this to take effect.")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
