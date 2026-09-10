#!/usr/bin/env python3
"""
apply_fix_self_assume_check.py

verify_deployment.py's check_3_self_assume_role() predates the same-account
fix in app/aws/sts.py (commit 22ff060, 2026-08-27). That fix made
assume_role() detect when role_arn's account matches the instance's own
account and short-circuit to boto3.Session() instead of a doomed real
AssumeRole call -- so a self-referential role_arn is no longer a bug, it's
the expected shape for a same-account monitored entry. check_3 was never
updated after that fix landed, so it still reports every such row as an
unconditional FAIL with a docstring claiming it "will always fail
AccessDenied", which stopped being true as of 22ff060. Confirmed live on
aws_accounts.id=7 (AuroGov Mumbai): 3947 metric_history rows, last_seen
minutes old, zero AccessDenied/AssumeRole hits in the service journal --
the check's FAIL was a false positive.

Separately, check_3's query has no status filter, so an inactive duplicate
row (aws_accounts.id=8: same account_id/role_arn as id=7, status='inactive',
zero resources, zero metrics -- never actually collecting) gets flagged
identically to a live, working row. Two unrelated rows, one misleading
FAIL for both, for two different reasons.

This patch:
  1. Filters the query to `status='active'` rows only -- an inactive
     account's role_arn is nobody's problem regardless of what it points to.
  2. Checks whether app/aws/sts.py's same-account short-circuit is present
     (searches for the same anchor string
     apply_same_account_role_fix_v2.py checks for). If present, a
     self-referential role_arn on an active row is reported as OK, with a
     note explaining why (handled automatically via boto3.Session()). If
     the short-circuit is NOT present in sts.py, the original FAIL
     behavior is preserved unchanged -- this patch only relaxes the check
     when the underlying code fix it depends on is actually there.

Same conventions as this project's other patch scripts: dry-run, backup
(.bak) of the file before editing, py_compile validation after, auto-revert
on syntax error, exact-text anchor matching (not line numbers).

Usage:
    python apply_fix_self_assume_check.py --dry-run
    python apply_fix_self_assume_check.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "verify_deployment.py"
STS_FILE = Path(__file__).resolve().parent / "app" / "aws" / "sts.py"
STS_FIX_ANCHOR = "No STS call is actually needed"  # same anchor apply_same_account_role_fix_v2.py checks

OLD = '''        cursor.execute("SELECT id, role_arn FROM aws_accounts WHERE role_arn IS NOT NULL")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
    except mysql.connector.Error as e:
        line(FAIL, f"Cannot query aws_accounts.role_arn: {e}")
        return

    bad = [(acc_id, arn) for acc_id, arn in rows if arn and arn.rstrip("/").split("/")[-1] == own_role]
    if bad:
        for acc_id, arn in bad:
            line(FAIL, f"aws_accounts.id={acc_id} has role_arn={arn}, which is the SAME role "
                        f"already attached to this instance ({own_role}). A role can't assume "
                        f"itself this way — this account's AssumeRole calls will always fail "
                        f"AccessDenied. Point role_arn at the actual cross-account role in the "
                        f"target AWS account instead.")
    else:
        line(OK, f"No aws_accounts rows reference this instance's own role ({own_role}).")'''

NEW = '''        cursor.execute("SELECT id, role_arn FROM aws_accounts WHERE role_arn IS NOT NULL AND status='active'")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
    except mysql.connector.Error as e:
        line(FAIL, f"Cannot query aws_accounts.role_arn: {e}")
        return

    # SAME-ACCOUNT AWARENESS (fix: 2026-09-10): app/aws/sts.py's assume_role()
    # has, since commit 22ff060 (2026-08-27), short-circuited same-account
    # role_arns to boto3.Session() instead of a real (and doomed) AssumeRole
    # call. A self-referential role_arn is therefore only a real problem if
    # that fix isn't present in the code this instance is actually running.
    same_account_fix_present = False
    try:
        same_account_fix_present = STS_FIX_ANCHOR in STS_FILE.read_text(encoding="utf-8")
    except OSError:
        pass  # can't read sts.py — fall back to treating it as unfixed (safer default)

    bad = [(acc_id, arn) for acc_id, arn in rows if arn and arn.rstrip("/").split("/")[-1] == own_role]
    if bad:
        for acc_id, arn in bad:
            if same_account_fix_present:
                line(OK, f"aws_accounts.id={acc_id} has role_arn={arn}, the same role already "
                          f"attached to this instance ({own_role}) — this is a same-account entry, "
                          f"not a bug. app/aws/sts.py's assume_role() short-circuits this to "
                          f"boto3.Session() automatically (no real AssumeRole call, no ARN needed).")
            else:
                line(FAIL, f"aws_accounts.id={acc_id} has role_arn={arn}, which is the SAME role "
                            f"already attached to this instance ({own_role}). A role can't assume "
                            f"itself this way — this account's AssumeRole calls will always fail "
                            f"AccessDenied. Point role_arn at the actual cross-account role in the "
                            f"target AWS account instead, or apply the same-account fix "
                            f"(see apply_same_account_role_fix_v2.py).")
    else:
        line(OK, f"No active aws_accounts rows reference this instance's own role ({own_role}).")'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    text = TARGET.read_text(encoding="utf-8")

    if "SAME-ACCOUNT AWARENESS" in text:
        print("Already applied — check_3_self_assume_role() already accounts for the sts.py fix. Nothing to do.")
        return

    if OLD not in text:
        print("ERROR: could not find the expected check_3_self_assume_role() text.")
        print("The file has likely drifted from what this patch expects — inspect")
        print(f"{TARGET} manually rather than trusting this script's anchors.")
        sys.exit(1)

    if text.count(OLD) != 1:
        print("ERROR: anchor text is not unique in the file — refusing to guess which occurrence to patch.")
        sys.exit(1)

    new_text = text.replace(OLD, NEW, 1)

    print("Change to apply:")
    print(f"  1. filter check_3's query to status='active' rows only")
    print(f"  2. downgrade self-referential role_arn from FAIL to OK when app/aws/sts.py's")
    print(f"     same-account short-circuit is present (checked via {STS_FIX_ANCHOR!r} in {STS_FILE})")
    print(f"  in {TARGET}")

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
    print("Re-run to confirm: python3 verify_deployment.py")


if __name__ == "__main__":
    main()
