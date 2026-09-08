#!/usr/bin/env python3
"""
apply_azure_direct_metrics_fetch.py
========================================
Monitoring Hub -- Phase 2 of removing VictoriaMetrics: Azure direct
metric fetch, replacing VM for Azure. GCP remains on VM for now (Phase 3).
AWS is already off VM (Phase 1, apply_direct_gmd_metrics_revival.py).

WHAT THIS DOES
--------------
1. Investigation finding (same shape as Phase 1's "revive, don't rewrite"
   discovery, but not identical): there is no disabled/dormant direct-fetch
   code to revive for Azure. What DOES already exist is
   app/providers/azure/metrics_collector.py -- a complete, working,
   already-batched (50 resource IDs/call) Azure Monitor MetricsClient
   integration. It already does the real work (auth, batching, the
   query_resources() call itself); it just points its output at
   VictoriaMetrics instead of this project's own `metrics` /
   `metric_history` tables. So Phase 2 is a retarget of a proven
   integration, not a from-scratch build -- closer in spirit to Phase 1
   than the original brief assumed.

2. Retargets app/providers/azure/metrics_collector.py's
   collect_account_metrics(): every returned datapoint (not just the
   latest) is now written to `metric_history`, and the latest value per
   (resource, metric) is upserted into `metrics` -- mirroring Phase 1's
   AWS GMD collector exactly (app/collector/metrics/runner.py's
   _execute_gmd()). The VictoriaMetrics push (vm_write_batch) is removed
   for Azure. Metric names written are metric_catalog's own stored name
   (e.g. "Percentage CPU"), matching what metrics_vm_sync.py used to
   reconstruct from VM and what alert_evaluator.py's join against
   metric_catalog expects -- verified against _sync_azure_gcp_metrics()'s
   existing convention in metrics_vm_sync.py before this fix.

3. Correctness-critical fix (same category as Phase 1's, applied to
   Azure this time): stops metrics_vm_sync.py's sync_metrics_from_vm()
   from also syncing Azure rows from VM. Once the collector above writes
   Azure's last-value cache directly, letting the VM sync job also run
   afterward and overwrite those same rows from VM's separately-scraped
   data would silently undo the fresher direct values every cycle --
   exactly the AWS Phase 1 race, now for Azure. GCP rows are unaffected
   and still sync from VM exactly as before, until Phase 3.

4. Removes the now-dead `_slug()` helper and `import re` from
   metrics_collector.py -- it existed only to build the VM series-name
   suffix, which no longer exists on this path. metrics_vm_sync.py keeps
   its own separate copy (still needed there for GCP).

5. Updates multicloud_scheduler.py's log line for the Azure branch only,
   so it accurately says "written directly" instead of "pushed" (GCP's
   line is untouched -- it still genuinely pushes to VM until Phase 3).

WHAT THIS DOES NOT DO
----------------------
- Does not touch GCP collection at all -- that's Phase 3.
- Does not touch dashboard/chart code to read from metric_history yet
  (Phase 4) -- Azure charts still read from VM until then, same known
  gap Phase 1 left for AWS.
- Does not remove VictoriaMetrics, vm_client.py, or any VM code -- GCP
  and existing dashboard reads still depend on it.
- Does not create metric_history -- Phase 1 already created it live on
  the server. This script verifies it exists and aborts with a clear
  message if it doesn't, rather than silently recreating a table whose
  canonical schema Phase 1 already owns.

TESTED: the new write-path logic (building metrics_rows/history_rows from
mocked azure-monitor-query SDK response objects, mocked DB cursors) and
the vm_sync Azure-skip logic were exercised locally -- confirms datapoints
route to both `metrics` (latest value) and `metric_history` (full series)
using metric_catalog's exact metric_name, and confirms
sync_metrics_from_vm() no longer touches Azure rows while GCP behavior is
byte-identical to before. NOT tested: an actual live Azure Monitor call
against a real tenant -- no Azure credentials or network access available
here; verify on the server by watching the next multicloud_scheduler
cycle's logs (see step B below).

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_azure_direct_metrics_fetch.py --dry-run
    python3 apply_azure_direct_metrics_fetch.py --apply

NOTE: this script only edits files -- no DB write is needed as part of
--apply (metric_history already exists), but it DOES run a read-only
`SHOW TABLES` check as a safety gate before patching. That check needs
DB credentials the same way apply_direct_gmd_metrics_revival.py did, so
per HANDOVER.md #4 run this as `sudo python3 ...` (root), not
`sudo -u cloudops python3 ...`, since .env is currently 600/hcsadmin-only.
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime

# ─────────────────────────── DB: verify metric_history exists ───────────────────────────


def _load_db_password():
    env_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        "/opt/monitoring-hub/app/.env",
        "/opt/monitoring-hub/.env.production",
    ]
    for key in ("DB_PASSWORD", "MONITOR_DB_PASSWORD"):
        if os.environ.get(key):
            return os.environ[key]
    for path in env_candidates:
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() in ("DB_PASSWORD", "MONITOR_DB_PASSWORD") and v.strip():
                    return v.strip().strip('"').strip("'")
    return None


def verify_metric_history_exists(dry_run):
    db_pass = _load_db_password()
    db_host = os.environ.get("DB_HOST", "127.0.0.1")
    db_port = os.environ.get("DB_PORT", "3306")
    db_user = os.environ.get("DB_USER", "monitor")
    db_name = os.environ.get("DB_NAME", "monitoring_hub")

    cmd = ["mysql", f"-u{db_user}", "-h", db_host, "-P", db_port, "-N", "-B"]
    if db_pass:
        cmd.append(f"-p{db_pass}")
    cmd.append(db_name)

    if dry_run:
        print("[DRY-RUN] would run: SHOW TABLES LIKE 'metric_history' (verify only, no DDL)")
        return True

    result = subprocess.run(
        cmd, input="SHOW TABLES LIKE 'metric_history';",
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[ERROR] Could not query the database:\n{result.stderr}", file=sys.stderr)
        return False
    if "metric_history" not in result.stdout:
        print(
            "[ERROR] metric_history table not found. This script assumes Phase 1 "
            "(apply_direct_gmd_metrics_revival.py) already created it -- re-run that "
            "script's table-creation step, or create it manually, before Phase 2.",
            file=sys.stderr,
        )
        return False
    print("Verified metric_history table exists.")
    return True


# ─────────────────────────── file patches ───────────────────────────

COLLECTOR_DOCSTRING_OLD = '''# app/providers/azure/metrics_collector.py
"""
Pulls metric VALUES for an Azure account's enabled metric selection and
pushes them into VictoriaMetrics.

Why this exists: AWS's tiered pipeline is YACE (a standalone Prometheus
exporter binary) scraping CloudWatch and pushing to VM on its own -- no
Python collector loop is involved for AWS metric *values* anymore (see
app/collector/scheduler.py's comment: "GMD collection skipped -- VM/YACE
migration in progress"). There is no YACE-equivalent for Azure, so
something has to actively pull Azure Monitor and push to VM. This is that
something.'''

COLLECTOR_DOCSTRING_NEW = '''# app/providers/azure/metrics_collector.py
"""
Pulls metric VALUES for an Azure account's enabled metric selection and
writes them DIRECTLY into the local `metrics` last-value cache and
`metric_history` table -- Azure's counterpart to Phase 1's AWS direct
GetMetricData revival (see apply_direct_gmd_metrics_revival.py). Before
Phase 2 (apply_azure_direct_metrics_fetch.py) this pushed into
VictoriaMetrics instead, and metrics_vm_sync.py pulled the values back out
again for alert_evaluator.py to read -- an unnecessary VM round-trip once
Azure Monitor is already being called directly here. GCP is unaffected --
see Phase 3.

Why a Python collector loop exists here at all (unlike AWS, which uses
YACE, a standalone Prometheus exporter binary scraping CloudWatch on its
own): there is no YACE-equivalent for Azure, so something has to actively
pull Azure Monitor. This is that something.'''

COLLECTOR_IMPORTS_OLD = '''import logging
import re
from datetime import timedelta

from app.db import get_connection
from app.credentials import load_credential
from app.clients.vm_client import vm_write_batch

logger = logging.getLogger(__name__)

_BATCH_SIZE = 50  # Azure Monitor Metrics Batch API hard limit per call


def _slug(name: str) -> str:
    """'Percentage CPU' -> 'percentage_cpu' for the VM metric name suffix."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return s or "value"'''

COLLECTOR_IMPORTS_NEW = '''import logging
from datetime import timedelta

from app.db import get_connection
from app.credentials import load_credential
from app.collector.metrics_writer import write_metrics_batch, write_metric_history_batch

logger = logging.getLogger(__name__)

_BATCH_SIZE = 50  # Azure Monitor Metrics Batch API hard limit per call'''

COLLECTOR_WRITE_OLD = '''                # MetricsClient.query_resources returns results in the same
                # order as resource_ids -- there's no resource_id field on
                # the result object itself (verified against the SDK's
                # MetricsQueryResult dataclass), so map back positionally.
                series = []
                for resource_row, query_result in zip(chunk, query_results):
                    for metric in query_result.metrics:
                        for ts_elem in metric.timeseries:
                            if not ts_elem.data:
                                continue
                            latest = ts_elem.data[-1]  # most recent datapoint in the window
                            value = latest.average
                            if value is None:
                                continue
                            series.append({
                                "metric": f"azure_{service}_{_slug(metric.name)}",
                                "labels": {
                                    "account_id": str(account["id"]),
                                    "resource_id": resource_row["resource_id"],
                                    "resource_name": resource_row["name"] or "",
                                    "region": region,
                                },
                                "value": float(value),
                            })
                if series:
                    if vm_write_batch(series):
                        result["pushed"] += len(series)
                    else:
                        result["errors"].append(f"{service}: VM write failed for {len(series)} points")'''

COLLECTOR_WRITE_NEW = '''                # MetricsClient.query_resources returns results in the same
                # order as resource_ids -- there's no resource_id field on
                # the result object itself (verified against the SDK's
                # MetricsQueryResult dataclass), so map back positionally.
                #
                # metrics_rows: (resource_db_id, metric_name, value) -> latest
                #   value only, upserted into `metrics` for alert_evaluator.py.
                # history_rows: (resource_db_id, metric_name, value, timestamp)
                #   -> every returned datapoint, appended into `metric_history`.
                # metric_name here is metric.name, the SDK's echo of the exact
                # string this account's metric_catalog row requested -- matches
                # what metrics_vm_sync.py's _sync_azure_gcp_metrics() used to
                # write into `metrics` from VM, and what alert_evaluator.py's
                # join against metric_catalog expects.
                metrics_rows = []
                history_rows = []
                for resource_row, query_result in zip(chunk, query_results):
                    for metric in query_result.metrics:
                        for ts_elem in metric.timeseries:
                            if not ts_elem.data:
                                continue
                            for point in ts_elem.data:
                                value = point.average
                                if value is None:
                                    continue
                                history_rows.append((
                                    resource_row["id"], metric.name,
                                    float(value), point.timestamp,
                                ))
                            latest = ts_elem.data[-1]  # most recent datapoint in the window
                            value = latest.average
                            if value is None:
                                continue
                            metrics_rows.append((resource_row["id"], metric.name, float(value)))
                if metrics_rows:
                    write_metrics_batch(metrics_rows)
                    result["pushed"] += len(metrics_rows)
                if history_rows:
                    write_metric_history_batch(history_rows)'''

VMSYNC_OLD = '''    rows = _fetch_enabled_threshold_targets()
    if not rows:
        logger.info("VM metrics sync: no enabled thresholds -- nothing to do")
        return 0

    other_rows = [r for r in rows if (r.get("provider") or "aws") != "aws"]

    other_datapoints, other_skipped, other_matched = _sync_azure_gcp_metrics(other_rows)

    datapoints = other_datapoints
    matched = other_matched

    write_metrics_batch(datapoints)

    total_skipped = sum(other_skipped.values())
    if total_skipped:
        detail_parts = [
            f"{prov}:{svc}/{metric} x{n}" for (prov, svc, metric), n in sorted(other_skipped.items())
        ]
        logger.info(
            f"VM metrics sync (azure/gcp only -- AWS now handled by direct GMD): "
            f"{matched} written, {total_skipped} skipped (no VM series yet) -- "
            f"{', '.join(detail_parts)}"
        )
    else:
        logger.info(f"VM metrics sync (azure/gcp only -- AWS now handled by direct GMD): "
                     f"{matched} written, 0 skipped")

    return matched'''

VMSYNC_NEW = '''    rows = _fetch_enabled_threshold_targets()
    if not rows:
        logger.info("VM metrics sync: no enabled thresholds -- nothing to do")
        return 0

    # Azure is intentionally NOT synced from VM here anymore (Phase 2, see
    # apply_azure_direct_metrics_fetch.py): app/providers/azure/metrics_collector.py
    # now writes Azure's last-value cache directly from Azure Monitor, and
    # multicloud_scheduler.py runs it on its own interval, independent of this
    # sync job. If this function also synced Azure from VM afterward, it would
    # race with those direct writes and could silently overwrite fresher values
    # with stale/duplicate VM data on every cycle -- the same correctness bug
    # Phase 1's AWS exclusion fixed. GCP is unaffected and still syncs from VM
    # exactly as before, until Phase 3 replaces that too.
    other_rows = [r for r in rows if (r.get("provider") or "aws") not in ("aws", "azure")]

    other_datapoints, other_skipped, other_matched = _sync_azure_gcp_metrics(other_rows)

    datapoints = other_datapoints
    matched = other_matched

    write_metrics_batch(datapoints)

    total_skipped = sum(other_skipped.values())
    if total_skipped:
        detail_parts = [
            f"{prov}:{svc}/{metric} x{n}" for (prov, svc, metric), n in sorted(other_skipped.items())
        ]
        logger.info(
            f"VM metrics sync (gcp only -- AWS + Azure now handled directly): "
            f"{matched} written, {total_skipped} skipped (no VM series yet) -- "
            f"{', '.join(detail_parts)}"
        )
    else:
        logger.info(f"VM metrics sync (gcp only -- AWS + Azure now handled directly): "
                     f"{matched} written, 0 skipped")

    return matched'''

MULTICLOUD_OLD = '''    try:
        azure_result = collect_all_azure_accounts()
        logger.info(
            f"[multicloud] Azure: {azure_result['accounts']} account(s), "
            f"{azure_result['pushed']} datapoints pushed"
            + (f", {len(azure_result['errors'])} account(s) had errors" if azure_result["errors"] else "")
        )
    except Exception as e:
        logger.error(f"[multicloud] Azure collection cycle crashed: {e}")'''

MULTICLOUD_NEW = '''    try:
        azure_result = collect_all_azure_accounts()
        logger.info(
            f"[multicloud] Azure: {azure_result['accounts']} account(s), "
            f"{azure_result['pushed']} datapoints written directly (Phase 2 -- no longer via VM)"
            + (f", {len(azure_result['errors'])} account(s) had errors" if azure_result["errors"] else "")
        )
    except Exception as e:
        logger.error(f"[multicloud] Azure collection cycle crashed: {e}")'''


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


def prepare_patch(path, label, replacements, done_marker):
    if not os.path.exists(path):
        die(f"{label} not found at {path}.")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    if done_marker in content:
        return None, f"{label} already patched -- skipping."
    new_content = content
    for old, new in replacements:
        n = new_content.count(old)
        if n != 1:
            die(f"{label}: expected exactly 1 match for one of the expected blocks, found {n}. "
                f"File may differ from what this script expects.")
        new_content = new_content.replace(old, new, 1)
    return new_content, f"{label}: OK ({len(new_content) - len(content):+d} bytes)"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply_ = args.apply and not args.dry_run

    repo_root = find_repo_root()
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    collector_path = os.path.join(repo_root, "app", "providers", "azure", "metrics_collector.py")
    vmsync_path = os.path.join(repo_root, "app", "collector", "metrics_vm_sync.py")
    multicloud_path = os.path.join(repo_root, "app", "collector", "multicloud_scheduler.py")

    results = []

    collector_content, collector_note = prepare_patch(
        collector_path, "app/providers/azure/metrics_collector.py",
        [
            (COLLECTOR_DOCSTRING_OLD, COLLECTOR_DOCSTRING_NEW),
            (COLLECTOR_IMPORTS_OLD, COLLECTOR_IMPORTS_NEW),
            (COLLECTOR_WRITE_OLD, COLLECTOR_WRITE_NEW),
        ],
        "write_metric_history_batch",
    )
    results.append((collector_path, "app/providers/azure/metrics_collector.py", collector_content, collector_note))

    vmsync_content, vmsync_note = prepare_patch(
        vmsync_path, "app/collector/metrics_vm_sync.py",
        [(VMSYNC_OLD, VMSYNC_NEW)],
        "Azure is intentionally NOT synced from VM here anymore",
    )
    results.append((vmsync_path, "app/collector/metrics_vm_sync.py", vmsync_content, vmsync_note))

    multicloud_content, multicloud_note = prepare_patch(
        multicloud_path, "app/collector/multicloud_scheduler.py",
        [(MULTICLOUD_OLD, MULTICLOUD_NEW)],
        "written directly (Phase 2 -- no longer via VM)",
    )
    results.append((multicloud_path, "app/collector/multicloud_scheduler.py", multicloud_content, multicloud_note))

    print("\nFile patch plan:")
    for _, _, _, note in results:
        print(f"  {note}")

    db_ok = verify_metric_history_exists(not apply_)
    if not db_ok:
        die("metric_history verification failed -- aborting before touching any files.")

    if all(content is None for _, _, content, _ in results):
        print("\nNothing to do -- everything this script would change is already applied.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    for path, label, content, note in results:
        if content is None:
            continue
        backup(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"Patched {label}")

    print("""
[Manual follow-up]

  A) Restart -- multicloud_scheduler.py's Azure branch now writes
     directly instead of pushing to VM:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 60 --no-pager

  B) Watch for these lines in the next multicloud cycle (~5 min):
       "[multicloud] Azure: N account(s), M datapoints written directly
        (Phase 2 -- no longer via VM)"
       "VM metrics sync (gcp only -- AWS + Azure now handled directly): ..."
       (confirms Azure is no longer being double-written from VM)

  C) Verify metric_history is accumulating Azure rows (Azure resources
     have resource_id values that look like ARM resource URIs, e.g.
     "/subscriptions/.../resourceGroups/..."):
       mysql -u monitor -p monitoring_hub -e \\
         "SELECT r.resource_type, COUNT(*), MIN(mh.metric_timestamp), MAX(mh.metric_timestamp)
          FROM metric_history mh JOIN resources r ON r.id = mh.resource_id
          JOIN aws_accounts a ON a.id = r.aws_account_id
          WHERE a.provider = 'azure' GROUP BY r.resource_type;"

  D) Confirm an Azure alert can actually fire now: pick a resource/metric
     with a configured threshold and check the `metrics` table has a
     fresh metric_timestamp for it, then watch alert_evaluator.py's log
     output on the next standard-tier cycle for that resource.

  E) Review, commit, push:
       git status
       git diff app/providers/azure/metrics_collector.py \\
                app/collector/metrics_vm_sync.py \\
                app/collector/multicloud_scheduler.py
       git add app/providers/azure/metrics_collector.py \\
               app/collector/metrics_vm_sync.py \\
               app/collector/multicloud_scheduler.py \\
               apply_azure_direct_metrics_fetch.py
       git commit -m "feat(metrics): Phase 2 of removing VictoriaMetrics -- direct Azure Monitor fetch, retargeting the existing collector instead of VM push"
       git push origin main

  Next: Phase 3 (GCP direct fetch -- same retarget approach likely
  applies, since app/providers/gcp/metrics_collector.py has the same
  shape as Azure's did), then Phase 4 (point dashboard charts at
  metric_history, retire vm_client.py and all remaining VM code).
""")


if __name__ == "__main__":
    main()
