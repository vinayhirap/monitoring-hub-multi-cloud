#!/usr/bin/env python3
"""
scripts/check_migration_numbers.py

Fails (non-zero exit) if db/migrations/ contains two files with the
same leading number prefix -- e.g. 050_auth_session_hardening.sql and
050_fix_rbac_global_scope_bootstrap.sql both claiming "050".

WHY THIS EXISTS
Migration numbers are assigned by hand, by whoever is working in a
given moment -- often several people/sessions working in parallel off
their own local clone, each picking "the next free number" from
whatever HEAD looked like when they started. Two clones started
minutes apart can legitimately both see 049 as the last one taken and
both reach for 050. migrate.py already refuses to run --all-pending
when this happens (good -- it fails at apply time, not silently), but
by then the collision has already been merged to main and possibly
deployed to one environment but not another. This script is meant to
catch it earlier, at push/PR time, so it's a five-second fix on a
branch instead of a `migrate.py apply <file>`-by-filename workaround
on a production box during a deploy.

Two files sharing a numeric root are NOT a collision if one has an
alpha suffix directly on the number (002_foo.sql vs 002b_bar.sql,
which this repo already uses deliberately for same-day follow-on
migrations) -- only an EXACT duplicate prefix is flagged.

A "<n>_..._rollback.sql" file is also NOT a collision with its
forward migration "<n>_....sql" -- migrate.py itself already treats
"*_rollback.sql" as a distinct category (see its own
--all-except-rollbacks flag), so a rollback companion sharing its
forward migration's number is the existing, intentional convention in
this repo, not something to flag.

Usage:
    python3 scripts/check_migration_numbers.py
    python3 scripts/check_migration_numbers.py --dir db/migrations

Exit 0  -- no duplicate prefixes.
Exit 1  -- duplicate(s) found; details printed to stderr.
"""
import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

PREFIX_RE = re.compile(r"^([0-9]+[a-zA-Z]*)_")


def find_duplicates(migrations_dir: Path) -> dict:
    by_prefix = defaultdict(list)
    for path in sorted(migrations_dir.glob("*.sql")):
        m = PREFIX_RE.match(path.name)
        if not m:
            # Files with no leading number (e.g. add_monitoring_tier.sql,
            # already in this repo) are outside the numbering scheme
            # entirely and can't collide with it -- not this script's
            # concern.
            continue
        # A forward migration and its own "*_rollback.sql" companion
        # are expected to share a number (migrate.py already treats
        # rollback files as a separate category) -- track them apart
        # so they never collide with each other, only with genuinely
        # unrelated files that happen to claim the same number.
        is_rollback = path.name.endswith("_rollback.sql")
        key = (m.group(1), is_rollback)
        by_prefix[key].append(path.name)
    return {prefix: names for prefix, names in by_prefix.items() if len(names) > 1}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir", default="db/migrations",
        help="Path to the migrations directory (default: db/migrations)",
    )
    args = parser.parse_args()

    migrations_dir = Path(args.dir)
    if not migrations_dir.exists():
        print(f"ERROR: migrations directory not found: {migrations_dir}", file=sys.stderr)
        return 1

    dupes = find_duplicates(migrations_dir)
    if not dupes:
        print(f"OK: no duplicate migration numbers in {migrations_dir}")
        return 0

    print("ERROR: duplicate migration numbers found:", file=sys.stderr)
    for (number, is_rollback), names in sorted(dupes.items()):
        label = f"{number} (rollback)" if is_rollback else number
        print(f"  {label}:", file=sys.stderr)
        for name in names:
            print(f"    - {name}", file=sys.stderr)
    print(
        "\nRename all but one of each group to the next free number "
        "(run scripts/next_migration_number.sh against an up-to-date "
        "origin/main first) before merging.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
