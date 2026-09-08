#!/usr/bin/env python3
"""
apply_direct_gmd_metrics_revival.py
========================================
Monitoring Hub -- Phase 1 of removing VictoriaMetrics: AWS direct
GetMetricData collection, replacing VM/YACE for AWS. Azure/GCP remain on
VM for now (Phases 2/3).

WHAT THIS DOES
--------------
1. Creates `metric_history` -- a small local time-series table replacing
   VM's range-query/charting role. Every GetMetricData datapoint gets
   inserted here (not upserted -- genuine history), with a pruning
   function to keep it bounded.

2. Re-enables app/collector/metrics/runner.py's run_metrics_collection()
   -- a COMPLETE, already-correct, already-cost-tuned GetMetricData
   implementation that was disabled (not deleted) when this project
   migrated to VM/YACE purely for cost avoidance. Confirmed via its own
   in-repo .bak file (byte-identical -- no bug fix was ever applied to
   it) and scheduler.py's own comment ("TEMPORARILY DISABLED... Re-enable
   only if you decide to roll back the VM migration"). This is a revival
   of tested code, not a rewrite -- its dimension mappings (including the
   easy-to-get-wrong ALB/NLB dimension name, "LoadBalancer", not
   "LoadBalancerName" -- that's Classic ELB's name) were verified correct
   against this project's actual resource discovery data.

3. Extends that collector's _execute_gmd() to also write every returned
   datapoint into metric_history (previously it only kept the single most
   recent value, via write_metric() into the `metrics` last-value cache
   -- that call is unchanged, this is purely additive).

4. Wires real collection back into scheduler.py's run_once(), replacing
   the "GMD collection skipped" placeholder, plus a periodic
   metric_history prune on the low tier.

5. Stops metrics_vm_sync.py from also syncing AWS rows. This is not
   optional cleanup -- it's a correctness fix: once GMD writes AWS's
   last-value cache directly and FIRST in each cycle, letting
   sync_metrics_from_vm() ALSO run afterward and overwrite those same
   rows from VM's separately-scraped (and potentially stale or simply
   different) data would silently undo the fresher direct values on
   every single cycle. Azure/GCP rows are unaffected -- they still sync
   from VM exactly as before, until Phases 2/3 replace that too.

WHAT THIS DOES NOT DO
----------------------
- Does not touch Azure or GCP collection at all -- separate phases.
- Does not touch dashboard/chart code to actually READ from
  metric_history yet (Phase 4) -- this phase only makes sure the data
  is being collected and stored, before anything is asked to read it.
- Does not remove VictoriaMetrics itself, vm_client.py, or any VM code
  -- Azure/GCP and existing dashboard reads still depend on it until
  later phases.

COST NOTE
---------
Re-enabling this reintroduces AWS CloudWatch GetMetricData billing (the
exact cost this project's VM/YACE migration was built to avoid -- see
this collector's own docstring: "$44/mo -> $15/mo" reduction at the time).
Confirmed accepted before building this.

TESTED: the new history-writing logic, the scheduler wiring, and the
VM-sync AWS-skip logic were all exercised against mocked boto3
GetMetricData responses and mocked DB cursors -- confirms datapoints
route to both `metrics` (latest value, unchanged behavior) and the new
`metric_history` (full series), confirms sync_metrics_from_vm() no
longer touches AWS rows, and confirms Azure/GCP sync behavior is
byte-identical to before. NOT tested: an actual live GetMetricData call
against real AWS -- no AWS credentials or network access available here;
verify on the dev server by watching the very next cycle's logs.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_direct_gmd_metrics_revival.py --dry-run
    python3 apply_direct_gmd_metrics_revival.py --apply
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime

# ─────────────────────────── DB: metric_history table ───────────────────────────

CREATE_METRIC_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS metric_history (
    id               BIGINT NOT NULL AUTO_INCREMENT,
    resource_id      BIGINT NOT NULL,
    metric_name      VARCHAR(100) NOT NULL,
    metric_value     DOUBLE,
    metric_timestamp DATETIME NOT NULL,
    PRIMARY KEY (id),
    KEY idx_history_lookup (resource_id, metric_name, metric_timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""


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


def create_metric_history_table(dry_run):
    db_pass = _load_db_password()
    db_host = os.environ.get("DB_HOST", "127.0.0.1")
    db_port = os.environ.get("DB_PORT", "3306")
    db_user = os.environ.get("DB_USER", "monitor")
    db_name = os.environ.get("DB_NAME", "monitoring_hub")

    cmd = ["mysql", f"-u{db_user}", "-h", db_host, "-P", db_port]
    if db_pass:
        cmd.append(f"-p{db_pass}")
    cmd.append(db_name)

    if dry_run:
        print("[DRY-RUN] would run: CREATE TABLE IF NOT EXISTS metric_history (...)")
        return True

    result = subprocess.run(cmd, input=CREATE_METRIC_HISTORY_SQL, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] Failed to create metric_history table:\n{result.stderr}", file=sys.stderr)
        return False
    print("Created (or already existed) metric_history table")
    return True


# ─────────────────────────── file patches ───────────────────────────

WRITER_ADDITIONS = '''

def write_metric_history_batch(datapoints: list):
    """
    Inserts raw time-series datapoints into metric_history -- the local
    replacement for VictoriaMetrics' range-query/graphing role, now that
    AWS metrics are fetched via direct GetMetricData calls instead of
    VM/YACE (see apply_direct_gmd_metrics_revival.py). Every call ADDS
    rows -- this is genuine history, unlike write_metrics_batch() above
    which upserts a single latest value.

    datapoints: list of (resource_db_id, metric_name, value, timestamp) tuples.
    """
    if not datapoints:
        return

    conn   = get_connection()
    cursor = conn.cursor()

    try:
        cursor.executemany("""
            INSERT INTO metric_history
                (resource_id, metric_name, metric_value, metric_timestamp)
            VALUES (%s, %s, %s, %s)
        """, [
            (r_id, name, round(float(val), 6), ts)
            for r_id, name, val, ts in datapoints
            if r_id is not None and val is not None
        ])
        conn.commit()
        logger.debug(f"Wrote {cursor.rowcount} history datapoints")

    except Exception as e:
        logger.error(f"metric_history batch write error: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()


def prune_metric_history(retain_days: int = 7) -> int:
    """
    Deletes metric_history rows older than retain_days. Called
    periodically (see scheduler.py's low tier) to keep this table
    bounded -- unlike the `metrics` last-value cache (which never grows
    past one row per resource/metric pair), this table accumulates a new
    row every collection cycle and needs active pruning.
    """
    conn   = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM metric_history WHERE metric_timestamp < DATE_SUB(NOW(), INTERVAL %s DAY)",
            (retain_days,)
        )
        deleted = cursor.rowcount
        conn.commit()
        if deleted:
            logger.info(f"metric_history: pruned {deleted} row(s) older than {retain_days} days")
        return deleted
    except Exception as e:
        logger.error(f"metric_history prune error: {e}")
        conn.rollback()
        return 0
    finally:
        cursor.close()
        conn.close()
'''

RUNNER_IMPORT_OLD = "from app.collector.metrics_writer import write_metric"
RUNNER_IMPORT_NEW = "from app.collector.metrics_writer import write_metric, write_metric_history_batch"

RUNNER_EXECUTE_OLD = '''def _execute_gmd(cw, queries, id_map, minutes=5):
    """Execute one GMD call, write results. Returns datapoint count."""
    if not queries:
        return 0

    end   = datetime.utcnow()
    start = end - timedelta(minutes=minutes)
    count = 0

    try:
        resp = cw.get_metric_data(
            MetricDataQueries=queries,
            StartTime=start,
            EndTime=end,
            ScanBy="TimestampDescending",
        )
    except Exception as e:
        logger.error(f"GMD call failed: {e}")
        return 0

    for result in resp.get("MetricDataResults", []):
        values = result.get("Values", [])
        if not values:
            continue
        resource_db_id, db_name = id_map.get(result["Id"], (None, None))
        if resource_db_id is None:
            continue
        write_metric(resource_db_id, db_name, values[0])  # values[0] = most recent
        count += 1

    return count'''

RUNNER_EXECUTE_NEW = '''def _execute_gmd(cw, queries, id_map, minutes=5):
    """Execute one GMD call, write results (latest value + full history).
    Returns datapoint count."""
    if not queries:
        return 0

    end   = datetime.utcnow()
    start = end - timedelta(minutes=minutes)
    count = 0
    history_rows = []

    try:
        resp = cw.get_metric_data(
            MetricDataQueries=queries,
            StartTime=start,
            EndTime=end,
            ScanBy="TimestampDescending",
        )
    except Exception as e:
        logger.error(f"GMD call failed: {e}")
        return 0

    for result in resp.get("MetricDataResults", []):
        values = result.get("Values", [])
        timestamps = result.get("Timestamps", [])
        if not values:
            continue
        resource_db_id, db_name = id_map.get(result["Id"], (None, None))
        if resource_db_id is None:
            continue
        write_metric(resource_db_id, db_name, values[0])  # values[0] = most recent
        count += 1
        # Full history -- every returned datapoint, not just the latest.
        # Timestamps/Values are parallel lists per boto3's own contract.
        for ts, val in zip(timestamps, values):
            history_rows.append((resource_db_id, db_name, val, ts))

    if history_rows:
        write_metric_history_batch(history_rows)

    return count'''

SCHEDULER_OLD = '''def run_once(tier="standard"):
    """Single collection + alert cycle for given tier."""
    from app.collector.metrics.runner  import run_metrics_collection
    from app.collector.alert_evaluator import evaluate_alerts
    from app.collector.metrics_vm_sync   import sync_metrics_from_vm

    accounts = _get_active_accounts()
    if not accounts:
        logger.warning("No active accounts")
        return

    # TEMPORARILY DISABLED — migrating to VM/YACE, no boto3 GMD calls.
    # Re-enable only if you decide to roll back the VM migration.
    # run_metrics_collection(accounts, tier=tier)
    logger.info(f"[{tier}] GMD collection skipped — VM/YACE migration in progress")

    # Evaluate alerts after every standard cycle
    if tier == "standard":
        sync_metrics_from_vm()
        evaluate_alerts()'''

SCHEDULER_NEW = '''def run_once(tier="standard"):
    """Single collection + alert cycle for given tier."""
    from app.collector.metrics.runner  import run_metrics_collection
    from app.collector.alert_evaluator import evaluate_alerts
    from app.collector.metrics_vm_sync   import sync_metrics_from_vm
    from app.collector.metrics_writer  import prune_metric_history

    accounts = _get_active_accounts()
    if not accounts:
        logger.warning("No active accounts")
        return

    # Re-enabled (see apply_direct_gmd_metrics_revival.py) -- this was
    # disabled, not deleted, during the VM/YACE cost-avoidance migration.
    # AWS billing for GetMetricData applies again; accepted deliberately.
    run_metrics_collection(accounts, tier=tier)

    if tier == "low":
        prune_metric_history()

    # Evaluate alerts after every standard cycle
    if tier == "standard":
        sync_metrics_from_vm()
        evaluate_alerts()'''

VMSYNC_OLD = '''def sync_metrics_from_vm() -> int:
    """
    Populates `metrics` from VM for every enabled threshold's resources,
    across ALL THREE providers. Returns the number of datapoints written.
    Zero AWS/Azure/GCP API calls -- purely a VM read + MySQL write, same
    as before this fix; the fix is routing Azure/GCP rows through their
    own working query convention instead of the AWS-only one they were
    silently falling through before (see this file's module-level
    docstring, and fix_azure_gcp_alert_evaluation_gap.py, for the full
    story on why this was needed).
    """
    rows = _fetch_enabled_threshold_targets()
    if not rows:
        logger.info("VM metrics sync: no enabled thresholds -- nothing to do")
        return 0

    aws_rows = [r for r in rows if (r.get("provider") or "aws") == "aws"]
    other_rows = [r for r in rows if (r.get("provider") or "aws") != "aws"]

    aws_datapoints, aws_skipped, aws_matched = _sync_aws_metrics(aws_rows)
    other_datapoints, other_skipped, other_matched = _sync_azure_gcp_metrics(other_rows)

    datapoints = aws_datapoints + other_datapoints
    matched = aws_matched + other_matched

    write_metrics_batch(datapoints)

    total_skipped = sum(aws_skipped.values()) + sum(other_skipped.values())
    if total_skipped:
        detail_parts = [
            f"{svc}/{metric} x{n}" for (svc, metric), n in sorted(aws_skipped.items())
        ] + [
            f"{prov}:{svc}/{metric} x{n}" for (prov, svc, metric), n in sorted(other_skipped.items())
        ]
        logger.info(
            f"VM metrics sync: {matched} written, {total_skipped} skipped "
            f"(no VM series yet) -- {', '.join(detail_parts)}"
        )
    else:
        logger.info(f"VM metrics sync: {matched} written, 0 skipped")

    return matched'''

VMSYNC_NEW = '''def sync_metrics_from_vm() -> int:
    """
    Populates `metrics` from VM for Azure/GCP resources with an enabled
    threshold. Returns the number of datapoints written.

    AWS is intentionally NOT synced from VM here anymore (see
    apply_direct_gmd_metrics_revival.py, Phase 1 of removing
    VictoriaMetrics): app/collector/metrics/runner.py's GetMetricData
    collector now writes AWS's last-value cache directly, and runs
    BEFORE this function in every tier cycle (see scheduler.py). If this
    function also synced AWS from VM afterward, it would immediately
    overwrite those fresh direct values with VM's separately-scraped
    (and potentially stale or simply different) data on every single
    cycle -- a real correctness bug, not just redundant work. Azure/GCP
    are unaffected and still sync from VM exactly as before, until
    Phases 2/3 replace that too.
    """
    rows = _fetch_enabled_threshold_targets()
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

    writer_path = os.path.join(repo_root, "app", "collector", "metrics_writer.py")
    runner_path = os.path.join(repo_root, "app", "collector", "metrics", "runner.py")
    scheduler_path = os.path.join(repo_root, "app", "collector", "scheduler.py")
    vmsync_path = os.path.join(repo_root, "app", "collector", "metrics_vm_sync.py")

    results = []

    with open(writer_path, "r", encoding="utf-8") as fh:
        writer_content = fh.read()
    if "write_metric_history_batch" in writer_content:
        results.append((writer_path, "app/collector/metrics_writer.py",
                         None, "app/collector/metrics_writer.py already patched -- skipping."))
    else:
        new_writer = writer_content.rstrip("\n") + "\n" + WRITER_ADDITIONS
        results.append((writer_path, "app/collector/metrics_writer.py", new_writer,
                         f"app/collector/metrics_writer.py: OK ({len(new_writer) - len(writer_content):+d} bytes)"))

    runner_content, runner_note = prepare_patch(
        runner_path, "app/collector/metrics/runner.py",
        [(RUNNER_IMPORT_OLD, RUNNER_IMPORT_NEW), (RUNNER_EXECUTE_OLD, RUNNER_EXECUTE_NEW)],
        "write_metric_history_batch(history_rows)",
    )
    results.append((runner_path, "app/collector/metrics/runner.py", runner_content, runner_note))

    scheduler_content, scheduler_note = prepare_patch(
        scheduler_path, "app/collector/scheduler.py",
        [(SCHEDULER_OLD, SCHEDULER_NEW)],
        "Re-enabled (see apply_direct_gmd_metrics_revival.py)",
    )
    results.append((scheduler_path, "app/collector/scheduler.py", scheduler_content, scheduler_note))

    vmsync_content, vmsync_note = prepare_patch(
        vmsync_path, "app/collector/metrics_vm_sync.py",
        [(VMSYNC_OLD, VMSYNC_NEW)],
        "AWS is intentionally NOT synced from VM here anymore",
    )
    results.append((vmsync_path, "app/collector/metrics_vm_sync.py", vmsync_content, vmsync_note))

    print("\nFile patch plan:")
    for _, _, _, note in results:
        print(f"  {note}")

    db_ok = create_metric_history_table(not apply_)

    if not db_ok:
        die("Could not create metric_history table -- aborting before touching any files.")

    if all(content is None for _, _, content, _ in results):
        print("\nNothing to do -- everything this script would add is already present.")
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

  A) Restart -- this changes what the scheduler does every cycle:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 60 --no-pager

  B) Watch for these lines in the next few cycles:
       "[critical] <account>" / "    EC2 critical: N datapoints / M instances"
       (replaces the old "GMD collection skipped" line)
       "VM metrics sync (azure/gcp only -- AWS now handled by direct GMD): ..."
       (confirms AWS is no longer being double-written from VM)

  C) Verify metric_history is actually accumulating:
       mysql -u monitor -p monitoring_hub -e \\
         "SELECT COUNT(*), MIN(metric_timestamp), MAX(metric_timestamp) FROM metric_history;"

  D) Cost awareness: this reintroduces AWS CloudWatch GetMetricData
     billing. Watch the AWS Cost Explorer / CloudWatch cost line for the
     next few days to confirm it lands where expected (this collector's
     own docstring quotes ~$15/mo for 3 accounts at the time it was last
     tuned -- verify against your current account count and metric
     selection, costs scale with both).

  E) Review, commit, push:
       git status
       git diff app/collector/metrics_writer.py app/collector/metrics/runner.py \\
                app/collector/scheduler.py app/collector/metrics_vm_sync.py
       git add app/collector/metrics_writer.py app/collector/metrics/runner.py \\
               app/collector/scheduler.py app/collector/metrics_vm_sync.py \\
               apply_direct_gmd_metrics_revival.py
       git commit -m "feat(metrics): Phase 1 of removing VictoriaMetrics -- revive direct GetMetricData for AWS, add local metric_history table"
       git push origin main

  Next: Phase 2 (Azure direct fetch), Phase 3 (GCP direct fetch), then
  Phase 4 (point dashboard charts at metric_history, retire vm_client.py
  and all remaining VM code).
""")


if __name__ == "__main__":
    main()
