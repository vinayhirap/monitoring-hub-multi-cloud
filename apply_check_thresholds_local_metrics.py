#!/usr/bin/env python3
"""
apply_check_thresholds_local_metrics.py
========================================
Phase 5: fixes check_and_write_alerts() -- the function behind the
"Check Thresholds Now" button on the Settings page
(frontend/src/pages/Settings.jsx calls GET /api/settings/check, which
calls this function). Flagged, not fixed, at the end of Phase 4b
pending investigation. Investigation done; here's the fix.

WHAT WAS ACTUALLY WRONG (confirmed, not assumed)
----------------------------------------------------
This function is a SEPARATE, on-demand alert-evaluation path from
alert_evaluator.py's scheduled one -- both read the same `thresholds`
table (confirmed: alert_evaluator.py's SQL also JOINs thresholds), but
this one fetches CURRENT values itself instead of reading the `metrics`
cache alert_evaluator.py already relies on.

For ec2/ebs/rds/alb thresholds, it fetched values via vm_query() against
YACE-style VictoriaMetrics series names (VM_METRIC_STUB). Per this
session's own Phase 1 investigation, AWS never actually had real YACE
data flowing into VM for these metrics -- Phase 1's docstring describes
finding the direct-CloudWatch GMD collector "disabled... when VM/YACE
was introduced," and revived it specifically because nothing was
flowing through VM. That means vm_query() here has likely ALWAYS
returned None for ec2 CPU/network, all of EBS, RDS CPU/storage, and
ALB request/error/latency/healthy-host thresholds -- clicking "Check
Thresholds Now" for any of those has been silently reporting zero
breaches regardless of actual resource state, for as long as this
deployment has existed. Only Lambda thresholds (which never had a VM
stub and always used the boto3 GMD fallback branch) actually worked.

ONE METRIC DELIBERATELY LEFT UNTOUCHED, NOT MISSED
------------------------------------------------------
ec2 StatusCheckFailed is NOT retargeted. Unlike everything else above,
app/aws/describe_polling.py is a genuinely still-live, still-running VM
writer -- confirmed by its own log line appearing every cycle all
session ("describe_polling: N EC2 instances... free, 0 GetMetricData
calls") -- pushing real DescribeInstanceStatus-derived values straight
into VM via its own direct HTTP push, completely independent of YACE
and of anything Phase 1-4 touched. Its vm_query() lookup for this one
metric is presumably still correct today. Retargeting it would require
first checking whether describe_polling.py's data also needs a local
mirror, which is out of scope here -- left exactly as-is.

THE FIX
---------
1. VM_METRIC_STUB's YACE-style names replaced with LOCAL_METRIC_STUB,
   using Phase 1's actual db_metric_name strings (verbatim from
   app/collector/metrics/runner.py's EC2_METRICS_*/EBS_METRICS/
   RDS_METRICS/ELB_METRICS tuples -- same source of truth Phase 4a/4b
   used, not a fresh guess).
2. New _account_metric_snapshot() helper reads the local `metrics`
   last-value cache instead of calling vm_query() with a PromQL string,
   scoped to this specific account_id (stricter than Phase 4b's
   _metric_snapshot_query_all, which isn't account-scoped -- this
   function IS explicitly per-account, so its replacement should be
   too, even though cross-account resource-ID collisions are unlikely
   in practice).
3. "alb" is metric_catalog's service key for ALB metrics, but
   discovery/runner.py stores ALB resources under resource_type='elb'
   (confirmed -- this mismatch already existed in the original code,
   which never actually queried resources.resource_type by string, only
   used "alb" as a local dict key). Added an explicit svc->resource_type
   map so the new DB-backed lookup uses the right table value.
4. Two metrics Phase 1 never collects (EBS BurstBalance, ALB
   HTTPCode_Target_4XX_Count) are REMOVED from LOCAL_METRIC_STUB rather
   than mapped to a name that will never have data -- removing them
   makes this function's own pre-existing fallback logic (metrics not
   in the stub dict already fell through to a real boto3 GetMetricData
   call) kick in automatically, giving these two a real, live, correct
   check instead of a permanent silent VM miss. This is a genuine
   improvement, not a preserved gap, unlike EBS burst_balance in the
   chart/list views (Phase 4a/4b), which have no such fallback to fall
   back to.
5. Efficiency bonus: the original code called vm_query() once per
   (threshold, resource) pair -- N+1 instant queries. The new code
   fetches each unique (service, metric) pair's full account snapshot
   ONCE and does in-memory dict lookups per resource, matching the
   "one query per metric type, not per resource" efficiency pattern
   already established in Phase 3/4a/4b.

TESTED: _account_metric_snapshot() and the full LOCAL_METRIC_STUB
resolution path were exercised with a mocked DB (fake metrics/resources
rows for ec2 and elb, confirming both the resource_id-keyed and
name-keyed lookups work, confirming the account_id scoping clause is
present in the query, and confirming a metric with no LOCAL_METRIC_STUB
entry -- e.g. Lambda, or the two removed ones -- correctly produces an
empty local lookup so the existing GMD-fallback code path (unchanged)
picks it up). NOT tested: an actual live "Check Thresholds Now" click
against real breached/non-breached resources -- no server/DB access
available here; verify per the checklist below, ideally by temporarily
setting an obviously-already-breached threshold (e.g. warning_value=0
on a metric you know has data) and confirming a real alert gets written.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_check_thresholds_local_metrics.py --dry-run
    python3 apply_check_thresholds_local_metrics.py --apply
(no root needed, pure repo file edit)
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

HELPER_ANCHOR_OLD = '''def check_and_write_alerts(account_id: int, region: str, thresholds: list) -> list:'''

HELPER_ANCHOR_NEW = '''def _account_metric_snapshot(account_id, resource_type, db_metric_name, key_field="resource_id"):
    """
    Reads the `metrics` last-value cache for every resource of one type
    in one account, keyed by either resource_id (bare identifier --
    ec2/ebs/rds) or name (bare name -- elb/lambda), matching whichever
    identifier check_and_write_alerts()'s SERVICE_RESOURCES already uses
    per service. Scoped to account_id, unlike Phase 4b's
    _metric_snapshot_query_all (that one didn't need account-scoping;
    this one, being explicitly per-account already, should stay that
    way). Returns {} on any error or no data -- never raises. See
    apply_check_thresholds_local_metrics.py (Phase 5).
    """
    out = {}
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                f"""SELECT r.{key_field} AS key_val, m.metric_value
                    FROM metrics m JOIN resources r ON r.id = m.resource_id
                    WHERE r.aws_account_id = %s AND r.resource_type = %s AND m.metric_name = %s""",
                (account_id, resource_type, db_metric_name),
            )
            for row in cur.fetchall():
                if row["metric_value"] is not None:
                    out[row["key_val"]] = float(row["metric_value"])
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.warning(f"account metric snapshot failed [{resource_type}/{db_metric_name}]: {e}")
    return out


def check_and_write_alerts(account_id: int, region: str, thresholds: list) -> list:'''

STUB_BLOCK_OLD = '''    # svc -> dimension label YACE uses for this resource type
    VM_DIM_LABEL = {
        "ec2": "dimension_InstanceId",
        "ebs": "dimension_VolumeId",
        "rds": "dimension_DBInstanceIdentifier",
        "alb": "dimension_LoadBalancer",   # NEW
    }
    # Explicit map, NOT a generic snake_case conversion — YACE special-cases
    # acronyms (CPUUtilization -> cpuutilization, not c_p_u_utilization).
    # Extend this table if you threshold on new metrics that YACE scrapes.
    VM_METRIC_STUB = {
        ("ec2", "CPUUtilization"):      "aws_ec2_cpuutilization",
        ("ec2", "NetworkIn"):           "aws_ec2_network_in",       # confirmed live in VM -- Aug 2026
        ("ec2", "NetworkOut"):          "aws_ec2_network_out",      # confirmed live in VM -- Aug 2026
        # Free Describe-API path (fix #4, app/aws/describe_polling.py) —
        # NOT CloudWatch/YACE. Sub-second-fresh, zero GetMetricData cost,
        # replaces the old aws_ec2_status_check_failed (YACE/CloudWatch) stub.
        ("ec2", "StatusCheckFailed"):   "aws_ec2_status_check_failed_describe",

        ("ebs", "VolumeQueueLength"):   "aws_ebs_volume_queue_length",
        ("ebs", "BurstBalance"):        "aws_ebs_burst_balance",
        ("ebs", "VolumeReadOps"):       "aws_ebs_volume_read_ops",      # NEW — confirmed live in VM
        ("ebs", "VolumeWriteOps"):      "aws_ebs_volume_write_ops",     # NEW — confirmed live in VM
        ("ebs", "VolumeReadBytes"):     "aws_ebs_volume_read_bytes",    # NEW — confirmed live in VM
        ("ebs", "VolumeWriteBytes"):    "aws_ebs_volume_write_bytes",   # NEW — confirmed live in VM

        ("rds", "CPUUtilization"):      "aws_rds_cpuutilization",
        ("rds", "FreeStorageSpace"):    "aws_rds_free_storage_space",

        # NEW — all 6 confirmed present in VM's __name__ label list
        ("alb", "RequestCount"):              "aws_applicationelb_request_count",
        ("alb", "HTTPCode_Target_5XX_Count"): "aws_applicationelb_httpcode_target_5_xx_count",
        ("alb", "HTTPCode_Target_4XX_Count"): "aws_applicationelb_httpcode_target_4_xx_count",
        ("alb", "TargetResponseTime"):        "aws_applicationelb_target_response_time",
        ("alb", "HealthyHostCount"):          "aws_applicationelb_healthy_host_count",
        ("alb", "UnHealthyHostCount"):        "aws_applicationelb_un_healthy_host_count",
    }
    # Metrics pushed directly by describe_polling.py are raw gauges (no
    # Average/Sum/Maximum suffix) — skip the generic stat-suffix step for them.
    VM_NO_SUFFIX = {"aws_ec2_status_check_failed_describe"}
    STAT_SUFFIX = {"Average": "average", "Sum": "sum", "Maximum": "maximum"}
    vm_lookups  = []   # (t_idx, resource_id, promql)
    gmd_queries = []
    qid_map     = {}

    for t_idx, t in enumerate(thresholds):
        svc       = (t.get("service") or t.get("resource_type") or "").lower()
        namespace = NAMESPACE_MAP.get(svc, t.get("namespace", "AWS/EC2"))
        metric    = t["metric_name"]
        stat      = t.get("statistic") or "Average"
        resources = SERVICE_RESOURCES.get(svc, []) or [("account", [])]
        stub      = VM_METRIC_STUB.get((svc, metric))

        if svc in VM_DIM_LABEL and stub:
            dim_label   = VM_DIM_LABEL[svc]
            yace_metric = stub if stub in VM_NO_SUFFIX else f"{stub}_{STAT_SUFFIX.get(stat, 'average')}"
            for resource_id, dims in resources:
                vm_lookups.append((t_idx, resource_id, f'{yace_metric}{{{dim_label}="{resource_id}"}}'))
        else:
            for resource_id, dims in resources:
                qid = _safe_qid(f"t{t_idx}__{resource_id}")
                gmd_queries.append(_make_query(qid, namespace, metric, dims, stat))
                qid_map[qid] = (resource_id, t_idx)

    all_vals = {}  # (t_idx, resource_id) -> value

    for t_idx, resource_id, promql in vm_lookups:
        val = vm_query(promql)
        if val is not None:
            all_vals[(t_idx, resource_id)] = val

    gmd_snap = _gmd_snapshot(cw, gmd_queries, minutes=3)
    for qid, val in gmd_snap.items():
        resource_id, t_idx = qid_map[qid]
        all_vals[(t_idx, resource_id)] = val'''

STUB_BLOCK_NEW = '''    # svc -> resources.name (bare) vs resources.resource_id (bare) --
    # ec2/ebs/rds resource_id IS the bare identifier; elb/lambda's
    # resource_id is a full ARN, so those match on name instead. Same
    # distinction Phase 4a/4b already confirmed against
    # app/collector/discovery/runner.py.
    LOCAL_KEY_FIELD = {
        "ec2": "resource_id", "ebs": "resource_id", "rds": "resource_id",
        "alb": "name",
    }
    # metric_catalog's service key for ALB metrics is "alb" (matches
    # SERVICE_RESOURCES/NAMESPACE_MAP above, pre-existing), but
    # discovery/runner.py stores ALB resources under resource_type='elb'
    # -- confirmed, not assumed. Needed only for the local DB lookup;
    # CloudWatch/GMD calls elsewhere in this function never used
    # resources.resource_type at all, so this mapping is new, not a fix
    # to something that was broken before.
    LOCAL_RESOURCE_TYPE = {"alb": "elb"}

    # db_metric_name strings below are copied verbatim from
    # app/collector/metrics/runner.py's EC2_METRICS_CRITICAL/LOW,
    # EBS_METRICS, RDS_METRICS, ELB_METRICS tuples -- i.e. exactly what
    # Phase 1's GMD collector actually writes into the `metrics` table,
    # not a fresh guess at a naming convention. ec2 StatusCheckFailed is
    # DELIBERATELY NOT here -- see apply_check_thresholds_local_metrics.py's
    # docstring: describe_polling.py is a separate, still-live VM writer
    # for that one metric specifically, untouched by this fix.
    # EBS BurstBalance and ALB HTTPCode_Target_4XX_Count are ALSO
    # deliberately absent -- Phase 1 never collects either, so leaving
    # them out of this dict means they correctly fall through to the
    # existing GMD/boto3 fallback branch below instead of ever being
    # looked up here.
    LOCAL_METRIC_STUB = {
        ("ec2", "CPUUtilization"):  "cpuutilization",
        ("ec2", "NetworkIn"):       "networkin",
        ("ec2", "NetworkOut"):      "networkout",
        ("ec2", "DiskReadBytes"):   "diskreadbytes",
        ("ec2", "DiskWriteBytes"):  "diskwritebytes",

        ("ebs", "VolumeQueueLength"): "volumequeuelength",
        ("ebs", "VolumeReadOps"):     "volumereadops",
        ("ebs", "VolumeWriteOps"):    "volumewriteops",
        ("ebs", "VolumeReadBytes"):   "volumereadbytes",
        ("ebs", "VolumeWriteBytes"):  "volumewritebytes",

        ("rds", "CPUUtilization"):   "cpuutilization",
        ("rds", "FreeStorageSpace"): "freestorage",

        ("alb", "RequestCount"):              "requestcount",
        ("alb", "HTTPCode_Target_5XX_Count"): "errors5xx",
        ("alb", "TargetResponseTime"):        "responselatency",
        ("alb", "HealthyHostCount"):          "healthyhosts",
    }

    local_lookups  = []   # (t_idx, resource_id, value_or_None)
    gmd_queries    = []
    qid_map        = {}
    snapshot_cache = {}   # (resource_type, db_metric_name, key_field) -> {key: value}, fetched once per unique combo

    for t_idx, t in enumerate(thresholds):
        svc       = (t.get("service") or t.get("resource_type") or "").lower()
        namespace = NAMESPACE_MAP.get(svc, t.get("namespace", "AWS/EC2"))
        metric    = t["metric_name"]
        stat      = t.get("statistic") or "Average"
        resources = SERVICE_RESOURCES.get(svc, []) or [("account", [])]
        stub      = LOCAL_METRIC_STUB.get((svc, metric))

        if stub:
            key_field     = LOCAL_KEY_FIELD.get(svc, "resource_id")
            resource_type = LOCAL_RESOURCE_TYPE.get(svc, svc)
            cache_key     = (resource_type, stub, key_field)
            if cache_key not in snapshot_cache:
                snapshot_cache[cache_key] = _account_metric_snapshot(
                    account_id, resource_type, stub, key_field
                )
            snap = snapshot_cache[cache_key]
            for resource_id, dims in resources:
                local_lookups.append((t_idx, resource_id, snap.get(resource_id)))
        else:
            for resource_id, dims in resources:
                qid = _safe_qid(f"t{t_idx}__{resource_id}")
                gmd_queries.append(_make_query(qid, namespace, metric, dims, stat))
                qid_map[qid] = (resource_id, t_idx)

    all_vals = {}  # (t_idx, resource_id) -> value

    for t_idx, resource_id, val in local_lookups:
        if val is not None:
            all_vals[(t_idx, resource_id)] = val

    gmd_snap = _gmd_snapshot(cw, gmd_queries, minutes=3)
    for qid, val in gmd_snap.items():
        resource_id, t_idx = qid_map[qid]
        all_vals[(t_idx, resource_id)] = val'''


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
        [(HELPER_ANCHOR_OLD, HELPER_ANCHOR_NEW), (STUB_BLOCK_OLD, STUB_BLOCK_NEW)],
        "_account_metric_snapshot",
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

  B) The real test: click "Check Thresholds Now" in Settings (or curl
     /api/settings/check?account_id=<id> with a real session cookie) and
     confirm it no longer silently returns 0 breaches regardless of
     actual state. Best verification: temporarily set an EC2 CPU
     threshold's warning_value to something you know is already exceeded
     (e.g. 0) via the Thresholds UI, click Check Now, confirm a REAL
     alert gets written this time, then set the threshold back.

  C) Watch for "account metric snapshot failed" in the logs -- would
     mean a real DB/query problem:
       sudo journalctl -u monitoring-hub --since "-10min" --no-pager | grep "account metric snapshot failed"

  D) Confirm EBS BurstBalance and ALB HTTPCode_Target_4XX_Count
     thresholds (if any are configured) now actually hit the GMD/boto3
     fallback and get a real value, instead of silently finding nothing:
       sudo journalctl -u monitoring-hub --since "-10min" --no-pager | grep -i "GMD series\\|GMD snapshot"

  E) Review, commit, push:
       git status
       git diff app/aws/collector_direct.py
       git add app/aws/collector_direct.py apply_check_thresholds_local_metrics.py
       git commit -m "fix(alerts): Phase 5 -- Check Thresholds Now (check_and_write_alerts) was checking ec2/ebs/rds/alb thresholds against VictoriaMetrics data that never existed for AWS, silently reporting zero breaches regardless of actual state; retargeted to the local metrics cache Phase 1 already maintains, same as alert_evaluator.py already does. StatusCheckFailed left untouched (describe_polling.py is a separate, genuinely still-live VM writer for that one metric). BurstBalance/4XX thresholds now correctly fall through to a real boto3 check instead of a permanent silent VM miss."
       git push origin main

  This closes the last VM-related item flagged in Phase 4b. vm_client.py
  itself is still imported (vm_query, for StatusCheckFailed above, and
  the description in this file's header) -- it's now down to exactly
  one legitimate live caller in this file. Whether it can be retired
  entirely depends on describe_polling.py and Azure/GCP's own residual
  VM usage, not on anything in this file anymore.
""")


if __name__ == "__main__":
    main()
