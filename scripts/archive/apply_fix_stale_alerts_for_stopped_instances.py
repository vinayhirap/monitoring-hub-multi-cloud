#!/usr/bin/env python3
"""
apply_fix_stale_alerts_for_stopped_instances.py
========================================
Fixes a real, user-reported bug: EC2 instances that have been stopped
still show a permanent CRITICAL/WARNING badge in the resource list and
account health summary, with frozen, stale metric values from before
they were stopped -- because nothing ever resolves their alerts once
the instance stops sending data.

WHY THIS IS SAFE AND NOT THE SAME BUG THAT WAS ALREADY REVERTED
--------------------------------------------------------------------
alert_evaluator.py's _auto_resolve_stale_alerts() already documents,
carefully, why it does NOT resolve alerts purely because metrics stop
flowing for a still-discovered, still-active resource: that was tried
once before (db/migrations/008_revert_falsely_resolved_alerts.sql) and
reverted, because "no data" is ambiguous -- it could mean the resource
is fine and just quiet, or it could mean the collector/VictoriaMetrics
itself is down while the resource is still actively breaching. Silently
resolving on ambiguous silence hides real, ongoing problems.

A STOPPED (or terminated) EC2 instance is NOT that ambiguous case. This
codebase already tracks instance_state as a definitively-known fact
(app/collector/discovery/runner.py updates it every discovery cycle
from AWS's own DescribeInstances state, and
app/aws/describe_polling.py already filters on it for its own free
polling loop). A stopped instance CANNOT be generating real CPU/network/
disk traffic -- there is no scenario where "instance_state=stopped" is
compatible with "still actually breaching a live metric threshold." This
is the same category of definitive-fact auto-resolve the function
already does for "account removed" and "resource not in `resources` at
all" -- just a third, equally definitive case, not a return to the
reverted "silence = resolved" heuristic.

THE FIX
---------
Adds a third case to _auto_resolve_stale_alerts(): any active alert
whose resource is an EC2 instance with instance_state IN ('stopped',
'terminated') gets auto-resolved. Deliberately does NOT include
'stopping'/'shutting-down'/'pending' -- those are transitional states
where a definitive "not running" fact doesn't fully hold yet; being
conservative here costs nothing (the alert will resolve a few seconds
later once the transition completes and instance_state updates on the
next discovery cycle) and avoids any edge-case ambiguity during the
transition window itself.

Only scoped to EC2 (resource_type='ec2') -- EBS/RDS/Lambda/ELB don't
have an equivalent "definitely can't be breaching" state tracked in
this codebase today, so they're intentionally left to the existing
"account removed" / "resource doesn't exist" cases only. Extending this
to e.g. EBS volumes in 'available' (unattached) state would be a
reasonable future addition but is a separate decision, not bundled in
here.

TESTED: the new SQL case's WHERE-clause logic was validated directly
against the running schema's column names (instance_state, resource_type)
-- confirmed both already exist and are already populated by live code
paths, not new columns this script would need to add. NOT tested: an
actual live evaluator cycle auto-resolving a real stale alert for a
real stopped instance -- no server/DB access available here; verify per
the checklist below, ideally by confirming the exact CloudOps-AI-
Assistant / test instances originally reported now get resolved.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_stale_alerts_for_stopped_instances.py --dry-run
    python3 apply_fix_stale_alerts_for_stopped_instances.py --apply
(no root needed, pure repo file edit -- the fix takes effect on the
next scheduled standard-tier cycle after a restart, no manual DB
cleanup needed for the two already-reported stale alerts specifically)
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

DOCSTRING_OLD = '''def _auto_resolve_stale_alerts(cursor):
    """
    Auto-resolves alerts in exactly two SAFE cases, both meaning the thing
    being alerted on no longer exists at all -- not just "quiet":

      1. The account was removed/deactivated (unchanged from before).
      2. The specific resource has no matching row in `resources` at all
         -- i.e. not "hasn't reported recently", but literally doesn't
         exist in current discovery. This catches orphaned rows from
         before the VM/YACE migration (mistyped/legacy resource_ids that
         can never receive a fresh metric again because nothing writes
         for a resource_id discovery doesn't know about) without touching
         alerts for resources that are simply between metric readings.

    Deliberately does NOT resolve purely because metrics stopped flowing
    for a still-discovered, still-active resource (collector down,
    VictoriaMetrics outage, network blip). That was tried once already
    and reverted -- see db/migrations/008_revert_falsely_resolved_alerts.sql.
    "No data" for an existing resource is surfaced as staleness by the API
    (last_seen_at), not auto-resolved.
    """'''

DOCSTRING_NEW = '''def _auto_resolve_stale_alerts(cursor):
    """
    Auto-resolves alerts in exactly three SAFE cases, all meaning the
    thing being alerted on either no longer exists, or is in a
    definitively-known state that cannot possibly still be breaching --
    never just "quiet":

      1. The account was removed/deactivated (unchanged from before).
      2. The specific resource has no matching row in `resources` at all
         -- i.e. not "hasn't reported recently", but literally doesn't
         exist in current discovery. This catches orphaned rows from
         before the VM/YACE migration (mistyped/legacy resource_ids that
         can never receive a fresh metric again because nothing writes
         for a resource_id discovery doesn't know about) without touching
         alerts for resources that are simply between metric readings.
      3. The resource is an EC2 instance whose instance_state (tracked by
         discovery/runner.py from AWS's own DescribeInstances state, not
         inferred from silence) is 'stopped' or 'terminated'. A stopped
         instance cannot generate real CPU/network/disk traffic -- this
         is a definitive fact, not an absence-of-data guess, so it's the
         same category as cases 1/2, not a return to the reverted
         "silence = resolved" heuristic. Deliberately excludes transitional
         states (stopping/shutting-down/pending) -- see
         apply_fix_stale_alerts_for_stopped_instances.py for why.

    Deliberately does NOT resolve purely because metrics stopped flowing
    for a still-discovered, still-RUNNING resource (collector down,
    VictoriaMetrics outage, network blip). That was tried once already
    and reverted -- see db/migrations/008_revert_falsely_resolved_alerts.sql.
    "No data" for an existing, still-running resource is surfaced as
    staleness by the API (last_seen_at), not auto-resolved.
    """'''

CASE2_ANCHOR_OLD = '''    orphaned_ids = [row["id"] for row in cursor.fetchall()]
    if orphaned_ids:
        fmt = ",".join(["%s"] * len(orphaned_ids))
        cursor.execute(f"""
            UPDATE alerts
            SET status = 'resolved', resolved_at = NOW(), last_seen_at = NOW()
            WHERE id IN ({fmt})
        """, orphaned_ids)

    total = account_removed + len(orphaned_ids)
    return total, account_removed, len(orphaned_ids)'''

CASE2_ANCHOR_NEW = '''    orphaned_ids = [row["id"] for row in cursor.fetchall()]
    if orphaned_ids:
        fmt = ",".join(["%s"] * len(orphaned_ids))
        cursor.execute(f"""
            UPDATE alerts
            SET status = 'resolved', resolved_at = NOW(), last_seen_at = NOW()
            WHERE id IN ({fmt})
        """, orphaned_ids)

    # Case 3: EC2 instance definitively stopped/terminated -- cannot
    # possibly still be breaching a live metric. See this script's
    # docstring for why this is safe and NOT the reverted "silence =
    # resolved" heuristic (instance_state is a known fact from AWS's own
    # DescribeInstances response, not inferred from absent data).
    cursor.execute("""
        SELECT a.id
        FROM alerts a
        JOIN resources r
            ON r.resource_id = a.resource_id
           AND r.resource_type = 'ec2'
           AND r.instance_state IN ('stopped', 'terminated')
        WHERE a.status = 'active'
    """)
    stopped_ids = [row["id"] for row in cursor.fetchall()]
    if stopped_ids:
        fmt = ",".join(["%s"] * len(stopped_ids))
        cursor.execute(f"""
            UPDATE alerts
            SET status = 'resolved', resolved_at = NOW(), last_seen_at = NOW()
            WHERE id IN ({fmt})
        """, stopped_ids)

    total = account_removed + len(orphaned_ids) + len(stopped_ids)
    return total, account_removed, len(orphaned_ids), len(stopped_ids)'''

CALLER_OLD = '''    stale_total, stale_accounts, stale_orphans = _auto_resolve_stale_alerts(cursor)
    conn.commit()
    if stale_total:
        logger.info(
            f"Auto-resolved {stale_total} stale alert(s) "
            f"({stale_accounts} account removed, {stale_orphans} orphaned resource_id)"
        )'''

CALLER_NEW = '''    stale_total, stale_accounts, stale_orphans, stale_stopped = _auto_resolve_stale_alerts(cursor)
    conn.commit()
    if stale_total:
        logger.info(
            f"Auto-resolved {stale_total} stale alert(s) "
            f"({stale_accounts} account removed, {stale_orphans} orphaned resource_id, "
            f"{stale_stopped} stopped/terminated EC2 instance)"
        )'''


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
    parser.add_argument("--apply", action="store_true", help="(default; kept for backward compatibility)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    repo_root = find_repo_root()
    path = os.path.join(repo_root, "app", "collector", "alert_evaluator.py")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    content, note = prepare_patch(
        path, "app/collector/alert_evaluator.py",
        [
            (DOCSTRING_OLD, DOCSTRING_NEW),
            (CASE2_ANCHOR_OLD, CASE2_ANCHOR_NEW),
            (CALLER_OLD, CALLER_NEW),
        ],
        "stopped/terminated EC2 instance",
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
    print(f"Patched app/collector/alert_evaluator.py")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 40 --no-pager

  B) Confirm the exact stale alerts reported (CloudOps-AI-Assistant,
     "test" instance -- both stopped) get resolved on the very next
     standard-tier cycle (~5 min):
       sudo journalctl -u monitoring-hub --since "-6min" --no-pager | grep -i "auto-resolved"
     Should show something like:
       "Auto-resolved 2 stale alert(s) (0 account removed, 0 orphaned resource_id, 2 stopped/terminated EC2 instance)"

  C) Confirm in the UI: both instances' CRITICAL badges should clear
     from the account resource list and Active Alerts within one cycle,
     without needing a page refresh trick or manual Resolve click.

  D) Confirm this does NOT resolve alerts for genuinely RUNNING
     instances that have simply gone quiet (collector/network issue) --
     pick any currently-active alert on a running instance and confirm
     it's still there after a cycle. This is the exact case that must
     stay unresolved (the previously-reverted bug).

  E) Review, commit, push:
       git status
       git diff app/collector/alert_evaluator.py
       git add app/collector/alert_evaluator.py apply_fix_stale_alerts_for_stopped_instances.py
       git commit -m "fix(alerts): auto-resolve stale CRITICAL/WARNING alerts for EC2 instances that are definitively stopped/terminated -- previously stuck open forever with frozen pre-shutdown values, since only 'account removed' and 'resource doesn't exist' were auto-resolved before. Does not touch the deliberately-preserved behavior of leaving alerts open when a still-RUNNING resource simply goes quiet."
       git push origin main
""")


if __name__ == "__main__":
    main()
