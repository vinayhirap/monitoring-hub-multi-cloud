#!/usr/bin/env python3
"""
apply_fresh_schema_migrations_fk_type_fix.py

Bug found 2026-08-26: apply_fresh_schema_migrations.py's provider_credentials
CREATE TABLE declares `aws_account_id INT NOT NULL PRIMARY KEY`, but
aws_accounts.id is BIGINT (confirmed: DESCRIBE aws_accounts shows
"id bigint NO PRI ... auto_increment"). MySQL requires FK columns to be
the same underlying type as what they reference, so the FK constraint
fk_provider_credentials_account fails with:

  1005/3780 (HY000): Referencing column 'aws_account_id' and referenced
  column 'id' in foreign key constraint '...' are incompatible.

Since provider_credentials is item #3 in plan_statements()'s list and the
whole script applies statements in a single loop that aborts on first
failure, this single wrong column type blocked every later statement in
the same run too (resources.resource_id widen, metrics dedup key, etc.)
on any DB where provider_credentials didn't already exist -- exactly what
happened on CloudOps_Main.

This patches the CREATE TABLE statement in-place: INT -> BIGINT. Nothing
else about the table changes. Existing installs where provider_credentials
was somehow already created with INT (unlikely, since the FK would have
failed there too) are unaffected -- this only changes the CREATE statement
used for DBs where the table doesn't exist yet.

Same conventions as this project's other patch scripts: dry-run, backup
(.bak) of the file before editing, py_compile validation after, auto-revert
on syntax error, exact-text anchor matching (not line numbers).

Usage:
    python apply_fresh_schema_migrations_fk_type_fix.py --dry-run
    python apply_fresh_schema_migrations_fk_type_fix.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "apply_fresh_schema_migrations.py"

OLD = '''            CREATE TABLE provider_credentials (
              aws_account_id     INT NOT NULL PRIMARY KEY,'''

NEW = '''            CREATE TABLE provider_credentials (
              -- aws_account_id must match aws_accounts.id's type (BIGINT) --
              -- fix: 2026-08-26, was INT, which fails FK creation with
              -- "Referencing column ... and referenced column ... are
              -- incompatible" (error 3780) and blocked every later
              -- statement in this script's single-loop apply.
              aws_account_id     BIGINT NOT NULL PRIMARY KEY,'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    text = TARGET.read_text(encoding="utf-8")

    if "must match aws_accounts.id's type" in text:
        print("Already applied — provider_credentials.aws_account_id is already BIGINT. Nothing to do.")
        return

    if OLD not in text:
        print("ERROR: could not find the expected provider_credentials CREATE TABLE text.")
        print("The file has likely drifted from what this patch expects — inspect")
        print(f"{TARGET} manually rather than trusting this script's anchors.")
        sys.exit(1)

    if text.count(OLD) != 1:
        print("ERROR: anchor text is not unique in the file — refusing to guess which occurrence to patch.")
        sys.exit(1)

    new_text = text.replace(OLD, NEW, 1)

    print("Change to apply:")
    print(f"  provider_credentials.aws_account_id: INT -> BIGINT in {TARGET}")

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
    print("Re-run it to pick up the remaining pending migrations:")
    print("  sudo -u hcsadmin /opt/monitoring-hub/venv/bin/python3 apply_fresh_schema_migrations.py --dry-run")


if __name__ == "__main__":
    main()
