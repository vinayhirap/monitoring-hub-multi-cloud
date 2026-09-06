#!/usr/bin/env python3
"""
fix_azure_gcp_alert_evaluation_gap.py
==========================================
Monitoring Hub -- Phase 5 of the provider-consistency audit, and the most
significant finding of the whole audit.

WHAT I WENT LOOKING FOR vs WHAT WAS ACTUALLY THERE
-----------------------------------------------------
The audit's original hypothesis (from the earlier session) was narrower:
"alert-evaluation logic might assume AWS's 2-minute critical-tier cadence
everywhere, making Azure/GCP alerts fire measurably slower." That turned
out to be a non-issue -- evaluate_alerts() is invoked exactly once per
"standard" (5-min) scheduler cycle regardless of provider, so its
CYCLE_MINUTES=5 constant is actually consistent for every alert, every
provider, all the time. No cadence bug there.

What tracing the real call chain found instead is much bigger:
app/collector/alert_evaluator.py's evaluate_alerts() reads exclusively
from the `metrics` MySQL table (a last-value cache -- see that module's
own docstring). The ONLY code that ever writes into that table is
app/collector/metrics_vm_sync.py's sync_metrics_from_vm(). And that
function's dimension-label map (_VM_DIM_LABEL) has exactly four entries:
ec2, ebs, rds, alb. Every other service key -- every single Azure and GCP
service key that exists anywhere in metric_catalog -- silently falls into
the `skipped_no_stub` bucket and is logged only as an aggregate count
alongside genuinely-not-yet-available AWS metrics, indistinguishable in
the logs from an expected gap.

The practical consequence: Azure and GCP metric VALUES do get collected
and pushed into VictoriaMetrics correctly (confirmed -- their collectors
in app/providers/{azure,gcp}/metrics_collector.py work fine and were
verified in this audit's earlier phases). But since evaluate_alerts()
never sees them (they never reach the `metrics` table it reads), NO
THRESHOLD HAS EVER BEEN ABLE TO FIRE for any Azure or GCP resource,
regardless of what's configured in Settings -> Metrics, regardless of how
severe a breach is. This is not a performance or fairness issue -- it is
a complete, silent failure of the single most important feature of a
monitoring product, for two of its three supported clouds. It has
presumably gone unnoticed because until this session's Phase 1/3 fixes,
Azure/GCP had very little enabled by default to begin with, and because
`skipped_no_stub` was designed for and reads exactly like an expected,
benign gap ("no VM series yet") rather than a hard failure.

ROOT CAUSE
----------
AWS's dimension-label convention (YACE's dimension_InstanceId etc.) is a
per-AWS-service-type label scheme. Azure/GCP's collectors push a plain
`resource_id` label instead (see app/providers/azure/metrics_collector.py
and app/providers/gcp/metrics_collector.py, both confirmed in this
audit's earlier phases) -- a single, uniform label for every service,
which is actually SIMPLER than AWS's per-service scheme. Nobody wired a
second lookup path for it when metrics_vm_sync.py was written during the
VM/YACE migration -- it was built AWS-first and the Azure/GCP half of the
migration was never finished on the read side, even though the write
side (metrics_collector.py, vm_write_batch) was.

FIX
---
Extends app/collector/metrics_vm_sync.py:
  1. _fetch_enabled_threshold_targets() now also selects mc.provider, so
     rows can be split by provider (previously implicit-AWS-only despite
     having no provider filter -- it happened to work because only AWS
     had a working sync path).
  2. sync_metrics_from_vm() now branches: AWS rows go through the
     EXACT SAME unchanged code path as before (zero behavior change for
     AWS -- same _VM_METRIC_STUB lookup, same dim_label scheme). Azure/GCP
     rows go through a new path that derives the VM metric name directly
     (f"{provider}_{service}_{slug(metric_name)}", matching exactly what
     the collectors already push) and queries with dim_label="resource_id"
     -- no stub table needed since, unlike YACE's AWS convention, this
     name is mechanically derivable from data already in metric_catalog.
  3. Both paths write into the same `metrics` table via the same
     write_metrics_batch(), so evaluate_alerts() needs ZERO changes --
     it already reads `metrics` generically with no provider filter.

TESTED (mocked, no live VM/DB available): AWS-only rows produce IDENTICAL
output to the pre-fix function (regression-safe); a mix of AWS+Azure+GCP
rows in one call correctly routes each to the right VM query convention;
an Azure/GCP metric with no live VM series yet is skipped and counted,
same "benign gap" semantics AWS already has; the datapoints written for
Azure/GCP carry the exact un-slugged metric_catalog.metric_name (not the
slugged VM series name) so evaluate_alerts()'s join keeps working
unchanged. NOT tested: an actual live sync against a real VictoriaMetrics
instance with real Azure/GCP data flowing through it -- verify on the dev
server by actually watching an alert fire for a real breach.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_azure_gcp_alert_evaluation_gap.py --dry-run
    python3 fix_azure_gcp_alert_evaluation_gap.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

OLD_FETCH = '''def _fetch_enabled_threshold_targets():
    """
    One row per (resource, metric) that has an enabled threshold.
    Resources come from the `resources` table -- populated by the
    discovery cycle, no AWS calls made here.
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT DISTINCT
                r.id             AS resource_db_id,
                r.resource_id    AS aws_resource_id,
                r.resource_type,
                mc.metric_name,
                mc.service,
                mc.statistic
            FROM thresholds t
            JOIN metric_catalog mc
                ON mc.id = t.metric_id
            JOIN resources r
                ON r.resource_type  = t.resource_type
               AND r.aws_account_id = t.aws_account_id
            WHERE t.enabled = 1
        """)
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()'''

NEW_FETCH = '''def _fetch_enabled_threshold_targets():
    """
    One row per (resource, metric) that has an enabled threshold, for
    EVERY provider -- this query itself was never AWS-specific, it just
    had no working sync path for anything but AWS until this fix. Now
    also selects mc.provider so sync_metrics_from_vm() can route each
    row through the right VM query convention.

    Resources come from the `resources` table -- populated by the
    discovery cycle, no cloud API calls made here.
    """
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT DISTINCT
                r.id             AS resource_db_id,
                r.resource_id    AS aws_resource_id,
                r.resource_type,
                mc.metric_name,
                mc.service,
                mc.statistic,
                mc.provider
            FROM thresholds t
            JOIN metric_catalog mc
                ON mc.id = t.metric_id
            JOIN resources r
                ON r.resource_type  = t.resource_type
               AND r.aws_account_id = t.aws_account_id
            WHERE t.enabled = 1
        """)
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()'''

OLD_SYNC = '''def sync_metrics_from_vm() -> int:
    """
    Populates `metrics` from VM for every enabled threshold's resources.
    Returns the number of datapoints written. Zero AWS API calls.
    """
    rows = _fetch_enabled_threshold_targets()
    if not rows:
        logger.info("VM metrics sync: no enabled thresholds -- nothing to do")
        return 0

    # Group by (service, metric_name) so each distinct metric gets exactly
    # ONE VM call (vm_query_all fetches every resource's value at once)
    # instead of one VM call per resource.
    by_metric = {}
    for row in rows:
        key = (row["service"], row["metric_name"])
        by_metric.setdefault(key, []).append(row)

    datapoints      = []   # (resource_db_id, metric_name, value)
    skipped_no_stub = {}   # (service, metric_name) -> resource count
    matched         = 0

    for (service, metric_name), resource_rows in by_metric.items():
        stub      = _VM_METRIC_STUB.get((service, metric_name))
        dim_label = _VM_DIM_LABEL.get(service)

        if not stub or not dim_label:
            skipped_no_stub[(service, metric_name)] = len(resource_rows)
            continue

        stat        = resource_rows[0]["statistic"] or "Average"
        yace_metric = stub if stub in _VM_NO_SUFFIX else f"{stub}_{_STAT_SUFFIX.get(stat, 'average')}"

        values = vm_query_all(yace_metric, dim_label)

        for row in resource_rows:
            val = values.get(row["aws_resource_id"])
            if val is not None:
                # metric_name here is metric_catalog's CamelCase form
                # (e.g. "CPUUtilization"), matching what evaluate_alerts()
                # joins against.
                datapoints.append((row["resource_db_id"], metric_name, val))
                matched += 1

    write_metrics_batch(datapoints)

    if skipped_no_stub:
        total_skipped = sum(skipped_no_stub.values())
        detail = ", ".join(
            f"{svc}/{metric} x{n}"
            for (svc, metric), n in sorted(skipped_no_stub.items())
        )
        logger.info(
            f"VM metrics sync: {matched} written, {total_skipped} skipped "
            f"(no VM series yet) -- {detail}"
        )
    else:
        logger.info(f"VM metrics sync: {matched} written, 0 skipped")

    return matched'''

NEW_SYNC = '''def _slug(name: str) -> str:
    """'Percentage CPU' -> 'percentage_cpu'. Matches EXACTLY the slug
    logic in app/providers/{azure,gcp}/metrics_collector.py's _slug() --
    must stay mirrored, since this has to reconstruct the same VM metric
    name those collectors already pushed under."""
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return s or "value"


def _sync_aws_metrics(rows) -> tuple[list, dict, int]:
    """
    Unchanged AWS sync logic, extracted as-is from the pre-fix
    sync_metrics_from_vm() so this fix makes zero behavior changes to the
    AWS path. Returns (datapoints, skipped_no_stub, matched).
    """
    by_metric = {}
    for row in rows:
        key = (row["service"], row["metric_name"])
        by_metric.setdefault(key, []).append(row)

    datapoints = []
    skipped_no_stub = {}
    matched = 0

    for (service, metric_name), resource_rows in by_metric.items():
        stub      = _VM_METRIC_STUB.get((service, metric_name))
        dim_label = _VM_DIM_LABEL.get(service)

        if not stub or not dim_label:
            skipped_no_stub[(service, metric_name)] = len(resource_rows)
            continue

        stat        = resource_rows[0]["statistic"] or "Average"
        yace_metric = stub if stub in _VM_NO_SUFFIX else f"{stub}_{_STAT_SUFFIX.get(stat, 'average')}"

        values = vm_query_all(yace_metric, dim_label)

        for row in resource_rows:
            val = values.get(row["aws_resource_id"])
            if val is not None:
                datapoints.append((row["resource_db_id"], metric_name, val))
                matched += 1

    return datapoints, skipped_no_stub, matched


def _sync_azure_gcp_metrics(rows) -> tuple[list, dict, int]:
    """
    Azure/GCP sync -- the actual fix. These collectors (see
    app/providers/{azure,gcp}/metrics_collector.py) push a plain
    `resource_id` label per datapoint, not AWS/YACE's per-service
    dimension_XxxId scheme, and the VM metric name is mechanically
    derivable (f"{provider}_{service}_{slug(metric_name)}") rather than
    needing a hand-curated stub table like AWS/YACE requires -- so this
    needs no equivalent of _VM_METRIC_STUB at all.

    Returns (datapoints, skipped_no_series, matched) -- same shape as
    _sync_aws_metrics so the caller can combine both uniformly.
    """
    by_metric = {}
    for row in rows:
        key = (row["provider"], row["service"], row["metric_name"])
        by_metric.setdefault(key, []).append(row)

    datapoints = []
    skipped_no_series = {}
    matched = 0

    for (provider, service, metric_name), resource_rows in by_metric.items():
        vm_metric = f"{provider}_{service}_{_slug(metric_name)}"
        values = vm_query_all(vm_metric, "resource_id")

        if not values:
            skipped_no_series[(provider, service, metric_name)] = len(resource_rows)
            continue

        for row in resource_rows:
            val = values.get(row["aws_resource_id"])
            if val is not None:
                # metric_name here is metric_catalog's exact stored name
                # (e.g. "Percentage CPU"), matching what evaluate_alerts()
                # joins against -- NOT the slugged VM series name above,
                # which is only used to know which series to query.
                datapoints.append((row["resource_db_id"], metric_name, val))
                matched += 1
            else:
                skipped_no_series.setdefault((provider, service, metric_name), 0)

    return datapoints, skipped_no_series, matched


def sync_metrics_from_vm() -> int:
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

    path = os.path.join(repo_root, "app", "collector", "metrics_vm_sync.py")
    if not os.path.exists(path):
        die(f"app/collector/metrics_vm_sync.py not found at {path}.")

    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    if "_sync_azure_gcp_metrics" in content:
        print("app/collector/metrics_vm_sync.py already has the Azure/GCP sync fix -- nothing to do.")
        return

    for old, label in [(OLD_FETCH, "_fetch_enabled_threshold_targets"), (OLD_SYNC, "sync_metrics_from_vm")]:
        n = content.count(old)
        if n != 1:
            die(f"{label}: expected exactly 1 match, found {n}. "
                f"File may differ from what this script expects.")

    new_content = content.replace(OLD_FETCH, NEW_FETCH, 1)
    new_content = new_content.replace(OLD_SYNC, NEW_SYNC, 1)

    print(f"\nPatch matched expected content exactly: app/collector/metrics_vm_sync.py "
          f"({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("Patched app/collector/metrics_vm_sync.py")

    print("""
[Manual follow-up -- please read all of this, this one matters more than the others]

  A) Restart is needed for the app to pick up the new function set:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 40 --no-pager

  B) Watch the very next "standard" cycle (every 5 min) in the log for a
     line like:
       VM metrics sync: N written, M skipped (no VM series yet) -- ...
     If any Azure/GCP accounts have enabled thresholds, you should now
     see their service/metric pairs among the WRITTEN count for the
     first time ever, not just AWS ones. If they still show up under
     "skipped", that means VictoriaMetrics doesn't have that exact series
     yet (e.g. the account hasn't collected that metric in the last
     lookback window) -- a genuinely different, much smaller problem than
     what this fix addresses, and worth checking
     app/providers/{azure,gcp}/metrics_collector.py's own logs for that
     account.

  C) The real test: pick one Azure or GCP resource with an enabled
     threshold, and either wait for a natural breach or temporarily set
     a threshold value guaranteed to be breached (e.g. CPU > 0%). Confirm
     an alert actually appears within one evaluation_period. This is the
     first time that will have ever worked for a non-AWS resource, so
     it's worth actually watching happen rather than trusting logs alone.

  D) Review, commit, push:
       git status
       git diff app/collector/metrics_vm_sync.py
       git add app/collector/metrics_vm_sync.py fix_azure_gcp_alert_evaluation_gap.py
       git commit -m "fix(alerts): Azure/GCP metrics were never synced into the alert-evaluation cache -- their thresholds could never fire"
       git push origin main

  This completes Phase 5 and the full multi-cloud provider-consistency
  audit. Recommend treating this one as the highest-priority deploy of
  everything from this audit, given the severity.
""")


if __name__ == "__main__":
    main()
