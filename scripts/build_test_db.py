#!/usr/bin/env python3
"""
build_test_db.py -- build a scratch MySQL database for the real-database tests (MH_TEST_DB=1).

What it does, in order:
  1. Drops every table in the target database (it REFUSES unless the database name contains
     "test", or you pass --force).
  2. Loads tests/fixtures/alerts_test_schema.sql (the frozen baseline schema).
  3. Applies db/migrations/NNN_*.sql in numeric order for every NNN >= --from-migration
     (default 74), skipping *rollback* files. Migrations are idempotent, so the ones the
     fixture already covers are harmless no-ops.
  4. Prints a summary and checks a few columns/tables the tests rely on.

Some migrations touch tables that are deliberately NOT in the test fixture (users, audit_logs,
permissions, synthetic_checks, ...). MySQL reports error 1146 "table doesn't exist" (or 1824, a
foreign key to a missing table) for those;
the script runs the migration with `mysql --force` so its other statements still apply, lists
the affected tables, and exits 0. Any OTHER SQL error fails the build (exit 1). Use --strict to
treat those missing-table notes as failures too.

It only ever touches the database you name. It never reads .env and never connects to the
application database.

Connection settings come from the same variables the tests use:
  MH_TEST_DB_HOST (127.0.0.1)  MH_TEST_DB_PORT (3306)  MH_TEST_DB_USER (mh)
  MH_TEST_DB_PASSWORD (mh)     MH_TEST_DB_NAME (mh_test)
The user must already exist with all privileges on the database (see tests/README.md).

Usage:
  python3 scripts/build_test_db.py
  MH_TEST_DB_PORT=3307 python3 scripts/build_test_db.py --name mh_test
"""
import argparse
import glob
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(_ROOT, "tests", "fixtures", "alerts_test_schema.sql")
MIGRATIONS = os.path.join(_ROOT, "db", "migrations")

# Columns / tables the real-DB tests need; checked at the end so a stale build fails loudly here
# instead of as "Unknown column" deep inside a test.
REQUIRED = [
    ("alerts", "cycle_at"),
    ("alerts", "breach_value"),
    ("alert_pending", "cycle_at"),
    ("resources", "env_resolved"),
    ("resources", "tag_state"),
    ("aws_accounts", "default_env"),
    ("notification_channels", "id"),
]


def _env(name, default):
    return os.getenv(name, default)


def _mysql_cmd(args):
    cmd = ["mysql", f"-h{args.host}", f"-P{args.port}", f"-u{args.user}", f"-p{args.password}",
           "--protocol=TCP"]
    return cmd


def _run_sql(args, sql, database=True, force=False, quiet=True):
    cmd = _mysql_cmd(args)
    if force:
        cmd.append("--force")
    if database:
        cmd.append(args.name)
    if quiet:
        cmd.append("--batch")
    proc = subprocess.run(cmd, input=sql.encode("utf-8"), capture_output=True)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def _real_errors(stderr):
    """ERROR lines from the mysql client, ignoring the password-on-command-line warning."""
    return [ln.strip() for ln in stderr.splitlines() if ln.startswith("ERROR")]


def _migration_files(first):
    out = []
    for path in glob.glob(os.path.join(MIGRATIONS, "*.sql")):
        base = os.path.basename(path)
        m = re.match(r"^(\d+)_", base)
        if not m or "rollback" in base.lower():
            continue
        if int(m.group(1)) >= first:
            out.append((int(m.group(1)), base, path))
    out.sort()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=_env("MH_TEST_DB_HOST", "127.0.0.1"))
    ap.add_argument("--port", default=_env("MH_TEST_DB_PORT", "3306"))
    ap.add_argument("--user", default=_env("MH_TEST_DB_USER", "mh"))
    ap.add_argument("--password", default=_env("MH_TEST_DB_PASSWORD", "mh"))
    ap.add_argument("--name", default=_env("MH_TEST_DB_NAME", "mh_test"))
    ap.add_argument("--from-migration", type=int, default=74,
                    help="first migration number to apply on top of the fixture (default 74)")
    ap.add_argument("--strict", action="store_true",
                    help="fail on missing-table notes from migrations too")
    ap.add_argument("--force", action="store_true",
                    help="allow a database name that does not contain 'test' (it WILL be wiped)")
    args = ap.parse_args(argv)

    if "test" not in args.name.lower() and not args.force:
        print(f"refusing to wipe database {args.name!r}: the name does not contain 'test' "
              f"(pass --force if you really mean it)", file=sys.stderr)
        return 2
    if not os.path.exists(FIXTURE):
        print(f"fixture not found: {FIXTURE}", file=sys.stderr)
        return 2

    # 1. wipe
    rc, out, err = _run_sql(args, "SHOW TABLES;")
    if rc != 0:
        print("cannot connect / read the database:\n" + "\n".join(_real_errors(err) or [err]), file=sys.stderr)
        return 2
    tables = [t for t in out.splitlines()[1:] if t.strip()]
    if tables:
        drop = "SET FOREIGN_KEY_CHECKS=0;\n" + "".join(f"DROP TABLE IF EXISTS `{t}`;\n" for t in tables) \
               + "SET FOREIGN_KEY_CHECKS=1;\n"
        rc, _, err = _run_sql(args, drop)
        if rc != 0:
            print("could not drop existing tables:\n" + "\n".join(_real_errors(err)), file=sys.stderr)
            return 2
    print(f"[1/4] wiped {len(tables)} existing table(s) in {args.name}")

    # 2. fixture
    with open(FIXTURE, encoding="utf-8") as f:
        rc, _, err = _run_sql(args, f.read())
    if rc != 0:
        print("fixture failed to load:\n" + "\n".join(_real_errors(err)), file=sys.stderr)
        return 1
    print(f"[2/4] loaded {os.path.relpath(FIXTURE, _ROOT)}")

    # 3. migrations
    files = _migration_files(args.from_migration)
    warnings, failures = [], []
    for num, base, path in files:
        with open(path, encoding="utf-8") as f:
            rc, _, err = _run_sql(args, f.read(), force=True)
        for line in _real_errors(err):
            # 1146 = table doesn't exist; 1824 = a foreign key points at a table that doesn't exist.
            # Both mean "this migration is about a table the test fixture deliberately lacks".
            m = re.search(r"ERROR (?:1146 .*Table '[^']*\.([^']+)' doesn't exist|1824 .*referenced table '([^']+)')", line)
            if m:
                warnings.append((base, m.group(1) or m.group(2)))
            else:
                failures.append((base, line))
    print(f"[3/4] applied {len(files)} migration file(s) from {args.from_migration:03d}")
    grouped = {}
    for base, table in warnings:
        grouped.setdefault(base, set()).add(table)
    for base, tabs in sorted(grouped.items()):
        print(f"      note     {base}: partly skipped, table(s) not in the test fixture: {', '.join(sorted(tabs))}")
    for base, line in failures:
        print(f"      FAILURE  {base}: {line}", file=sys.stderr)

    # 4. sanity
    missing = []
    for table, column in REQUIRED:
        rc, out, _ = _run_sql(
            args, "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() "
                  f"AND table_name = '{table}' AND column_name = '{column}';")
        if rc != 0 or out.split()[-1:] != ["1"]:
            missing.append(f"{table}.{column}")
    print("[4/4] required columns: " + ("all present" if not missing else "MISSING " + ", ".join(missing)))

    if failures or missing or (args.strict and warnings):
        return 1
    print(f"OK: run the tests with  MH_TEST_DB=1 MH_TEST_DB_PORT={args.port} "
          f"MH_TEST_DB_NAME={args.name} /tmp/venv/bin/python -m pytest tests -q -p no:cacheprovider")
    return 0


if __name__ == "__main__":
    sys.exit(main())
