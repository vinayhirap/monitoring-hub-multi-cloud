#!/usr/bin/env python3
"""
apply_same_account_role_fix.py

Root cause (found 2026-08-26): aws_accounts row id=7 "AuroGov Mumbai" has
account_id = 924922671984 and role_arn pointing at
arn:aws:iam::924922671984:role/Monitoring_Role — the SAME account and the
SAME role already attached to the CloudOps_Main app server's own instance
profile (confirmed via journalctl: the AccessDenied error is literally
"assumed-role/Monitoring_Role/... not authorized to assume ... role/Monitoring_Role").
Assuming your own role via sts:AssumeRole always fails unless its trust
policy explicitly allows self-assumption, which nothing here sets up.

This isn't a data error to "correct" in the DB — a monitored AWS account
legitimately CAN be the same account the app server lives in (that's
exactly AuroGov Mumbai's case). The bug is that app/aws/sts.py's
assume_role() never checks for this and always tries a real AssumeRole
call, which is what silently breaks EC2/RDS/S3/Lambda/ECS/EBS discovery
and metric collection for this account while ALB — which for other
reasons in this codebase goes through app/aws/collector_direct.py's
get_session(), a completely separate path with no AssumeRole at all —
keeps working fine. That's why only ALB metrics were unaffected.

The fix goes in exactly one place: assume_role() in app/aws/sts.py. Every
call site that matters (app/providers/aws/provider.py,
app/collector/metrics/runner.py, app/collector/discovery/runner.py) calls
assume_role(role_arn, external_id) directly whenever role_arn is set, so
fixing it there fixes all three without touching any of them.

The fix uses tooling that ALREADY EXISTS in this file and is currently
unused for this purpose: get_own_account_id() (STS GetCallerIdentity
against the server's own credentials, cached 5 min) and
get_self_federation_session() (STS GetFederationToken, read-only scoped,
no cross-account trust policy required). assume_role() now extracts the
target account id from role_arn, compares it to the server's own account
id, and if they match, returns a self-federation session instead of
attempting a real (and doomed) AssumeRole call. Every other account
(genuinely cross-account) is completely unaffected — this only changes
behavior when target_account_id == own_account_id.

Same conventions as this project's other patch scripts: dry-run, backup
(.bak) of the file before editing, py_compile validation after, auto-revert
on syntax error, exact-text anchor matching (not line numbers).

Usage:
    python apply_same_account_role_fix.py --dry-run
    python apply_same_account_role_fix.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "app" / "aws" / "sts.py"

OLD = '''def assume_role(role_arn: str, external_id: str | None = None,
                 session_name: str | None = None, policy: str | None = None):
    """
    `session_name` attributes the resulting CloudTrail activity to
    the actual monitoring-hub user who requested it (see
    _sanitize_session_name), instead of the previous shared
    "monitoring-hub-session" name used for every operator and every
    scheduled job alike. `policy` is an optional IAM session policy
    JSON string (see federation.build_scoped_session_policy) — AWS
    takes the INTERSECTION of the role's own permissions and this
    policy, so it can only restrict, never expand, access.
    """
    sts = boto3.client("sts")'''

NEW = '''def assume_role(role_arn: str, external_id: str | None = None,
                 session_name: str | None = None, policy: str | None = None):
    """
    `session_name` attributes the resulting CloudTrail activity to
    the actual monitoring-hub user who requested it (see
    _sanitize_session_name), instead of the previous shared
    "monitoring-hub-session" name used for every operator and every
    scheduled job alike. `policy` is an optional IAM session policy
    JSON string (see federation.build_scoped_session_policy) — AWS
    takes the INTERSECTION of the role's own permissions and this
    policy, so it can only restrict, never expand, access.

    SAME-ACCOUNT SHORT-CIRCUIT (fix: 2026-08-26 AuroGov Mumbai incident):
    role_arn's account can legitimately be the SAME account the server's
    own credentials belong to (an account row doesn't have to be
    cross-account). Real sts:AssumeRole on your own role always fails
    AccessDenied unless its trust policy explicitly allows self-assumption
    -- which nothing here configures and shouldn't have to. Detect this via
    the role ARN's account id vs get_own_account_id() and use
    get_self_federation_session() instead: same read-only scoping, no
    cross-account trust policy required. Any genuinely cross-account
    role_arn is completely unaffected by this check.
    """
    match = re.match(r"arn:aws:iam::(\\d+):role/", role_arn or "")
    if match:
        target_account_id = match.group(1)
        own_account_id = get_own_account_id()
        if own_account_id and target_account_id == own_account_id:
            return get_self_federation_session(session_name=session_name, policy=policy)

    sts = boto3.client("sts")'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    text = TARGET.read_text(encoding="utf-8")

    if "SAME-ACCOUNT SHORT-CIRCUIT" in text:
        print("Already applied — assume_role() already has the same-account short-circuit. Nothing to do.")
        return

    if OLD not in text:
        print("ERROR: could not find the expected assume_role() function text.")
        print("The file has likely drifted from what this patch expects — inspect")
        print(f"{TARGET} manually rather than trusting this script's anchors.")
        sys.exit(1)

    if text.count(OLD) != 1:
        print("ERROR: anchor text is not unique in the file — refusing to guess which occurrence to patch.")
        sys.exit(1)

    new_text = text.replace(OLD, NEW, 1)

    print("Change to apply:")
    print(f"  add same-account short-circuit to assume_role() in {TARGET}")
    print("  (affects app/providers/aws/provider.py, app/collector/metrics/runner.py,")
    print("   and app/collector/discovery/runner.py automatically -- none of those files change)")

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        return

    backup_path = TARGET.with_suffix(TARGET.suffix + ".bak")
    shutil.copy2(TARGET, backup_path)
    print(f"Backup written to {backup_path}")

    TARGET.write_text(new_text, encoding="utf-8")

    try:
        py_compile.compile(str(TARGET), doraise=True)
    except py_compile.PyCompileError as e:
        print(f"\nERROR: patched file fails to compile:\n{e}")
        print("Reverting from backup...")
        shutil.copy2(backup_path, TARGET)
        sys.exit(1)

    print(f"\nOK: {TARGET} patched and compiles cleanly.")
    print("Restart the service to pick this up: sudo systemctl restart monitoring-hub")
    print("\nThen watch for these in the logs (should disappear within one collector cycle):")
    print("  journalctl -u monitoring-hub -f | grep -i -E 'assumerole|describe_polling'")


if __name__ == "__main__":
    main()
