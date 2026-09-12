#!/usr/bin/env python3
"""
apply_resource_counts_duplicate_route_fix.py

Root cause of "tiles still don't hide after the first patch"
--------------------------------------------------------------
app/api/live_data.py already defined /api/live/resource-counts/{id}
TWICE, before either of us touched it:

  1. An old definition (~line 207) that calls get_account_summary()
     and returns keys like "ec2_total", "ebs_total", etc.
  2. A newer definition (~line 310, the one
     apply_extended_service_resource_counts_fix.py correctly updated)
     that returns plain per-service keys ("ec2", "acm", "dms", ...)
     matching what ServiceList.jsx actually reads.

FastAPI/Starlette matches routes in registration order and stops at
the first match. Definition #1 always wins, so #2 — the one with the
correct logic — was silently DEAD CODE the whole time. The frontend
was calling the endpoint, getting back "_total"-suffixed keys it
doesn't recognize, treating every single service as "unknown", and
failing OPEN across the board (which is also why core tiles that
previously looked "fixed" were really just visible because they
happen to have real resources, not because the filter logic engaged).

This also explains the 52-second load time: get_account_summary()
does much heavier per-instance CloudWatch work than the lightweight
describe/list-only collectors in the correct definition — unrelated to
the new ACM/Backup/DMS/DirectConnect/StepFunctions collectors.

Fix
---
1. Delete the old, shadowing duplicate route definition entirely, so
   the correct one (with real per-service keys) is the only one and
   actually gets called.
2. Parallelize the surviving endpoint's per-service collector calls
   with a small ThreadPoolExecutor (same pattern already used by
   live_accounts() in this file) instead of looping sequentially, so
   one slow/denied AWS API call doesn't stall the whole request.

Usage:
    python apply_resource_counts_duplicate_route_fix.py [repo_root]

Idempotent: safe to re-run. Backs up the file to
"live_data.py.bak.pre-duplicate-route-fix" (first run only). Reverts
automatically if the patched file fails py_compile.
"""
import py_compile
import shutil
import sys
from pathlib import Path

BAK_SUFFIX = ".bak.pre-duplicate-route-fix"


class PatchError(Exception):
    pass


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def backup(path: Path):
    bak = path.with_name(path.name + BAK_SUFFIX)
    if not bak.exists():
        shutil.copy2(path, bak)


OLD_DUPLICATE_ROUTE = '''@router.get("/resource-counts/{account_db_id}")
def live_resource_counts(account_db_id: int):
    """
    Lightweight per-account resource counts for the Services page
    (ServiceList.jsx tile visibility) — deliberately scoped to ONE
    account instead of GET /api/live/accounts, which fans out to
    every active account (including ones with no real deployment,
    e.g. accounts without a working role/YACE setup) and makes the
    caller wait for the slowest one. Reuses the same 60s in-process
    cache in collector_direct.py, so this costs nothing extra beyond
    what get_account_summary() already does for the Overview page.
    """
    acc     = _get_db_account(account_db_id)
    region  = acc.get("default_region")
    summary = get_account_summary(region)
    return {
        "ec2_total":    summary.get("ec2_total",    0),
        "ebs_total":    summary.get("ebs_total",    0),
        "rds_total":    summary.get("rds_total",    0),
        "lambda_total": summary.get("lambda_total", 0),
        "s3_total":     summary.get("s3_total",     0),
        "elb_total":    summary.get("elb_total",    0),
        "ecs_total":    summary.get("ecs_total",    0),
    }


'''

OLD_SEQUENTIAL_ENDPOINT = '''@router.get("/resource-counts/{account_db_id}")
def live_resource_counts(account_db_id: int):
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region")
    counts = {}
    for svc, collector in _RESOURCE_COLLECTORS.items():
        try:
            counts[svc] = len(collector(region))
        except Exception as e:
            # Unknown, not zero — a transient AWS/permissions error
            # shouldn't hide a tile that may well have real resources.
            logger.warning(f"resource-counts: {svc} failed for account {account_db_id}: {e}")
            counts[svc] = None
    return counts'''

NEW_PARALLEL_ENDPOINT = '''@router.get("/resource-counts/{account_db_id}")
def live_resource_counts(account_db_id: int):
    acc    = _get_db_account(account_db_id)
    region = acc.get("default_region")
    counts = {}

    def _one(svc, collector):
        try:
            return svc, len(collector(region))
        except Exception as e:
            # Unknown, not zero — a transient AWS/permissions error
            # shouldn't hide a tile that may well have real resources.
            logger.warning(f"resource-counts: {svc} failed for account {account_db_id}: {e}")
            return svc, None

    with ThreadPoolExecutor(max_workers=len(_RESOURCE_COLLECTORS)) as ex:
        futures = [ex.submit(_one, svc, collector) for svc, collector in _RESOURCE_COLLECTORS.items()]
        for f in as_completed(futures):
            svc, count = f.result()
            counts[svc] = count
    return counts'''


def main():
    repo_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    path = repo_root / "app" / "api" / "live_data.py"
    print(f"Repo root: {repo_root}")
    print(f"Target: {path}")

    text = read(path)

    marker = "def _one(svc, collector):"
    if marker in text:
        print("  SKIP  already patched (parallel endpoint + duplicate removed)")
        return

    if text.count(OLD_DUPLICATE_ROUTE) == 0:
        raise PatchError(
            "Could not find the shadowing duplicate /resource-counts route. "
            "It may already have been removed, or the file has diverged from "
            "what this script expects — check manually."
        )
    if text.count(OLD_DUPLICATE_ROUTE) > 1:
        raise PatchError("Duplicate route pattern matched more than once — aborting.")

    if text.count(OLD_SEQUENTIAL_ENDPOINT) != 1:
        raise PatchError(
            "Could not find exactly one copy of the sequential resource-counts "
            "endpoint from apply_extended_service_resource_counts_fix.py — "
            "make sure that script ran first."
        )

    backup(path)

    text = text.replace(OLD_DUPLICATE_ROUTE, "", 1)
    text = text.replace(OLD_SEQUENTIAL_ENDPOINT, NEW_PARALLEL_ENDPOINT, 1)

    # ThreadPoolExecutor/as_completed are already imported at the top of
    # this file (used by live_accounts() above) — no new import needed.

    path.write_text(text, encoding="utf-8")
    print("  OK    removed shadowing duplicate route + parallelized the real one")

    print("\nCompiling patched file...")
    try:
        py_compile.compile(str(path), doraise=True)
        print("  OK    live_data.py compiles cleanly")
    except py_compile.PyCompileError as e:
        print(f"  FAIL  {e}\n  Reverting...")
        bak = path.with_name(path.name + BAK_SUFFIX)
        shutil.copy2(bak, path)
        print("  Reverted. No changes applied.")
        sys.exit(1)

    print(
        "\nDone. Restart the backend now — this is a route-table change, "
        "'--reload' should pick it up automatically but restart manually if "
        "you're not sure. Then hard-refresh the Services page and re-check "
        "the resource-counts/{id} response in the Network tab: you should "
        "now see plain keys (ec2, ebs, rds, lambda, s3, elb, alb, nlb, ecs, "
        "acm, backup, dms, directconnect, states) instead of the old "
        "'_total'-suffixed ones, and the request should take well under a "
        "second instead of ~50s."
    )


if __name__ == "__main__":
    main()
