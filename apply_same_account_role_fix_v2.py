#!/usr/bin/env python3
"""
apply_same_account_role_fix_v2.py

Follow-up to apply_same_account_role_fix.py. That patch's same-account
short-circuit called get_self_federation_session(), which uses STS
GetFederationToken -- and AWS rejects GetFederationToken when called with
SESSION credentials (confirmed live: "AccessDenied ... Cannot call
GetFederationToken with session credentials"). An EC2 instance profile
(what CloudOps_Main actually runs as) always hands out temporary session
credentials via IMDS, so that call can never succeed here regardless of
which account is targeted -- this isn't specific to AuroGov Mumbai, it
would fail identically for any account, including a genuinely fresh
same-account setup.

The actual fix needed is simpler than federation: when the target account
IS the server's own account, there's no STS call needed at all -- just use
the instance's own default credential chain directly, exactly like
app/collector/discovery/runner.py and app/collector/metrics/runner.py
already do for accounts with no role_arn set (both fall back to plain
boto3.Session()). This patch replaces the get_self_federation_session()
call added by the previous script with boto3.Session().

Safe to run whether or not apply_same_account_role_fix.py has been run yet
-- if the same-account short-circuit isn't present at all, this script
says so and exits without changing anything (run the v1 script first).

Same conventions as this project's other patch scripts: dry-run, backup
(.bak2) of the file before editing, py_compile validation after,
auto-revert on syntax error, exact-text anchor matching (not line numbers).

Usage:
    python apply_same_account_role_fix_v2.py --dry-run
    python apply_same_account_role_fix_v2.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "app" / "aws" / "sts.py"

OLD = '''    match = re.match(r"arn:aws:iam::(\\d+):role/", role_arn or "")
    if match:
        target_account_id = match.group(1)
        own_account_id = get_own_account_id()
        if own_account_id and target_account_id == own_account_id:
            return get_self_federation_session(session_name=session_name, policy=policy)

    sts = boto3.client("sts")'''

NEW = '''    match = re.match(r"arn:aws:iam::(\\d+):role/", role_arn or "")
    if match:
        target_account_id = match.group(1)
        own_account_id = get_own_account_id()
        if own_account_id and target_account_id == own_account_id:
            # NOTE (2026-08-26): get_self_federation_session() was tried
            # here first but AWS rejects STS GetFederationToken when called
            # with SESSION credentials -- confirmed live: "Cannot call
            # GetFederationToken with session credentials". An EC2 instance
            # profile (what this server runs as) always provides temporary
            # session credentials via IMDS, so that call can never succeed
            # regardless of target account. No STS call is actually needed
            # for the same-account case -- the instance's own default
            # credential chain already has whatever permissions its
            # instance profile grants, same as the existing no-role_arn
            # fallback in discovery/runner.py and metrics/runner.py.
            return boto3.Session()

    sts = boto3.client("sts")'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    text = TARGET.read_text(encoding="utf-8")

    if "No STS call is actually needed" in text:
        print("Already applied — assume_role() already uses boto3.Session() for the same-account case. Nothing to do.")
        return

    if OLD not in text:
        if "SAME-ACCOUNT SHORT-CIRCUIT" not in text:
            print("ERROR: the same-account short-circuit isn't present at all in this file.")
            print("Run apply_same_account_role_fix.py first, then re-run this script.")
        else:
            print("ERROR: could not find the expected get_self_federation_session() call text.")
            print("The file has likely drifted from what this patch expects — inspect")
            print(f"{TARGET} manually rather than trusting this script's anchors.")
        sys.exit(1)

    if text.count(OLD) != 1:
        print("ERROR: anchor text is not unique in the file — refusing to guess which occurrence to patch.")
        sys.exit(1)

    new_text = text.replace(OLD, NEW, 1)

    print("Change to apply:")
    print(f"  replace get_self_federation_session() with boto3.Session() in the same-account")
    print(f"  short-circuit inside assume_role(), in {TARGET}")

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        return

    backup_path = TARGET.with_suffix(TARGET.suffix + ".bak2")
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
    print("\nThen watch for these (should disappear within one collector cycle):")
    print("  journalctl -u monitoring-hub -f | grep -i -E 'assumerole|federationtoken|describe_polling'")


if __name__ == "__main__":
    main()
