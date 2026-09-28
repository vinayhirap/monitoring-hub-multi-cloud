#!/usr/bin/env python3
"""
apply_final_cleanup.py
========================================
Consolidates everything left over from this session into one script:
a real bug fix in Phase 5's own patch, plus the remaining ops cleanup
(cloudops user removal, stray .bak file cleanup).

PART 1 -- A REAL BUG IN PHASE 5's OWN FIX (found by re-investigating
describe_polling.py, not assumed)
----------------------------------------------------------------------
Phase 5 (apply_check_thresholds_local_metrics.py) documented "ec2
StatusCheckFailed is NOT retargeted... its vm_query() lookup... is
presumably still correct" -- but Phase 5's actual patch REMOVED the
entire vm_lookups/vm_query() code path (replaced by local_lookups),
and StatusCheckFailed was deliberately left OUT of LOCAL_METRIC_STUB.
Net effect, not what Phase 5's own docstring claimed: StatusCheckFailed
now falls through to the GMD/boto3 branch -- a REAL, BILLED CloudWatch
GetMetricData call -- every time "Check Thresholds Now" is clicked for
a StatusCheckFailed threshold. That's the opposite of
describe_polling.py's entire purpose (its own docstring: "This REPLACES
the need to CloudWatch-poll EC2 StatusCheckFailed at all"). Confirmed by
checking the actual patched file: `vm_query` is imported in
collector_direct.py but never called anywhere -- dead import, silent
behavior change, not documented correctly by Phase 5.

Fixed properly here, not patched around: app/aws/describe_polling.py's
poll_ec2_status() already queries `resources` for every running EC2
instance (for the VM push) -- it just wasn't selecting resources.id.
Adding that one column lets it ALSO write StatusCheckFailed into the
local `metrics` table (dual-write: VM push kept as-is, local write
added), using the exact same account-scoped snapshot mechanism Phase 5
already built. Then StatusCheckFailed goes back into LOCAL_METRIC_STUB,
now genuinely backed by real local data, and the now-truly-unused
`vm_query` import in collector_direct.py is removed for real.

PART 2 -- poll_alb_target_health() IS DELIBERATELY NOT TOUCHED
------------------------------------------------------------------
Investigated, not guessed at, and concluded it should stay VM-only:
  - It operates at the TARGET GROUP level (DescribeTargetGroups),
    and there is no "target_group" resource_type anywhere in the
    `resources` table (confirmed against
    app/collector/discovery/runner.py -- only 'elb' exists, for load
    balancers, keyed by LB ARN). There's no local resource_db_id to
    write against without either adding a whole new resource type
    (real scope creep) or aggregating target-group health up to the
    LB level via each target group's LoadBalancerArns field (a real
    option, but introduces a many-target-groups-per-LB aggregation
    decision this script shouldn't make unilaterally).
  - More importantly: this module's own docstring says its purpose is
    "so the rest of the app (Grafana, FastAPI reads) can query them
    exactly like any YACE-scraped series" -- describing an EXTERNAL
    Grafana dashboard as an intended consumer, entirely outside this
    Python codebase's visibility. Retargeting or removing this VM push
    could silently break a dashboard this script has no way to check
    for. EC2 StatusCheckFailed above is different: this codebase's OWN
    check_and_write_alerts() is confirmed to be the (mis-)consumer
    there, so fixing that consumer's data source is safe. No equivalent
    internal consumer was found for the ALB describe metrics.
  - Conclusion: vm_client.py is NOT fully retirable, and that's fine --
    it has exactly one remaining legitimate purpose
    (poll_alb_target_health()'s external-Grafana-compatible push),
    clearly identified rather than left as a mystery.

PART 3 -- OPS CLEANUP
-------------------------
  - Removes the cloudops OS user (deliberately left in place after the
    hcsadmin consolidation, pending confirmation of stability -- it's
    been running clean since). Confirms no process is still running as
    cloudops before removing it, rather than assuming.
  - Cleans up the accumulated *.bak.TIMESTAMP backup files this
    session's various apply_*.py/fix_*.py scripts have been leaving in
    /opt/monitoring-hub/app/ every time they patch something (by
    design -- each script backs up before writing). They're genuinely
    unneeded once a change is committed to git (git IS the real backup),
    and there are enough of them now to be clutter. Moved into
    scripts/archive/session_backups/ rather than deleted outright --
    consistent with this session's own earlier precedent
    (fix_archive_orphaned_root_scripts.py archived rather than deleted).

TESTED: the describe_polling.py SQL change and the new local-write logic
were exercised with a mocked DB cursor/write_metrics_batch, confirming
resource_db_id is correctly threaded through per instance and the VM
push payload is unchanged (still keyed by resource_id, not resource_db_id,
so the external Grafana consumer sees zero difference). NOT tested: an
actual live poll cycle against real AWS accounts -- no credentials or
network access available here; verify per the checklist below.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_final_cleanup.py --dry-run
    python3 apply_final_cleanup.py --apply
Part 3's userdel/file-move steps need root; Part 1's file patch does not.
If run as a non-root user, Part 1 still applies fully and Part 3 is
skipped with a clear note (re-run with sudo to also do Part 3).
"""

import argparse
import grp
import os
import pwd
import shutil
import subprocess
import sys
from datetime import datetime

# ─────────────────────────── Part 1: describe_polling.py + collector_direct.py ───────────────────────────

DESCRIBE_HEADER_OLD = '''Metric names pushed:
  aws_ec2_status_check_failed_describe{dimension_InstanceId="..."}   0|1
  aws_alb_healthy_host_count_describe{dimension_TargetGroup="..."}   int
  aws_alb_unhealthy_host_count_describe{dimension_TargetGroup="..."} int
"""
import time
import logging
import requests

from app.db import get_connection'''

DESCRIBE_HEADER_NEW = '''Metric names pushed:
  aws_ec2_status_check_failed_describe{dimension_InstanceId="..."}   0|1
  aws_alb_healthy_host_count_describe{dimension_TargetGroup="..."}   int
  aws_alb_unhealthy_host_count_describe{dimension_TargetGroup="..."} int

EC2 StatusCheckFailed is ALSO written into the local `metrics` table
(see poll_ec2_status() below) -- so check_and_write_alerts() (Settings'
"Check Thresholds Now") can read it locally instead of falling through
to a real, billed CloudWatch call. Fixed a real bug found while
investigating this file for Phase 5 -- see apply_final_cleanup.py.
ALB target-group health stays VM-only: no "target_group" resource type
exists in `resources` to write against, and this module's external-
Grafana-compatible push (see above) is the only known consumer for it.
"""
import time
import logging
import requests

from app.db import get_connection
from app.collector.metrics_writer import write_metrics_batch'''

EC2_QUERY_OLD = '''        cur.execute("""
            SELECT a.id AS account_db_id, a.role_arn, a.external_id, a.default_region,
                   r.resource_id
            FROM resources r
            JOIN aws_accounts a ON a.id = r.aws_account_id
            WHERE r.resource_type = 'ec2'
              AND r.instance_state = 'running'
              AND a.status = 'active'
        """)
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()

    grouped = {}
    for row in rows:
        key = (row["account_db_id"], row["role_arn"], row["external_id"], row["default_region"])
        grouped.setdefault(key, []).append(row["resource_id"])
    return grouped'''

EC2_QUERY_NEW = '''        cur.execute("""
            SELECT a.id AS account_db_id, a.role_arn, a.external_id, a.default_region,
                   r.id AS resource_db_id, r.resource_id
            FROM resources r
            JOIN aws_accounts a ON a.id = r.aws_account_id
            WHERE r.resource_type = 'ec2'
              AND r.instance_state = 'running'
              AND a.status = 'active'
        """)
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()

    grouped = {}
    for row in rows:
        key = (row["account_db_id"], row["role_arn"], row["external_id"], row["default_region"])
        grouped.setdefault(key, []).append((row["resource_id"], row["resource_db_id"]))
    return grouped'''

POLL_EC2_OLD = '''    total = 0
    for (account_db_id, role_arn, external_id, region), instance_ids in _get_ec2_instances_by_region().items():
        if not region or not instance_ids:
            continue
        try:
            session = _session_for(role_arn, external_id, region)
            ec2 = session.client("ec2", region_name=region)
            ts = int(time.time() * 1000)
            lines = []
            # DescribeInstanceStatus accepts up to 100 IDs per call — chunk defensively.
            for i in range(0, len(instance_ids), 100):
                chunk = instance_ids[i:i + 100]
                resp = ec2.describe_instance_status(InstanceIds=chunk, IncludeAllInstances=True)
                for s in resp.get("InstanceStatuses", []):
                    iid = s["InstanceId"]
                    sys_ok = s.get("SystemStatus", {}).get("Status") == "ok"
                    inst_ok = s.get("InstanceStatus", {}).get("Status") == "ok"
                    failed = 0 if (sys_ok and inst_ok) else 1
                    lines.append(
                        f'aws_ec2_status_check_failed_describe{{dimension_InstanceId="{iid}",dimension_AccountId="{account_db_id}"}} {failed} {ts}'
                    )
            _push_to_vm(lines)
            total += len(instance_ids)
        except Exception as e:
            logger.warning(f"describe_polling: EC2 status [{region}, account {account_db_id}]: {e}")
    return total'''

POLL_EC2_NEW = '''    total = 0
    for (account_db_id, role_arn, external_id, region), instance_pairs in _get_ec2_instances_by_region().items():
        if not region or not instance_pairs:
            continue
        instance_ids = [iid for iid, _rdid in instance_pairs]
        resource_db_id_by_iid = dict(instance_pairs)
        try:
            session = _session_for(role_arn, external_id, region)
            ec2 = session.client("ec2", region_name=region)
            ts = int(time.time() * 1000)
            lines = []
            local_rows = []  # (resource_db_id, "statuscheckfailed", value) for the `metrics` table
            # DescribeInstanceStatus accepts up to 100 IDs per call — chunk defensively.
            for i in range(0, len(instance_ids), 100):
                chunk = instance_ids[i:i + 100]
                resp = ec2.describe_instance_status(InstanceIds=chunk, IncludeAllInstances=True)
                for s in resp.get("InstanceStatuses", []):
                    iid = s["InstanceId"]
                    sys_ok = s.get("SystemStatus", {}).get("Status") == "ok"
                    inst_ok = s.get("InstanceStatus", {}).get("Status") == "ok"
                    failed = 0 if (sys_ok and inst_ok) else 1
                    lines.append(
                        f'aws_ec2_status_check_failed_describe{{dimension_InstanceId="{iid}",dimension_AccountId="{account_db_id}"}} {failed} {ts}'
                    )
                    resource_db_id = resource_db_id_by_iid.get(iid)
                    if resource_db_id is not None:
                        local_rows.append((resource_db_id, "statuscheckfailed", float(failed)))
            _push_to_vm(lines)
            if local_rows:
                write_metrics_batch(local_rows)
            total += len(instance_ids)
        except Exception as e:
            logger.warning(f"describe_polling: EC2 status [{region}, account {account_db_id}]: {e}")
    return total'''

COLLECTOR_STUB_OLD = '''    LOCAL_METRIC_STUB = {
        ("ec2", "CPUUtilization"):  "cpuutilization",
        ("ec2", "NetworkIn"):       "networkin",
        ("ec2", "NetworkOut"):      "networkout",
        ("ec2", "DiskReadBytes"):   "diskreadbytes",
        ("ec2", "DiskWriteBytes"):  "diskwritebytes",'''

COLLECTOR_STUB_NEW = '''    LOCAL_METRIC_STUB = {
        ("ec2", "CPUUtilization"):  "cpuutilization",
        ("ec2", "NetworkIn"):       "networkin",
        ("ec2", "NetworkOut"):      "networkout",
        ("ec2", "DiskReadBytes"):   "diskreadbytes",
        ("ec2", "DiskWriteBytes"):  "diskwritebytes",
        # Fixed here (apply_final_cleanup.py) -- Phase 5 documented this
        # as "left on vm_query()" but its own patch actually removed the
        # vm_query() path entirely, so this was silently falling through
        # to a real billed CloudWatch call. app/aws/describe_polling.py
        # now also writes this into the local `metrics` table (in
        # addition to its existing VM push, unchanged), so it belongs
        # here for real now.
        ("ec2", "StatusCheckFailed"): "statuscheckfailed",'''

COLLECTOR_IMPORT_OLD = "from app.clients.vm_client import vm_query  # vm_query_all retired here -- see apply_list_view_snapshots_metrics.py (Phase 4b). Still imported: vm_query, used only by check_and_write_alerts() below (NOT yet converted -- separate, not-yet-investigated legacy alert path, see this script's docstring)."

COLLECTOR_IMPORT_NEW = "# vm_client fully retired from THIS file (apply_final_cleanup.py): vm_query_all went in Phase 4b, vm_query's only use (StatusCheckFailed) is fixed by describe_polling.py now also writing locally. vm_client.py itself is NOT retired overall -- see that script's docstring for its one remaining legitimate use (ALB target-group health, external-Grafana-compatible, in app/aws/describe_polling.py)."


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


# ─────────────────────────── Part 3: cloudops removal + .bak cleanup ───────────────────────────

def part3_ops_cleanup(repo_root, apply_):
    if os.geteuid() != 0:
        print("\n[Part 3 skipped] Not running as root -- cloudops removal and file "
              "moves need it. Re-run with sudo to also do Part 3.")
        return

    print("\n--- Part 3a: cloudops user removal ---")
    try:
        pwd.getpwnam("cloudops")
    except KeyError:
        print("cloudops user already doesn't exist -- nothing to do.")
    else:
        ps = subprocess.run(["pgrep", "-u", "cloudops"], capture_output=True, text=True)
        if ps.stdout.strip():
            print(f"cloudops still has running processes (PIDs: {ps.stdout.strip().splitlines()}) "
                  f"-- NOT removing the user. Investigate before retrying.")
        else:
            print("No processes running as cloudops -- safe to remove.")
            if apply_:
                result = subprocess.run(["userdel", "cloudops"], capture_output=True, text=True)
                if result.returncode == 0:
                    print("Removed cloudops user.")
                else:
                    print(f"WARNING: userdel failed: {result.stderr.strip()}")
            else:
                print("[dry-run] would run: userdel cloudops")

    print("\n--- Part 3b: archive stray .bak.TIMESTAMP files from THIS session's scripts ---")
    import re
    # Only the exact pattern this session's scripts produce:
    # <name>.bak.YYYYMMDD_HHMMSS or <name>.deleted.YYYYMMDD_HHMMSS.
    # Deliberately does NOT match older, meaningfully-named backups like
    # "scheduler.py.bak.pre-shutdown-partition-fix" -- those predate this
    # session and clearly weren't meant as disposable clutter.
    TIMESTAMP_PATTERN = re.compile(r"\.(bak|deleted)\.\d{8}_\d{6}$")
    archive_dir = os.path.join(repo_root, "scripts", "archive", "session_backups")
    bak_files = []
    for fname in os.listdir(repo_root):
        fpath = os.path.join(repo_root, fname)
        if os.path.isfile(fpath) and TIMESTAMP_PATTERN.search(fname):
            bak_files.append(fpath)
    for dirpath, _, filenames in os.walk(os.path.join(repo_root, "app")):
        for fname in filenames:
            if TIMESTAMP_PATTERN.search(fname):
                bak_files.append(os.path.join(dirpath, fname))

    if not bak_files:
        print("No stray .bak/.deleted files found -- nothing to archive.")
        return

    print(f"Found {len(bak_files)} backup file(s):")
    for f in bak_files:
        print(f"  {f}")

    if not apply_:
        print(f"[dry-run] would move these into {archive_dir}/")
        return

    os.makedirs(archive_dir, exist_ok=True)
    for f in bak_files:
        dest = os.path.join(archive_dir, os.path.basename(f) + f".from_{os.path.relpath(os.path.dirname(f), repo_root).replace('/', '_')}")
        shutil.move(f, dest)
    print(f"Moved {len(bak_files)} file(s) into {archive_dir}/ "
          f"(not committed to git automatically -- review and `git add` if you want them kept in history, "
          f"or just leave them there / delete the folder, since git itself is the real backup).")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="(default; kept for backward compatibility)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    repo_root = find_repo_root()
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    describe_path = os.path.join(repo_root, "app", "aws", "describe_polling.py")
    collector_path = os.path.join(repo_root, "app", "aws", "collector_direct.py")

    describe_content, describe_note = prepare_patch(
        describe_path, "app/aws/describe_polling.py",
        [
            (DESCRIBE_HEADER_OLD, DESCRIBE_HEADER_NEW),
            (EC2_QUERY_OLD, EC2_QUERY_NEW),
            (POLL_EC2_OLD, POLL_EC2_NEW),
        ],
        "write_metrics_batch",
    )
    collector_content, collector_note = prepare_patch(
        collector_path, "app/aws/collector_direct.py",
        [(COLLECTOR_STUB_OLD, COLLECTOR_STUB_NEW), (COLLECTOR_IMPORT_OLD, COLLECTOR_IMPORT_NEW)],
        "vm_client fully retired from THIS file",
    )

    print("\nPart 1 file patch plan:")
    print(f"  {describe_note}")
    print(f"  {collector_note}")

    if not apply_:
        print("\n[dry-run] Part 1: no files written.")
        part3_ops_cleanup(repo_root, apply_=False)
        print("\n[dry-run overall] Re-run with --apply (or no flags) to make real changes.")
        return

    if describe_content is not None:
        backup(describe_path)
        with open(describe_path, "w", encoding="utf-8") as fh:
            fh.write(describe_content)
        print(f"Patched app/aws/describe_polling.py")
    if collector_content is not None:
        backup(collector_path)
        with open(collector_path, "w", encoding="utf-8") as fh:
            fh.write(collector_content)
        print(f"Patched app/aws/collector_direct.py")

    part3_ops_cleanup(repo_root, apply_=True)

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 40 --no-pager

  B) Confirm StatusCheckFailed thresholds no longer trigger a real
     CloudWatch call from "Check Thresholds Now" -- watch for a GMD
     query log line mentioning StatusCheckFailed specifically; there
     should be none after the next poll cycle populates `metrics`:
       sudo journalctl -u monitoring-hub --since "-5min" --no-pager | grep -i statuscheck

  C) Confirm the describe-polling loop is still pushing to VM AND now
     also writing locally (dual-write, no behavior change for any
     external Grafana dashboard):
       mysql -u monitor -p monitoring_hub -e \\
         "SELECT r.resource_id, m.metric_value, m.metric_timestamp
          FROM metrics m JOIN resources r ON r.id = m.resource_id
          WHERE r.resource_type='ec2' AND m.metric_name='statuscheckfailed'
          LIMIT 5;"

  D) Review, commit, push:
       git status
       git diff app/aws/describe_polling.py app/aws/collector_direct.py
       git add app/aws/describe_polling.py app/aws/collector_direct.py apply_final_cleanup.py
       git commit -m "fix(alerts): correct a bug in Phase 5's own patch -- StatusCheckFailed was silently falling through to a real billed CloudWatch call instead of the free describe path; describe_polling.py now also writes locally so check_and_write_alerts() can read it correctly. Confirmed poll_alb_target_health() should stay VM-only (external Grafana consumer, no local resource type exists for target groups). Removed cloudops OS user and archived accumulated session .bak files."
       git push origin main

  This is genuinely the end of the VM-removal work started this
  session, as far as this codebase's own internal consumers go.
  vm_client.py's one remaining legitimate purpose (ALB target-group
  health for an external Grafana dashboard) is now clearly documented,
  not a mystery -- whether to eventually also migrate THAT is a product
  decision (does an external Grafana dashboard actually still get used?),
  not a code-correctness one.
""")


if __name__ == "__main__":
    main()
