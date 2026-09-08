#!/usr/bin/env python3
"""
apply_list_view_snapshots_metrics.py
========================================
Phase 4b of removing VictoriaMetrics: retarget the last 2 VM-dependent
list-view snapshot functions in app/aws/collector_direct.py --
_ec2_raw() and _ebs_raw(), which power the account-level EC2/EBS list
pages (as opposed to Phase 4a's per-resource chart-detail pages).

WHAT THIS FINISHES, AND WHAT IT DOESN'T
------------------------------------------
Investigating collector_direct.py for Phase 4a found MORE VM read call
sites than HANDOVER.md's one-line Phase 4 description implied. This
script closes the ones that are the same shape as Phase 4a (snapshot/
series reads feeding this app's own resource pages):
  - _ec2_raw(): 3 vm_query_all calls (cpu, network_in, network_out)
  - _ebs_raw(): 6 vm_query_all calls (read/write ops, read/write bytes,
    queue_length, burst_balance)
Both retargeted to a new _metric_snapshot_query_all() helper reading the
`metrics` last-value cache Phase 1's GMD collector already maintains --
no time range needed here (list views want CURRENT values, not history,
so `metrics` is the right table, not metric_history).

STILL NOT DONE, FOUND BUT DELIBERATELY NOT TOUCHED HERE: a THIRD, and
structurally different, VM-reading code path -- collector_direct.py's
check_and_write_alerts() (imported live by app/api/settings.py) has its
OWN separate threshold-alerting logic built around YACE/VM metric-name
stubs (VM_METRIC_STUB, VM_DIM_LABEL dicts, a single vm_query() instant
call per resource+metric). This is NOT the same thing as
alert_evaluator.py (the scheduled, `metrics`-table-reading evaluator
Phase 1-3 already feed correctly) -- it looks like a second,
independent alerting mechanism, possibly older or possibly a
manually-triggered "test thresholds now" path reachable from Settings.
Retargeting it blind, without first understanding WHY two separate
alert-evaluation code paths exist and which one is actually
authoritative, risks silently changing alerting behavior in a way this
script can't verify against live data. Flagged clearly as a genuinely
new finding for next session, not fixed here -- see the verification
checklist below for how to start investigating it.

Once check_and_write_alerts() is understood and either retargeted or
confirmed to intentionally still need VM, vm_client.py's reachability
can be re-checked for real retirement -- not before, since that
function is still a live caller.

RESOURCE MATCHING
-------------------
Both EC2 and EBS use resources.resource_id-based matching (bare
instance_id / volume_id) -- same convention already confirmed and used
in Phase 4a, verified against app/collector/discovery/runner.py.

TESTED: the new _metric_snapshot_query_all() helper was exercised with
a mocked DB cursor returning realistic metrics/resources rows,
confirming correct SQL, correct {resource_id: value} dict shape matching
vm_query_all's old contract, and safe {} on error. NOT tested: an actual
live list-page request against real data -- no server/DB access
available here; verify per the checklist below.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_list_view_snapshots_metrics.py --dry-run
    python3 apply_list_view_snapshots_metrics.py --apply
(no root needed, pure repo file edit)
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

HELPER_ANCHOR_OLD = '''def _metric_history_query_range(resource_type, identifier, db_metric_name,
                                 start_dt, end_dt, match_field="resource_id"):'''

HELPER_ANCHOR_NEW = '''def _metric_snapshot_query_all(resource_type, db_metric_name):
    """
    Drop-in replacement for vm_client.vm_query_all's role in the
    list-view snapshot functions below (_ec2_raw, _ebs_raw): every
    resource's CURRENT value in one query, keyed by resources.resource_id
    (bare instance_id/volume_id -- both list views only use resource_id-
    based matching, same convention Phase 4a confirmed and used).
    Reads the `metrics` last-value cache Phase 1's GMD collector already
    maintains -- no time range needed, this is a snapshot, not a series.
    Returns {} on any error -- same never-raises contract vm_query_all
    had. See apply_list_view_snapshots_metrics.py (Phase 4b).
    """
    out = {}
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                """SELECT r.resource_id, m.metric_value
                   FROM metrics m JOIN resources r ON r.id = m.resource_id
                   WHERE r.resource_type = %s AND m.metric_name = %s""",
                (resource_type, db_metric_name),
            )
            for row in cur.fetchall():
                if row["metric_value"] is not None:
                    out[row["resource_id"]] = float(row["metric_value"])
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.warning(f"metric snapshot query_all failed [{resource_type}/{db_metric_name}]: {e}")
    return out


def _metric_history_query_range(resource_type, identifier, db_metric_name,
                                 start_dt, end_dt, match_field="resource_id"):'''

EC2_RAW_OLD = '''        # One VM call per metric gets EVERY instance's current value at once —
        # no need to loop per-instance like the old GMD approach.
        cpu_map    = vm_query_all("aws_ec2_cpuutilization_average", "dimension_InstanceId")
        netin_map  = vm_query_all("aws_ec2_network_in_average",      "dimension_InstanceId")
        netout_map = vm_query_all("aws_ec2_network_out_average",     "dimension_InstanceId")'''

EC2_RAW_NEW = '''        # One DB query per metric gets EVERY instance's current value at
        # once -- same "one call, not one per instance" shape the VM call
        # this replaces had, just against the local `metrics` cache now.
        cpu_map    = _metric_snapshot_query_all("ec2", "cpuutilization")
        netin_map  = _metric_snapshot_query_all("ec2", "networkin")
        netout_map = _metric_snapshot_query_all("ec2", "networkout")'''

EC2_LOG_OLD = '''        logger.info(f"EC2: {len(out)} in {region} ({len(running)} running, via VM)")'''

EC2_LOG_NEW = '''        logger.info(f"EC2: {len(out)} in {region} ({len(running)} running, via metrics cache)")'''

EBS_RAW_OLD = '''        read_ops_map  = vm_query_all("aws_ebs_volume_read_ops_average",     "dimension_VolumeId")
        write_ops_map = vm_query_all("aws_ebs_volume_write_ops_average",    "dimension_VolumeId")
        read_b_map    = vm_query_all("aws_ebs_volume_read_bytes_average",   "dimension_VolumeId")
        write_b_map   = vm_query_all("aws_ebs_volume_write_bytes_average",  "dimension_VolumeId")
        queue_map     = vm_query_all("aws_ebs_volume_queue_length_average", "dimension_VolumeId")
        burst_map     = vm_query_all("aws_ebs_burst_balance_average", "dimension_VolumeId")'''

EBS_RAW_NEW = '''        read_ops_map  = _metric_snapshot_query_all("ebs", "volumereadops")
        write_ops_map = _metric_snapshot_query_all("ebs", "volumewriteops")
        read_b_map    = _metric_snapshot_query_all("ebs", "volumereadbytes")
        write_b_map   = _metric_snapshot_query_all("ebs", "volumewritebytes")
        queue_map     = _metric_snapshot_query_all("ebs", "volumequeuelength")
        # burst_balance: Phase 1's GMD collector never collects this
        # (dropped per its own triage note, "gp3 irrelevant") -- always
        # empty now, same documented gap as the EBS chart-detail page
        # (Phase 4a). Unlike that page, this list view's burst_balance
        # column has always defaulted to 0.0 via .get(vid, 0.0) below
        # rather than showing "no data", so the visible behavior here is
        # unchanged either way -- just always 0.0 now instead of
        # sometimes-VM-sometimes-0.0.
        burst_map     = _metric_snapshot_query_all("ebs", "volumeburstbalance")'''

HEADER_LIST_VIEW_OLD = '''  - LIST-view snapshot functions (_ec2_raw, _ebs_raw, etc. -- "every
    resource's current value in one call") still read from VM via
    vm_query_all. NOT yet converted -- Phase 4b, still open.'''

HEADER_LIST_VIEW_NEW = '''  - LIST-view snapshot functions (_ec2_raw, _ebs_raw) now read from the
    `metrics` last-value cache too (Phase 4b, see
    apply_list_view_snapshots_metrics.py). check_and_write_alerts()
    (below) has its OWN separate, still-VM-dependent alerting logic --
    NOT part of Phase 4a/4b, found but deliberately not touched yet
    (needs its own investigation first -- see that script's docstring).'''

IMPORT_OLD = "from app.clients.vm_client import vm_query, vm_query_all"

IMPORT_NEW = "from app.clients.vm_client import vm_query  # vm_query_all retired here -- see apply_list_view_snapshots_metrics.py (Phase 4b). Still imported: vm_query, used only by check_and_write_alerts() below (NOT yet converted -- separate, not-yet-investigated legacy alert path, see this script's docstring)."


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
            (HEADER_LIST_VIEW_OLD, HEADER_LIST_VIEW_NEW),
            (IMPORT_OLD, IMPORT_NEW),
            (HELPER_ANCHOR_OLD, HELPER_ANCHOR_NEW),
            (EC2_RAW_OLD, EC2_RAW_NEW),
            (EC2_LOG_OLD, EC2_LOG_NEW),
            (EBS_RAW_OLD, EBS_RAW_NEW),
        ],
        "_metric_snapshot_query_all",
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

  B) Confirm EC2/EBS list pages still populate CPU/network/read-write
     columns (curl or open the UI):
       curl -s "http://127.0.0.1:8000/api/live/ec2/<account_db_id>" \\
         -H "Cookie: <your session cookie>" | python3 -m json.tool
       curl -s "http://127.0.0.1:8000/api/live/ebs/<account_db_id>" \\
         -H "Cookie: <your session cookie>" | python3 -m json.tool
     burst_balance will show 0.0 for every volume now (documented, see
     this script's EBS_RAW_NEW comment) -- everything else should have
     real numbers for resources Phase 1 has collected data for.

  C) Watch for "metric snapshot query_all failed" in logs -- would mean
     a real DB/query problem, not just a no-data case:
       sudo journalctl -u monitoring-hub --since "-10min" --no-pager | grep "metric snapshot query_all failed"

  D) Start investigating check_and_write_alerts() (this script's
     docstring) before touching it -- don't guess:
       grep -n "def check_and_write_alerts" -A 30 app/aws/collector_direct.py
       grep -n "check_and_write_alerts" app/api/settings.py
     Figure out: is this reachable from a live endpoint right now, and
     if so is it actually still relied on, or has alert_evaluator.py
     fully superseded it? That answer decides whether it needs a Phase
     5 retarget, a removal, or nothing at all.

  E) Review, commit, push:
       git status
       git diff app/aws/collector_direct.py
       git add app/aws/collector_direct.py apply_list_view_snapshots_metrics.py
       git commit -m "feat(lists): Phase 4b of removing VictoriaMetrics -- retarget EC2/EBS list-view snapshots (_ec2_raw, _ebs_raw) from VM to the metrics last-value cache; found (not fixed) a third, separate VM-dependent alert path in check_and_write_alerts() that needs its own investigation before touching"
       git push origin main
""")


if __name__ == "__main__":
    main()
