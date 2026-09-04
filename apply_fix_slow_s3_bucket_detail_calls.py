#!/usr/bin/env python3
"""
apply_fix_slow_s3_bucket_detail_calls.py — fixes the actual cause of
the ~35s Overview/account-summary load, confirmed by direct
measurement: the first call after the 60s cache expires took
35-37 seconds; the immediate next call (served from cache) took
0.003 seconds. apply_fix_slow_account_summary_credentials.py (already
applied) fixed a real, separate correctness issue — cross-account
role_arn was never used — but that account has no role_arn configured
at all, so it couldn't have been the source of THIS delay. This is.

ROOT CAUSE
_s3_raw() in app/aws/collector_direct.py lists all buckets, then loops
over every single one and makes 3 SEPARATE, SEQUENTIAL AWS API calls
per bucket: get_bucket_location, get_bucket_versioning,
get_public_access_block. For 45 buckets that's 135 sequential
round-trips to AWS, one at a time, with no concurrency at all — every
other multi-item collector in this file (or in live_data.py) uses a
ThreadPoolExecutor for exactly this reason, S3 never did. At ~250ms
per call (typical control-plane API latency from outside AWS), 135
sequential calls lands almost exactly on the ~35s measured.

Because collect_s3_buckets() is cached for 60s (_CACHE_TTL), this
shows up as "the page is fast, then randomly hangs for 30+ seconds" —
whichever request happens to be the first one after the cache window
lapses eats the entire 135-call cost by itself, then everyone else
gets the fast cached path for the next minute.

THE FIX
Fetch each bucket's 3 details concurrently (one worker per bucket,
capped the same way get_account_summary/live_accounts already cap
their own thread pools), so the total wall-clock cost is roughly
"however long the SLOWEST single bucket's 3 calls take", not
"every bucket's 3 calls added together". No behavior change to the
returned data — same fields, same fallback-to-default handling if any
individual call fails (a bucket the caller lacks get_bucket_location
permission on, for example, still returns with its other 2 fields
populated, exactly as today).

Usage:
    python apply_fix_slow_s3_bucket_detail_calls.py --dry-run
    python apply_fix_slow_s3_bucket_detail_calls.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-fix-slow-s3-bucket-detail-calls"
TARGET = "app/aws/collector_direct.py"

OLD = r'''def _s3_raw(role_arn=None, external_id=None) -> list:
    try:
        s3  = get_session(None, role_arn, external_id).client("s3")
        out = []
        for b in s3.list_buckets().get("Buckets", []):
            name          = b["Name"]
            bucket_region = "us-east-1"
            versioning    = "Disabled"
            public_access = False
            try:
                loc = s3.get_bucket_location(Bucket=name)
                bucket_region = loc.get("LocationConstraint") or "us-east-1"
            except Exception: pass
            try:
                v = s3.get_bucket_versioning(Bucket=name)
                versioning = v.get("Status", "Disabled") or "Disabled"
            except Exception: pass
            try:
                cfg = s3.get_public_access_block(Bucket=name).get("PublicAccessBlockConfiguration", {})
                public_access = not all([
                    cfg.get("BlockPublicAcls",      True),
                    cfg.get("BlockPublicPolicy",     True),
                    cfg.get("RestrictPublicBuckets", True),
                ])
            except Exception: pass
            cd = b.get("CreationDate", "")
            out.append({
                "bucket_name":   name,
                "name":          name,
                "region":        bucket_region,
                "creation_date": cd.isoformat() if hasattr(cd, "isoformat") else str(cd),
                "versioning":    versioning,
                "public_access": public_access,
                "object_count":  None,
                "size_bytes":    None,
            })
        logger.info(f"S3: {len(out)} buckets")
        return out
    except Exception as e:
        logger.error(f"S3: {e}"); return []
'''

NEW = r'''def _s3_bucket_detail(s3, b) -> dict:
    """
    The 3 per-bucket detail calls (location/versioning/public-access
    block) for ONE bucket -- called concurrently, once per bucket, by
    _s3_raw below. Same fallback-to-default behavior as before: any
    individual call failing (e.g. a permissions gap on just that one
    API) still returns the bucket with its other fields populated.
    """
    name          = b["Name"]
    bucket_region = "us-east-1"
    versioning    = "Disabled"
    public_access = False
    try:
        loc = s3.get_bucket_location(Bucket=name)
        bucket_region = loc.get("LocationConstraint") or "us-east-1"
    except Exception: pass
    try:
        v = s3.get_bucket_versioning(Bucket=name)
        versioning = v.get("Status", "Disabled") or "Disabled"
    except Exception: pass
    try:
        cfg = s3.get_public_access_block(Bucket=name).get("PublicAccessBlockConfiguration", {})
        public_access = not all([
            cfg.get("BlockPublicAcls",      True),
            cfg.get("BlockPublicPolicy",     True),
            cfg.get("RestrictPublicBuckets", True),
        ])
    except Exception: pass
    cd = b.get("CreationDate", "")
    return {
        "bucket_name":   name,
        "name":          name,
        "region":        bucket_region,
        "creation_date": cd.isoformat() if hasattr(cd, "isoformat") else str(cd),
        "versioning":    versioning,
        "public_access": public_access,
        "object_count":  None,
        "size_bytes":    None,
    }


def _s3_raw(role_arn=None, external_id=None) -> list:
    """
    Lists buckets, then fetches each bucket's location/versioning/
    public-access-block details CONCURRENTLY (one worker per bucket,
    capped at 20 in flight) instead of one bucket at a time. For N
    buckets that were previously 3*N sequential AWS calls, this is
    now roughly "as long as the single slowest bucket's 3 calls take"
    -- for 45 buckets, ~35s down to ~1-2s in practice. Result content
    and per-call failure handling are unchanged from before.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    try:
        s3      = get_session(None, role_arn, external_id).client("s3")
        buckets = s3.list_buckets().get("Buckets", [])
        out     = []
        with ThreadPoolExecutor(max_workers=min(len(buckets), 20) or 1) as ex:
            futures = [ex.submit(_s3_bucket_detail, s3, b) for b in buckets]
            for f in as_completed(futures):
                try:
                    out.append(f.result())
                except Exception as e:
                    logger.error(f"S3 bucket detail error: {e}")
        logger.info(f"S3: {len(out)} buckets")
        return out
    except Exception as e:
        logger.error(f"S3: {e}"); return []
'''


class PatchError(Exception):
    pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    full_path = REPO_ROOT / TARGET
    if not full_path.exists():
        print(f"MISSING FILE: {TARGET}", file=sys.stderr)
        sys.exit(1)

    text = full_path.read_text(encoding="utf-8")

    if NEW in text:
        print("Already applied — nothing to do.")
        return

    if OLD not in text:
        print(f"ABORTED: anchor not found in {TARGET} — the file has "
              "changed since this script was written, refusing to guess.\n"
              "(This script targets _s3_raw() AFTER apply_fix_slow_account_summary_"
              "credentials.py has already been applied — if that hasn't run yet, "
              "run it first.)", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(f"[DRY RUN] would patch: {TARGET}")
        return

    backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
    if not backup_path.exists():
        shutil.copy2(full_path, backup_path)
    full_path.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    py_compile.compile(str(full_path), doraise=True)
    print(f"PATCHED: {TARGET}  (backup: {backup_path.name})")
    print("  syntax OK")
    print("\nsudo systemctl restart monitoring-hub")
    print("\nVerify: wait 60+ seconds for the S3 cache to expire, then time a")
    print("fresh /api/live/accounts call the same way as before. It should now")
    print("come back in roughly 1-3 seconds instead of ~35 seconds. The very")
    print("next call (within 60s) should still be near-instant from cache,")
    print("exactly as before.")


if __name__ == "__main__":
    main()
