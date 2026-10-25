#!/usr/bin/env python3
"""
Backfill resources.env_resolved / env_source / env_reason (phase 4a, migration 083).

DRY-RUN BY DEFAULT: it classifies every live resource, prints how many land in each
source (manual / tag / name / related / account / unknown) and each environment (prod / nonprod; a resource
nobody has placed is prod with source 'unknown'), how many rows
would change, and writes nothing. Re-run with --apply to write.

Safe to re-run: only rows whose stored classification differs are written, so a second
run reports 0 changes. It never touches env_override (a user's manual choice) and
changes no alerting by itself: the evaluator only reads these columns when
ENV_AWARE_CLASSIFICATION=true.

    cd /opt/monitoring-hub/app
    /opt/monitoring-hub/venv/bin/python3 scripts/backfill_environments.py              # dry run
    /opt/monitoring-hub/venv/bin/python3 scripts/backfill_environments.py --apply
    /opt/monitoring-hub/venv/bin/python3 scripts/backfill_environments.py --account-id 7
"""
import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# app.db refuses to import without DB_PASSWORD; the service gets it from .env, so a standalone
# script must load the same file (migrate.py does the same).
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(_ROOT, ".env"))

from app.collector.env_sync import sync_resource_environments  # noqa: E402


def _fmt(d):
    return ", ".join(f"{k}={v}" for k, v in sorted(d.items())) or "(none)"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the classification (default: dry run)")
    ap.add_argument("--account-id", type=int, default=None, help="limit to one aws_accounts.id")
    args = ap.parse_args(argv)

    try:
        rep = sync_resource_environments(account_id=args.account_id, dry_run=not args.apply)
    except Exception as e:
        if getattr(e, "errno", None) == 1054:
            print("Migration 083 is not applied on this database: run migrate.py apply --all-pending first.")
            return 2
        raise

    print(("APPLIED" if args.apply else "DRY RUN (nothing written)")
          + (f", account {args.account_id}" if args.account_id else ", all accounts"))
    print(f"resources considered : {rep['total']}")
    print(f"by source            : {_fmt(rep['by_source'])}")
    print(f"by environment       : {_fmt(rep['by_env'])}")
    print(f"rows that change     : {rep['changed']}  (by source: {_fmt(rep['changed_by_source'])})")
    print(f"newly unplaced       : {rep['newly_unknown']}  -> PROD (fail-safe) with env_source='unknown'; they appear in the Needs-classification queue")
    if args.apply:
        print(f"rows written         : {rep['applied']}")
    else:
        print("re-run with --apply to write these.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
