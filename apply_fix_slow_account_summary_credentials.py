#!/usr/bin/env python3
"""
apply_fix_slow_account_summary_credentials.py — fixes the real cause
of the ~1 minute wait when loading account data (Overview page, Add
User account list, etc.): the account-summary collector NEVER used
the account's configured cross-account role — it always fell back to
whatever AMBIENT AWS credentials the server process happened to have,
regardless of which account was actually being queried.

For any account that isn't the server's own AWS account (i.e. every
real cross-account setup, exactly like this app's onboarding flow is
built for), that means all 7 of the parallel per-account API calls
(EC2, EBS, RDS, Lambda, S3, ELB, ECS) had to exhaust botocore's full
credential-provider chain — environment vars, shared config, an IMDS
probe — before failing, on EVERY single request, since nothing was
ever cached at the credential level. That's exactly consistent with
the "Unable to locate credentials" warnings already showing up in this
server's logs, and with a slow, request-by-request wait rather than a
one-time cost.

THE FIX
  - app/aws/collector_direct.py: get_session() now accepts optional
    role_arn/external_id and, when given, resolves credentials via a
    real STS AssumeRole (app.aws.sts.assume_role — the same helper
    already used correctly elsewhere in this app, for discovery and
    console links). All 7 collectors used by get_account_summary()
    (EC2/EBS/RDS/Lambda/S3/ELB/ECS) now accept and forward role_arn/
    external_id through to get_session(), and their internal caches
    are keyed per-account (not just per-region) so two different
    accounts sharing a region can no longer collide in the cache —
    that would have been a real correctness bug once accounts are
    properly differentiated by credentials.
  - app/api/live_data.py: _get_db_accounts() now actually selects
    role_arn/external_id from aws_accounts (it never did before —
    there was no way to pass them through even if downstream code had
    wanted to), and process_account() passes them into
    get_account_summary().

Every OTHER caller of get_session()/the ~30 other collector functions
in this file (used by different features) is untouched — role_arn/
external_id default to None everywhere, preserving their exact
current behavior. This is scoped specifically to the account-summary
path that was reported as slow.

Usage:
    python apply_fix_slow_account_summary_credentials.py --dry-run
    python apply_fix_slow_account_summary_credentials.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-fix-slow-account-summary-credentials"

PATCHES = [
    (
        "app/aws/collector_direct.py",
        [
            (r'''def get_session(region=None):
    return boto3.Session(region_name=region)
''', r'''def get_session(region=None, role_arn=None, external_id=None):
    """
    Cross-account credentials for a SPECIFIC AWS account (role_arn
    given) come from a real STS AssumeRole (app.aws.sts.assume_role) —
    the same helper already used correctly by discovery and console
    federation. Without role_arn, falls back to ambient credentials
    (env vars / instance profile / shared config), unchanged from
    before — every OTHER caller of get_session in this file that
    doesn't pass role_arn is unaffected.

    Why this matters beyond correctness: before this, every collector
    below fell through to ambient-credential resolution regardless of
    which account was being queried. For a cross-account setup with
    no matching ambient credentials, that meant every AWS API call (7
    of them per account, in get_account_summary) had to exhaust
    botocore's full credential-provider chain — including an IMDS
    probe — before failing, on EVERY request. A single AssumeRole call
    resolves once and is reused for every client built from the
    returned session.
    """
    if role_arn:
        from app.aws.sts import assume_role
        base_session = assume_role(role_arn, external_id, session_name="mh-account-summary")
        creds = base_session.get_credentials().get_frozen_credentials()
        return boto3.Session(
            aws_access_key_id=creds.access_key,
            aws_secret_access_key=creds.secret_key,
            aws_session_token=creds.token,
            region_name=region,
        )
    return boto3.Session(region_name=region)
'''),
            (r'''def collect_ec2_instances(region=None) -> list:
    return _cached(f"ec2_{region}", lambda: _ec2_raw(region))

def _ec2_raw(region) -> list:
    try:
        ec2 = get_session(region).client("ec2")
''', r'''def collect_ec2_instances(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"ec2_{region}_{role_arn or 'self'}", lambda: _ec2_raw(region, role_arn, external_id))

def _ec2_raw(region, role_arn=None, external_id=None) -> list:
    try:
        ec2 = get_session(region, role_arn, external_id).client("ec2")
'''),
            (r'''def collect_ebs_volumes(region=None) -> list:
    return _cached(f"ebs_{region}", lambda: _ebs_raw(region))

def _ebs_raw(region) -> list:
    try:
        ec2  = get_session(region).client("ec2")
''', r'''def collect_ebs_volumes(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"ebs_{region}_{role_arn or 'self'}", lambda: _ebs_raw(region, role_arn, external_id))

def _ebs_raw(region, role_arn=None, external_id=None) -> list:
    try:
        ec2  = get_session(region, role_arn, external_id).client("ec2")
'''),
            (r'''def collect_rds_instances(region=None) -> list:
    return _cached(f"rds_{region}", lambda: _rds_raw(region))

def _rds_raw(region) -> list:
    try:
        rds = get_session(region).client("rds")
''', r'''def collect_rds_instances(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"rds_{region}_{role_arn or 'self'}", lambda: _rds_raw(region, role_arn, external_id))

def _rds_raw(region, role_arn=None, external_id=None) -> list:
    try:
        rds = get_session(region, role_arn, external_id).client("rds")
'''),
            (r'''def collect_s3_buckets(region=None) -> list:
    return _cached("s3_global", lambda: _s3_raw())

def _s3_raw() -> list:
    try:
        s3  = boto3.client("s3")
''', r'''def collect_s3_buckets(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"s3_global_{role_arn or 'self'}", lambda: _s3_raw(role_arn, external_id))

def _s3_raw(role_arn=None, external_id=None) -> list:
    try:
        s3  = get_session(None, role_arn, external_id).client("s3")
'''),
            (r'''def collect_elb(region=None) -> list:
    return _cached(f"elb_{region}", lambda: _elb_raw(region))

def _elb_raw(region) -> list:
    try:
        elb = get_session(region).client("elbv2")
''', r'''def collect_elb(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"elb_{region}_{role_arn or 'self'}", lambda: _elb_raw(region, role_arn, external_id))

def _elb_raw(region, role_arn=None, external_id=None) -> list:
    try:
        elb = get_session(region, role_arn, external_id).client("elbv2")
'''),
            (r'''def collect_ecs_clusters(region=None) -> list:
    return _cached(f"ecs_{region}", lambda: _ecs_raw(region))

def _ecs_raw(region) -> list:
    try:
        ecs = get_session(region).client("ecs")
        cw  = get_session(region).client("cloudwatch")
''', r'''def collect_ecs_clusters(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"ecs_{region}_{role_arn or 'self'}", lambda: _ecs_raw(region, role_arn, external_id))

def _ecs_raw(region, role_arn=None, external_id=None) -> list:
    try:
        session = get_session(region, role_arn, external_id)
        ecs = session.client("ecs")
        cw  = session.client("cloudwatch")
'''),
            (r'''def collect_lambda_functions(region=None) -> list:
    return _cached(f"lambda_{region}", lambda: _lambda_raw(region))

def _lambda_raw(region) -> list:
    try:
        lmb = get_session(region).client("lambda")
''', r'''def collect_lambda_functions(region=None, role_arn=None, external_id=None) -> list:
    return _cached(f"lambda_{region}_{role_arn or 'self'}", lambda: _lambda_raw(region, role_arn, external_id))

def _lambda_raw(region, role_arn=None, external_id=None) -> list:
    try:
        lmb = get_session(region, role_arn, external_id).client("lambda")
'''),
            (r'''def get_account_summary(region=None) -> dict:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    collectors = {
        "ec2": lambda: collect_ec2_instances(region),
        "ebs": lambda: collect_ebs_volumes(region),
        "rds": lambda: collect_rds_instances(region),
        "lmb": lambda: collect_lambda_functions(region),
        "s3":  lambda: collect_s3_buckets(region),
        "elb": lambda: collect_elb(region),
        "ecs": lambda: collect_ecs_clusters(region),
    }
''', r'''def get_account_summary(region=None, role_arn=None, external_id=None) -> dict:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    collectors = {
        "ec2": lambda: collect_ec2_instances(region, role_arn, external_id),
        "ebs": lambda: collect_ebs_volumes(region, role_arn, external_id),
        "rds": lambda: collect_rds_instances(region, role_arn, external_id),
        "lmb": lambda: collect_lambda_functions(region, role_arn, external_id),
        "s3":  lambda: collect_s3_buckets(region, role_arn, external_id),
        "elb": lambda: collect_elb(region, role_arn, external_id),
        "ecs": lambda: collect_ecs_clusters(region, role_arn, external_id),
    }
'''),
        ],
    ),
    (
        "app/api/live_data.py",
        [
            (r'''def _get_db_accounts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
            SELECT id, account_name, account_id,
                default_region, status,
                created_at, last_synced_at
            FROM aws_accounts
            WHERE status = 'active'
            ORDER BY created_at DESC
        """)
''', r'''def _get_db_accounts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
            SELECT id, account_name, account_id,
                default_region, status, role_arn, external_id,
                created_at, last_synced_at
            FROM aws_accounts
            WHERE status = 'active'
            ORDER BY created_at DESC
        """)
'''),
            (r'''    def process_account(acc):
        region  = acc.get("default_region")
        summary = get_account_summary(region)
''', r'''    def process_account(acc):
        region  = acc.get("default_region")
        summary = get_account_summary(region, role_arn=acc.get("role_arn"), external_id=acc.get("external_id"))
'''),
        ],
    ),
]


class PatchError(Exception):
    pass


def preflight():
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
                problems.append(f"{rel_path}: anchor not found (0 matches) — {old[:70]!r}")
            elif count > 1:
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1")
            else:
                print(f"  OK  {rel_path}: anchor matched exactly once")

    if problems:
        print("\n".join(problems))

        def _already(rel, new_text):
            p = REPO_ROOT / rel
            return p.exists() and new_text in p.read_text(encoding="utf-8")

        already_applied = all(_already(rel, new) for rel, repls in PATCHES for _old, new in repls)
        if already_applied:
            print("\nAll target text already present — patch appears to be already applied. Nothing to do.")
            sys.exit(0)
        raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")
    print("Pre-flight OK.\n")


def apply_all(dry_run: bool):
    changed_files = []
    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        original_text = text
        for old, new in replacements:
            if new in text:
                continue
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        preflight()
        changed = apply_all(args.dry_run)
        if not args.dry_run:
            for f in changed:
                py_compile.compile(str(f), doraise=True)
                print(f"  syntax OK: {f.relative_to(REPO_ROOT)}")
            print(f"\n=== Done. {len(changed)} file(s) touched. ===")
            print("\nsudo systemctl restart monitoring-hub")
            print("\nVerify: check that AWS accounts actually have role_arn set:")
            print("  mysql -umonitor -proot123 monitoring_hub -e \"")
            print("    SELECT id, account_name, role_arn, external_id FROM aws_accounts WHERE status='active';\"")
            print("If role_arn is empty/NULL for an account, this fix can't help it —")
            print("that account was never configured for cross-account access at all;")
            print("it needs a role_arn set via account onboarding/settings first.")
            print("\nThen time a fresh account load (clear the frontend cache too —")
            print("localStorage, or a hard refresh) and compare before/after.")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
