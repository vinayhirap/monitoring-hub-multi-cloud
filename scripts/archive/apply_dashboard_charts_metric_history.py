#!/usr/bin/env python3
"""
apply_dashboard_charts_metric_history.py
========================================
Phase 4a of removing VictoriaMetrics: retarget the 6 per-resource chart
"detail page" endpoints (EC2/EBS/RDS/Lambda/ELB/ECS series) from VM's
query_range to the local metric_history table Phase 1-3 already write
into. S3 is untouched -- its chart function was already boto3-only, no
VM dependency to remove.

SCOPE, HONESTLY STATED
------------------------
This is Phase 4a, not all of Phase 4. Investigating app/aws/collector_direct.py
found MORE VM read call sites than HANDOVER.md's one-line Phase 4
description implied:
  - 6 per-resource chart-series functions (get_ec2_metric_series,
    _get_ebs_metric_series, _get_rds_metric_series,
    _get_lambda_metric_series_raw, _get_elb_metric_series,
    _get_ecs_metric_series) -- called by the 6 /api/live/metrics/*
    detail-page chart endpoints in app/api/live_data.py. THIS script
    retargets these 6.
  - Several LIST-view snapshot functions (_ec2_raw, _ebs_raw, and the
    same shape likely repeated for RDS/Lambda/ELB list views further
    down the file) using vm_query_all for "every resource's current
    value in one call" -- used by the account-level resource list
    pages, not the detail-page charts. NOT touched by this script --
    a separate Phase 4b, same shape of work, not done here.
  - app/aws/cloudwatch.py's single vm_query call -- confirmed dead code
    (its only caller, app/collector/ec2_cpu_collector.py, is itself
    never imported anywhere). Left alone; harmless, and touching dead
    code isn't worth the risk here.
vm_client.py itself is NOT removed by this script -- it's still needed
by Phase 4b's not-yet-converted call sites and by GCP's remaining VM
dependency for anything Phase 3 didn't cover. Once Phase 4b is done,
vm_client.py's reachability can be re-checked and retired for real.

RESOURCE MATCHING
-------------------
Two different matching conventions, confirmed against
app/collector/discovery/runner.py's actual _upsert_resource() calls
(not guessed):
  - EC2 (resource_type='ec2'), EBS ('ebs'), RDS ('rds'): resources.resource_id
    IS the bare identifier the chart endpoint's URL param already is
    (instance_id / volume_id / db_id) -- match on resource_id directly.
  - Lambda ('lambda'), ELB ('elb'), ECS ('ecs'): resources.resource_id is
    the full ARN (FunctionArn / LoadBalancerArn / clusterArn), but the
    chart endpoint's URL param is the bare NAME (function_name / lb_name /
    cluster_name), which matches resources.name instead -- match on name.
  This is the exact same distinction app/collector/metrics/runner.py's
  own _resource_dim_value()/_DIM_NAME already draw for CloudWatch
  dimension purposes -- confirmed by reading that code, not assumed.

METRIC NAME MAPPING
----------------------
db_metric_name strings below are copied verbatim from
app/collector/metrics/runner.py's own EC2_METRICS_CRITICAL/LOW,
EBS_METRICS, RDS_METRICS, ELB_METRICS, LAMBDA_METRICS_STANDARD/LOW
tuples -- i.e. exactly what Phase 1's GMD collector actually writes into
metric_history, not a guess at a naming convention.

A KNOWN, DOCUMENTED REGRESSION -- NOT SILENTLY PAPERED OVER
--------------------------------------------------------------
Phase 1's GMD collector deliberately trimmed several metrics per cost
triage (see its own docstring): EBS BurstBalance ("gp3 irrelevant"),
Lambda ConcurrentExecutions, ELB's 4XX/ELB-5XX/UnHealthyHostCount/
ActiveConnectionCount/NewConnectionCount, and ALL of AWS/ECS CPU/Memory
(free-tier metrics, excluded from paid GMD entirely). VictoriaMetrics
(via YACE, if it was actually deployed and scraping) may have had real
data for some of these that GMD/metric_history never will.
  - For Lambda, ELB, and ECS: SAFE, because all three chart functions
    already have automatic boto3-fallback-on-empty logic built in
    (`missing = [k for k, v in result.items() if not v]` / `if not cpu
    or not mem`) -- these metrics will now ALWAYS take that fallback
    path instead of sometimes finding VM data, which is a real cost
    trade (a live boto3 call every time a chart is opened, instead of a
    free VM read that may or may not have had data) but not a
    correctness regression -- the chart still populates.
  - For EBS burst_balance specifically: NOT safe in the same way --
    _get_ebs_metric_series has NO fallback logic at all (unlike the
    other 5), so this chart series will now ALWAYS be an empty array,
    permanently, until burst_balance is either added to Phase 1's GMD
    collection or _get_ebs_metric_series gets its own boto3 fallback
    added. This is a real, if minor (gp3-irrelevant per Phase 1's own
    triage note), degradation from whatever VM may have had. Flagged
    here rather than fixed, since fixing it means expanding Phase 1's
    metric set, which is a cost decision this script shouldn't make
    unilaterally.
Every retargeted call site degrades to an empty array on no match or no
data, never raises -- the exact same failure contract vm_query_range
already had (it also caught all exceptions and returned []).

TESTED: the new _metric_history_query_range() helper and both matching
conventions (resource_id-based and name-based) were exercised with a
mocked DB cursor returning realistic resources/metric_history rows,
confirming correct SQL, correct resource resolution for both match
types, correct handling of a no-match case (empty list, not an
exception), and byte-identical output shape to vm_query_range's old
contract ([{"t": iso, "v": rounded_float}, ...], oldest to newest). NOT
tested: an actual live chart request against real data -- no server/DB
access available here; verify per the checklist this script prints.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_dashboard_charts_metric_history.py --dry-run
    python3 apply_dashboard_charts_metric_history.py --apply
(no root needed, no DB touch by this script itself -- pure repo file
edit; the new code it adds does read the DB at request time, same as
the vm_query_range calls it replaces did)
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

HEADER_OLD = '''# app/aws/collector_direct.py
"""
Live data collector for frontend detail pages.

MIGRATION STATE (cost-optimization pass):
  - EC2 / EBS / RDS metrics: VM-backed (fed by YACE), no boto3.
  - ALB: all 8 metrics are VM-first with automatic per-metric boto3 fallback
    (_get_elb_metric_series) — the 2 metrics YACE previously never scraped
    (HTTPCode_ELB_5XX_Count, NewConnectionCount) now work the same way once
    you enable them in the Metric Catalog and redeploy the YACE config.
  - ECS: AWS/ECS CPUUtilization/MemoryUtilization are VM-first with boto3
    fallback; ECS/ContainerInsights task-count fields stay boto3-only
    (metric-name convention for that namespace not yet verified live).
  - Lambda: VM-first with boto3 fallback (metric-name convention derived
    from the confirmed EC2/RDS/ALB pattern but not yet verified live —
    check `curl $VM_URL/api/v1/label/__name__/values | grep aws_lambda`
    after first deploy).
  - S3: still boto3-only (StorageType-dimensioned metrics need YACE storage-
    type config which hasn't been set up; low call volume already since
    this is only hit on a per-bucket detail-page click, not a poll loop).
  - EC2 StatusCheckFailed (used only by check_and_write_alerts, below) now
    reads from the FREE Describe-API path (app/aws/describe_polling.py)
    instead of CloudWatch/YACE — zero GetMetricData cost, sub-second fresh.

Every VM-first function above falls back to boto3 automatically per-metric
if VM has no data yet, so all of this is safe to ship before the
corresponding YACE config is actually deployed — cost drops to zero for a
given metric only once VM genuinely has fresh data for it.

Two GMD helpers (unchanged, still used for the boto3 fallback paths):
  _gmd_snapshot(cw, queries)  — latest single value per metric (for list views)
  _gmd_series(cw, queries)    — time-series arrays (for chart/detail views)
"""
import boto3, logging, time, math
from datetime import datetime, timedelta, timezone
from app.clients.vm_client import vm_query, vm_query_all, vm_query_range

logger = logging.getLogger(__name__)'''

HEADER_NEW = '''# app/aws/collector_direct.py
"""
Live data collector for frontend detail pages.

MIGRATION STATE (Phase 4a of removing VictoriaMetrics -- see
apply_dashboard_charts_metric_history.py):
  - EC2 / EBS / RDS / Lambda / ELB / ECS chart-detail series (the 6
    get_*/_get_*_metric_series functions) now read from the local
    metric_history table (written by Phase 1's GMD collector), not VM.
    Lambda/ELB/ECS keep their existing automatic boto3 fallback for
    metrics Phase 1 doesn't collect (ConcurrentExecutions, several ELB
    fields, all of ECS) -- see apply_dashboard_charts_metric_history.py's
    docstring for the one real gap this created (EBS burst_balance has
    no fallback and is now permanently empty).
  - LIST-view snapshot functions (_ec2_raw, _ebs_raw, etc. -- "every
    resource's current value in one call") still read from VM via
    vm_query_all. NOT yet converted -- Phase 4b, still open.
  - S3: still boto3-only, unrelated to VM either way.
  - EC2 StatusCheckFailed (used only by check_and_write_alerts, below)
    reads from the FREE Describe-API path (app/aws/describe_polling.py)
    instead of CloudWatch — zero GetMetricData cost, sub-second fresh.

Two GMD helpers (unchanged, still used for the boto3 fallback paths):
  _gmd_snapshot(cw, queries)  — latest single value per metric (for list views)
  _gmd_series(cw, queries)    — time-series arrays (for chart/detail views)
"""
import boto3, logging, time, math
from datetime import datetime, timedelta, timezone
from app.clients.vm_client import vm_query, vm_query_all
from app.db import get_connection

logger = logging.getLogger(__name__)


def _metric_history_query_range(resource_type, identifier, db_metric_name,
                                 start_dt, end_dt, match_field="resource_id"):
    """
    Drop-in replacement for vm_client.vm_query_range's role in the 6
    chart-series functions below. Reads app/collector/metrics/runner.py's
    (Phase 1) local metric_history table instead of VictoriaMetrics.
    Returns the SAME shape vm_query_range did:
      [{"t": iso_timestamp, "v": rounded_float}, ...] oldest -> newest.
    Returns [] on no matching resource, no data in range, or any error --
    same never-raises, degrade-to-empty contract vm_query_range already
    had. match_field is always one of the two literal strings this file
    passes in below ("resource_id" or "name"), never user input.
    """
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                f"SELECT id FROM resources WHERE resource_type = %s AND {match_field} = %s LIMIT 1",
                (resource_type, identifier),
            )
            row = cur.fetchone()
            if not row:
                return []
            cur.execute(
                """SELECT metric_value, metric_timestamp FROM metric_history
                   WHERE resource_id = %s AND metric_name = %s
                         AND metric_timestamp BETWEEN %s AND %s
                   ORDER BY metric_timestamp""",
                (row["id"], db_metric_name, start_dt, end_dt),
            )
            rows = cur.fetchall()
        finally:
            cur.close()
            conn.close()
        return [
            {"t": r["metric_timestamp"].isoformat(), "v": round(float(r["metric_value"]), 2)}
            for r in rows if r["metric_value"] is not None
        ]
    except Exception as e:
        logger.warning(f"metric_history query_range failed [{resource_type}/{identifier}/{db_metric_name}]: {e}")
        return []'''

EC2_OLD = '''        def s(yace_metric):
            return vm_query_range(
                f'{yace_metric}{{{dim}}}',
                start=int(start.timestamp()), end=int(end.timestamp()),
                step=f"{period}s",
            )

        cwagent_installed = _ec2_cwagent_installed(instance_id, region)'''

EC2_NEW = '''        def s(db_metric_name):
            return _metric_history_query_range("ec2", instance_id, db_metric_name, start, end)

        cwagent_installed = _ec2_cwagent_installed(instance_id, region)'''

EC2_FIELDS_OLD = '''            "cpu":               s("aws_ec2_cpuutilization_average"),
            "network_in":        s("aws_ec2_network_in_average"),
            "network_out":       s("aws_ec2_network_out_average"),
            "disk_read":         s("aws_ec2_disk_read_bytes_sum"),
            "disk_write":        s("aws_ec2_disk_write_bytes_sum"),'''

EC2_FIELDS_NEW = '''            "cpu":               s("cpuutilization"),
            "network_in":        s("networkin"),
            "network_out":       s("networkout"),
            "disk_read":         s("diskreadbytes"),
            "disk_write":        s("diskwritebytes"),'''

EBS_OLD = '''        def s(yace_metric):
            return vm_query_range(
                f'{yace_metric}{{{dim}}}',
                start=int(start.timestamp()), end=int(end.timestamp()),
                step=f"{period}s",
            )
        return {
            "volume_id":    volume_id,
            "read_ops":     s("aws_ebs_volume_read_ops_average"),
            "write_ops":    s("aws_ebs_volume_write_ops_average"),
            "read_bytes":   s("aws_ebs_volume_read_bytes_average"),
            "write_bytes":  s("aws_ebs_volume_write_bytes_average"),
            "queue_length": s("aws_ebs_volume_queue_length_average"),
            "burst_balance": s("aws_ebs_burst_balance_average"),
            "period_hours": hours,
            "period_secs":  period,
        }'''

EBS_NEW = '''        def s(db_metric_name):
            return _metric_history_query_range("ebs", volume_id, db_metric_name, start, end)
        return {
            "volume_id":    volume_id,
            "read_ops":     s("volumereadops"),
            "write_ops":    s("volumewriteops"),
            "read_bytes":   s("volumereadbytes"),
            "write_bytes":  s("volumewritebytes"),
            "queue_length": s("volumequeuelength"),
            # burst_balance: Phase 1's GMD collector deliberately dropped
            # BurstBalance ("gp3 irrelevant" per its own triage note), so
            # metric_history never has this metric_name and this call
            # always returns []. Unlike the other 5 functions in this
            # file, this one has no boto3 fallback -- this chart series
            # is now PERMANENTLY EMPTY. See apply_dashboard_charts_metric_history.py's
            # docstring: a known, documented trade, not fixed here.
            "burst_balance": s("volumeburstbalance"),
            "period_hours": hours,
            "period_secs":  period,
        }'''

LAMBDA_OLD = '''        def vm_series(yace_metric):
            return vm_query_range(
                f'{yace_metric}{{{dim}}}',
                start=int(start.timestamp()), end=int(end.timestamp()),
                step=f"{period}s",
            )

        result = {
            "invocations": vm_series("aws_lambda_invocations_sum"),
            "errors":      vm_series("aws_lambda_errors_sum"),
            "duration":    vm_series("aws_lambda_duration_average"),
            "throttles":   vm_series("aws_lambda_throttles_sum"),
            "concurrent":  vm_series("aws_lambda_concurrent_executions_average"),
        }'''

LAMBDA_NEW = '''        def vm_series(db_metric_name):
            return _metric_history_query_range("lambda", function_name, db_metric_name,
                                                start, end, match_field="name")

        result = {
            "invocations": vm_series("invocations"),
            "errors":      vm_series("errors"),
            "duration":    vm_series("duration"),
            # concurrent: Phase 1's GMD collector never collects
            # ConcurrentExecutions -- metric_history never has it, so this
            # always returns [] and correctly falls through to the boto3
            # fallback below every time (safe: this function already has
            # per-metric fallback logic, unlike EBS burst_balance).
            "concurrent":  vm_series("concurrentexecutions"),
            "throttles":   vm_series("throttles"),
        }'''

RDS_OLD = '''        def s(yace_metric):
            return vm_query_range(
                f'{yace_metric}{{{dim}}}',
                start=int(start.timestamp()), end=int(end.timestamp()),
                step=f"{period}s",
            )
        return {
            "db_id":           db_id,
            "cpu":             s("aws_rds_cpuutilization_average"),
            "free_storage":    s("aws_rds_free_storage_space_average"),
            "db_connections":  s("aws_rds_database_connections_average"),
            "read_iops":       s("aws_rds_read_iops_average"),
            "write_iops":      s("aws_rds_write_iops_average"),
            "read_latency":    s("aws_rds_read_latency_average"),
            "write_latency":   s("aws_rds_write_latency_average"),
            "freeable_memory": s("aws_rds_freeable_memory_average"),
            "period_hours":    hours,
            "period_secs":     period,
        }'''

RDS_NEW = '''        def s(db_metric_name):
            return _metric_history_query_range("rds", db_id, db_metric_name, start, end)
        return {
            "db_id":           db_id,
            "cpu":             s("cpuutilization"),
            "free_storage":    s("freestorage"),
            "db_connections":  s("dbconnections"),
            "read_iops":       s("readiops"),
            "write_iops":      s("writeiops"),
            "read_latency":    s("readlatency"),
            "write_latency":   s("writelatency"),
            "freeable_memory": s("freeablememory"),
            "period_hours":    hours,
            "period_secs":     period,
        }'''

ELB_OLD = '''        def vm_series(yace_metric):
            return vm_query_range(
                f'{yace_metric}{{{dim}}}',
                start=int(start.timestamp()), end=int(end.timestamp()),
                step=f"{period}s",
            )

        result = {
            "requests":           vm_series("aws_applicationelb_request_count_sum"),
            "errors_5xx":         vm_series("aws_applicationelb_httpcode_target_5_xx_count_sum"),
            "errors_4xx":         vm_series("aws_applicationelb_httpcode_target_4_xx_count_sum"),
            "errors_elb_5xx":     vm_series("aws_applicationelb_httpcode_elb_5_xx_count_sum"),
            "latency":            vm_series("aws_applicationelb_target_response_time_average"),
            "healthy_hosts":      vm_series("aws_applicationelb_healthy_host_count_average"),
            "unhealthy_hosts":    vm_series("aws_applicationelb_un_healthy_host_count_average"),
            "active_connections": vm_series("aws_applicationelb_active_connection_count_average"),
            "new_connections":    vm_series("aws_applicationelb_new_connection_count_sum"),
        }'''

ELB_NEW = '''        # Match on the ORIGINAL bare lb_name param (== resources.name),
        # NOT lb_dim (the ARN-suffix computed above for the CloudWatch
        # fallback dimension) -- resource_discovery stores resources.name
        # as the bare LoadBalancerName, confirmed against
        # app/collector/discovery/runner.py's _discover_elb().
        def vm_series(db_metric_name):
            return _metric_history_query_range("elb", lb_name, db_metric_name,
                                                start, end, match_field="name")

        # requests/errors_5xx/latency/healthy_hosts: Phase 1's GMD collector
        # covers these (ELB_METRICS). The other 5 keys were deliberately
        # excluded from Phase 1 (4XX/ELB-5XX/UnHealthyHostCount dropped as
        # "client noise"/"redundant" per its own triage note;
        # ActiveConnectionCount/NewConnectionCount were never in the YACE
        # config either) -- metric_history never has them, so they always
        # return [] and correctly fall through to the boto3 fallback below
        # every time. Safe: this function already had per-metric fallback
        # logic for exactly this situation.
        result = {
            "requests":           vm_series("requestcount"),
            "errors_5xx":         vm_series("errors5xx"),
            "errors_4xx":         vm_series("errors4xx"),
            "errors_elb_5xx":     vm_series("errorselb5xx"),
            "latency":            vm_series("responselatency"),
            "healthy_hosts":      vm_series("healthyhosts"),
            "unhealthy_hosts":    vm_series("unhealthyhosts"),
            "active_connections": vm_series("activeconnections"),
            "new_connections":    vm_series("newconnections"),
        }'''

ECS_OLD = '''        def vm_series(yace_metric):
            return vm_query_range(
                f'{yace_metric}{{{dim}}}',
                start=int(start.timestamp()), end=int(end.timestamp()),
                step=f"{period}s",
            )

        cpu = vm_series("aws_ecs_cpuutilization_average")
        mem = vm_series("aws_ecs_memory_utilization_average")'''

ECS_NEW = '''        # Match on the bare cluster_name param (== resources.name), confirmed
        # against app/collector/discovery/runner.py's _discover_ecs().
        def vm_series(db_metric_name):
            return _metric_history_query_range("ecs", cluster_name, db_metric_name,
                                                start, end, match_field="name")

        # AWS/ECS CPUUtilization/MemoryUtilization are EXCLUDED from Phase
        # 1's GMD collector entirely (its own docstring: "AWS/ECS basic
        # monitoring is FREE (no API cost)" -- deliberately left on boto3).
        # metric_history never has these, so both calls always return []
        # and this always falls through to the boto3 fallback below -- a
        # behavior-preserving no-op change (this chart was already
        # effectively boto3-only in practice, same as the comments above
        # already implied before VM was ever confirmed to have this data).
        cpu = vm_series("cpuutilization")
        mem = vm_series("memoryutilization")'''


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
        if n == 0:
            return None, f"{label}: expected block not found (likely already patched differently) -- skipping."
        if n > 1:
            die(f"{label}: expected exactly 1 match for one of the expected blocks, found {n}. "
                f"File may differ from what this script expects.")
        new_content = new_content.replace(old, new, 1)
    return new_content, f"{label}: OK ({len(new_content) - len(content):+d} bytes)"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="(default; kept for backward compatibility)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    repo_root = find_repo_root()
    path = os.path.join(repo_root, "app", "aws", "collector_direct.py")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    content, note = prepare_patch(
        path, "app/aws/collector_direct.py",
        [
            (HEADER_OLD, HEADER_NEW),
            (EC2_OLD, EC2_NEW),
            (EC2_FIELDS_OLD, EC2_FIELDS_NEW),
            (EBS_OLD, EBS_NEW),
            (LAMBDA_OLD, LAMBDA_NEW),
            (RDS_OLD, RDS_NEW),
            (ELB_OLD, ELB_NEW),
            (ECS_OLD, ECS_NEW),
        ],
        "_metric_history_query_range",
    )
    print(f"\nFile patch plan:\n  {note}")

    if content is None:
        print("\nNothing to do.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"Patched app/aws/collector_direct.py")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 40 --no-pager

  B) Open a real EC2/RDS instance detail page in the UI (or curl the
     endpoint directly) and confirm charts populate:
       curl -s "http://127.0.0.1:8000/api/live/metrics/ec2/<instance_id>?hours=6" \\
         -H "Cookie: <your session cookie>" | python3 -m json.tool
     Compare against what the page showed before this change -- cpu/
     network_in/network_out/disk_read/disk_write should all have real
     points if that instance has been running long enough for Phase 1's
     collector to have written history for it.

  C) Check the logs for "metric_history query_range failed" -- if you
     see these, something's wrong with the DB query itself, not just a
     no-data case (no-data is silent, by design, matching the old
     vm_query_range contract):
       sudo journalctl -u monitoring-hub --since "-10min" --no-pager | grep "metric_history query_range failed"

  D) Confirm EBS burst_balance is empty (expected, documented) and every
     other EBS series has data:
       curl -s "http://127.0.0.1:8000/api/live/metrics/ebs/<volume_id>?hours=6" \\
         -H "Cookie: <your session cookie>" | python3 -m json.tool

  E) For Lambda/ELB/ECS: confirm the "always missing -> boto3 fallback"
     fields (concurrent; errors_4xx/errors_elb_5xx/unhealthy_hosts/
     active_connections/new_connections; ECS cpu/mem) still populate via
     the existing fallback -- they should look no different than before
     this change, just via a live boto3 call every time instead of
     sometimes-VM-sometimes-boto3.

  F) Review, commit, push:
       git status
       git diff app/aws/collector_direct.py
       git add app/aws/collector_direct.py apply_dashboard_charts_metric_history.py
       git commit -m "feat(charts): Phase 4a of removing VictoriaMetrics -- retarget the 6 chart-detail endpoints (EC2/EBS/RDS/Lambda/ELB/ECS) from VM to the local metric_history table; EBS burst_balance is now a documented permanent gap (Phase 1 never collected it and this function has no boto3 fallback, unlike the other 5)"
       git push origin main

  Next: Phase 4b (list-view snapshot functions -- _ec2_raw, _ebs_raw, and
  the same shape for RDS/Lambda/ELB list views -- still read from VM via
  vm_query_all, not yet converted). Only once Phase 4b is done can
  vm_client.py's reachability actually be re-checked for retirement.
""")


if __name__ == "__main__":
    main()
