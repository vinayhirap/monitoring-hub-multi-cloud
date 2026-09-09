#!/usr/bin/env python3
"""
apply_fix_alb_healthy_hosts_history.py
========================================
Fixes a bug in the previous fix (apply_fix_alb_healthy_hosts.py). That
script correctly computes and writes healthy/unhealthy host counts --
confirmed live, real data was in the database. But it only called
write_metrics_batch(), which writes to the `metrics` LAST-VALUE cache
(what alert_evaluator.py / check_and_write_alerts read). The Services
page chart reads from a DIFFERENT table entirely --
_metric_history_query_range() queries `metric_history`, the time-series
table -- which never received these rows. Confirmed by reading both
functions again: write_metrics_batch() upserts into `metrics` only;
write_metric_history_batch() is the one that inserts into
`metric_history`, and poll_alb_target_health() never called it.

Net effect of the bug: the DB proof looked completely correct (because
it was -- the `metrics` write genuinely worked), but the chart kept
showing "No data in last 6H" because it was reading from the one table
that was never written to.

THE FIX
---------
poll_alb_target_health() now ALSO calls write_metric_history_batch()
with the same per-LB aggregated healthy/unhealthy sums, timestamped
with the actual poll time -- a real dual-write (both tables), not a
replacement. `metrics` keeps working for alert-checking exactly as
before; `metric_history` now also gets a row every poll cycle, which is
what the chart actually needs.

TESTED: re-ran the same mocked two-target-groups-on-one-LB scenario
from the previous fix's test, extended to also assert on the
metric_history write -- confirmed both write_metrics_batch (latest
value, for alerts) AND write_metric_history_batch (timestamped row, for
the chart) receive the correct aggregated sums.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_alb_healthy_hosts_history.py --dry-run
    python3 apply_fix_alb_healthy_hosts_history.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

IMPORT_OLD = "from app.collector.metrics_writer import write_metrics_batch"

IMPORT_NEW = "from datetime import datetime\nfrom app.collector.metrics_writer import write_metrics_batch, write_metric_history_batch"

WRITE_OLD = '''            if lb_totals:
                resource_ids_by_arn = _elb_resource_db_ids_by_arn(account_db_id)
                local_rows = []
                for lb_arn, (healthy_sum, unhealthy_sum) in lb_totals.items():
                    resource_db_id = resource_ids_by_arn.get(lb_arn)
                    if resource_db_id is None:
                        continue
                    local_rows.append((resource_db_id, "healthyhosts_describe", float(healthy_sum)))
                    local_rows.append((resource_db_id, "unhealthyhosts_describe", float(unhealthy_sum)))
                if local_rows:
                    write_metrics_batch(local_rows)'''

WRITE_NEW = '''            if lb_totals:
                resource_ids_by_arn = _elb_resource_db_ids_by_arn(account_db_id)
                local_rows = []
                history_rows = []
                now = datetime.utcnow()
                for lb_arn, (healthy_sum, unhealthy_sum) in lb_totals.items():
                    resource_db_id = resource_ids_by_arn.get(lb_arn)
                    if resource_db_id is None:
                        continue
                    local_rows.append((resource_db_id, "healthyhosts_describe", float(healthy_sum)))
                    local_rows.append((resource_db_id, "unhealthyhosts_describe", float(unhealthy_sum)))
                    # Bug fix (apply_fix_alb_healthy_hosts_history.py):
                    # write_metrics_batch() alone only updates the `metrics`
                    # last-value cache -- the Services page chart reads from
                    # `metric_history` instead (_metric_history_query_range),
                    # which never received these rows before this fix, so
                    # the chart kept showing "No data" despite the DB write
                    # being genuinely correct. This is a real dual-write, not
                    # a replacement -- `metrics` still gets updated the same
                    # way for alert-checking.
                    history_rows.append((resource_db_id, "healthyhosts_describe", float(healthy_sum), now))
                    history_rows.append((resource_db_id, "unhealthyhosts_describe", float(unhealthy_sum), now))
                if local_rows:
                    write_metrics_batch(local_rows)
                if history_rows:
                    write_metric_history_batch(history_rows)'''


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
    parser.add_argument("--apply", action="store_true", help="(default; kept for backward compatibility)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    repo_root = find_repo_root()
    path = os.path.join(repo_root, "app", "aws", "describe_polling.py")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    with open(path) as f:
        content = f.read()

    if "write_metric_history_batch" in content:
        print("\nAlready patched -- nothing to do.")
        return

    if content.count(IMPORT_OLD) != 1:
        die(f"Import line not found exactly once -- file may differ from what this script expects.")
    if content.count(WRITE_OLD) != 1:
        die(f"Write block not found exactly once -- file may differ from what this script expects.")

    new_content = content.replace(IMPORT_OLD, IMPORT_NEW, 1).replace(WRITE_OLD, WRITE_NEW, 1)
    print(f"\nFile patch plan:\n  app/aws/describe_polling.py: OK ({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w") as f:
        f.write(new_content)
    print("Patched app/aws/describe_polling.py")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub

  B) Wait for a describe_polling cycle (~1-2 min), then check:
       mysql -u monitor -p monitoring_hub -e \\
         "SELECT r.resource_id, mh.metric_name, mh.metric_value, mh.metric_timestamp
          FROM metric_history mh JOIN resources r ON r.id = mh.resource_id
          WHERE r.resource_type='elb' AND mh.metric_name IN ('healthyhosts_describe','unhealthyhosts_describe')
          ORDER BY mh.metric_timestamp DESC LIMIT 10;"
     Should show real rows now (previously empty for this table).

  C) Refresh the Load Balancer detail page in the UI -- Healthy Hosts /
     Unhealthy Hosts charts should now show real data instead of
     "No data in last 6H".

  D) Review, commit, push:
       git diff app/aws/describe_polling.py
       git add app/aws/describe_polling.py apply_fix_alb_healthy_hosts_history.py
       git commit -m "fix(alb): previous healthy-hosts fix only wrote to the metrics last-value cache; the chart reads from metric_history, a different table, which never received these rows. Real dual-write now."
       git push origin main
""")


if __name__ == "__main__":
    main()
