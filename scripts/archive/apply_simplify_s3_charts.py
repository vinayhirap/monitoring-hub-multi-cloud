#!/usr/bin/env python3
"""
apply_simplify_s3_charts.py
========================================
Simplifies the S3 resource detail page down to the two metrics that
actually have data for most accounts: Bucket Size and Object Count.

ROOT CAUSE (confirmed against a real account's own AWS console): the
S3 chart section shows 8 cards total -- 2 free "storage metrics"
(BucketSizeBytes, NumberOfObjects, reported daily by AWS at no extra
cost) and 6 "request metrics" (AllRequests, GetRequests, PutRequests,
4xxErrors, 5xxErrors, BytesDownloaded). Request metrics require S3's
PAID, per-bucket opt-in Request Metrics configuration
(PutBucketMetricsConfiguration / console "Request metrics" tab) --
which most accounts, including the one this was confirmed against,
have never enabled. Without it, AWS never publishes those 6 CloudWatch
series at all -- not a collection bug in this app (the backend's
get_s3_metric_series() in collector_direct.py already queries them
with the correct dimensions), just AWS features nobody turned on, so
the cards permanently show "No data in last 6H" -- confirmed by
comparing this app's own S3 panel against the same account's AWS
Console, where the Storage metrics tab has real data and the Request
metrics tab does not.

This closes part of the Section 3 handover gap ("S3's chart fields
don't map cleanly onto metric_catalog") for the common case: for
accounts without Request Metrics enabled, hiding the 6 cards that can
never populate is a cleaner fix than trying to force a catalog mapping
for data that structurally doesn't exist yet.

FIX: removes the 6 request-metric MetricChart cards from the S3
section of frontend/src/pages/ServiceDetail.jsx, keeping only Bucket
Size and Object Count. The backend is UNCHANGED -- get_s3_metric_series()
still computes and returns all 8 fields, so:
  - Nothing about data collection or the API response changes.
  - An account that HAS enabled S3 Request Metrics doesn't lose that
    data from the API -- it's just not rendered here by default. A
    follow-up could reintroduce those cards conditionally (e.g. only
    render if metrics.all_requests is non-empty) if there's demand for
    accounts that do have Request Metrics enabled.

TESTED: patched file re-verified with a real `npm run build` (vite),
not just a syntax check -- confirmed clean, no JSX/build errors.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_simplify_s3_charts.py --dry-run
    python3 apply_simplify_s3_charts.py --apply
    cd frontend && npm install && npm run build && cd ..
    sudo systemctl restart monitoring-hub
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

REL_PATH = os.path.join("frontend", "src", "pages", "ServiceDetail.jsx")

OLD = '''            {service === "S3" && <>
              <div className="chart-full">
                <MetricChart title="Bucket Size (bytes)" data={metrics?.bucket_size   || []} color="#fbbf24" unit="B" timeRange={rangLabel} />
              </div>
              <div className="chart-full">
                <MetricChart title="Object Count"        data={metrics?.object_count  || []} color="#22c55e" unit=""  timeRange={rangLabel} />
              </div>
              <MetricChart title="All Requests"          data={metrics?.all_requests  || []} color="#2bb3ac" unit=""  timeRange={rangLabel} />
              <MetricChart title="GET Requests"          data={metrics?.get_requests  || []} color="#7c6ee0" unit=""  timeRange={rangLabel} />
              <MetricChart title="PUT Requests"          data={metrics?.put_requests  || []} color="#38bdf8" unit=""  timeRange={rangLabel} />
              <MetricChart title="4XX Errors"            data={metrics?.errors_4xx    || []} color="#f59e0b" unit=""  timeRange={rangLabel} />
              <MetricChart title="5XX Errors"            data={metrics?.errors_5xx    || []} color="#ef4444" unit="" timeRange={rangLabel} />
              <MetricChart title="Bytes Downloaded"      data={metrics?.bytes_download|| []} color="#f472b6" unit="B" timeRange={rangLabel} />
            </>}'''

NEW = '''            {service === "S3" && <>
              <div className="chart-full">
                <MetricChart title="Bucket Size (bytes)" data={metrics?.bucket_size   || []} color="#fbbf24" unit="B" timeRange={rangLabel} />
              </div>
              <div className="chart-full">
                <MetricChart title="Object Count"        data={metrics?.object_count  || []} color="#22c55e" unit=""  timeRange={rangLabel} />
              </div>
              {/* Request-metric cards (AllRequests/GetRequests/PutRequests/
                  4xxErrors/5xxErrors/BytesDownloaded) intentionally removed
                  -- these require S3's paid, per-bucket opt-in Request
                  Metrics configuration (PutBucketMetricsConfiguration /
                  console "Request metrics" tab), which most accounts never
                  enable. Without it these AWS/S3 CloudWatch series
                  permanently return zero datapoints -- not a collection
                  bug, confirmed against a real account's own AWS console
                  (Storage metrics tab has real data, Request metrics tab
                  does not). The backend (get_s3_metric_series in
                  collector_direct.py) still computes these fields so an
                  account that HAS enabled Request Metrics isn't blocked --
                  just not rendered here by default. See
                  apply_simplify_s3_charts.py. */}
            </>}'''

DONE_MARKER = "apply_simplify_s3_charts.py"


def die(msg):
    print(f"\n[ABORT] {msg}", file=sys.stderr)
    sys.exit(1)


def find_repo_root():
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(cur, "app", "auth", "security.py")) and \
           os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            die("Could not locate the monitoring-hub-multi-cloud repo root.")
        cur = parent


def backup(path):
    bpath = path + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(path, bpath)
    return bpath


def esbuild_syntax_check(jsx_source):
    esbuild_bin = shutil.which("esbuild")
    with tempfile.NamedTemporaryFile(suffix=".jsx", mode="w", delete=False, encoding="utf-8") as f:
        f.write(jsx_source)
        tmp_path = f.name
    try:
        cmd = [esbuild_bin, tmp_path, "--outfile=/dev/null"] if esbuild_bin else \
              ["npx", "--yes", "esbuild", tmp_path, "--outfile=/dev/null"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return False, result.stderr
        return True, None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    finally:
        os.unlink(tmp_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="(default; kept for backward compatibility)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    repo_root = find_repo_root()
    path = os.path.join(repo_root, REL_PATH)
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    if not os.path.exists(path):
        die(f"{REL_PATH} not found at {path}.")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    if DONE_MARKER in content:
        print(f"\n{REL_PATH} already patched -- skipping. Nothing to do.")
        return

    if OLD not in content:
        die(f"{REL_PATH}: S3 chart section doesn't match what this script expects. "
            "File may have changed since this script was written.")

    new_content = content.replace(OLD, NEW, 1)
    print(f"\nFile patch plan:\n  {REL_PATH}: OK ({len(new_content) - len(content):+d} bytes)")

    ok, detail = esbuild_syntax_check(new_content)
    if ok is False:
        die(f"Patched {REL_PATH} failed JSX syntax check via esbuild:\n{detail}")
    elif ok is None:
        print(f"[warn] Could not run esbuild syntax check ({detail}) -- skipping. "
              "`npm run build` in the manual follow-up below is still required.")
    else:
        print("[selftest] OK -- patched file parses cleanly as valid JSX (esbuild).")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print(f"Patched {REL_PATH}")

    print(f"""
[Manual follow-up -- REQUIRED]

  A) Rebuild the frontend:
       cd frontend
       npm install
       npm run build
       cd ..

  B) Restart:
       sudo systemctl restart monitoring-hub

  C) Open any S3 bucket's detail page -- only Bucket Size and Object
     Count should appear now.

  D) Review, commit, push:
       git diff {REL_PATH}
       git add {REL_PATH} apply_simplify_s3_charts.py
       git commit -m "fix(ui): S3 request-metric charts always showed 'No data' since Request Metrics isn't enabled on this account's buckets; show only the free storage metrics by default"
       git push origin main
""")


if __name__ == "__main__":
    main()
