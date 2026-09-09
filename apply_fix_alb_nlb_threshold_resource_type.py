#!/usr/bin/env python3
"""
apply_fix_alb_nlb_threshold_resource_type.py
========================================
Fixes what is likely the highest-impact bug found this entire session:
Application/Network Load Balancer alert thresholds have probably NEVER
fired via app/collector/alert_evaluator.py -- the primary, always-
running, every-5-minutes scheduled evaluator every other fix this
session fed correct data into -- for as long as this deployment has
existed, for any account.

HOW THIS WAS FOUND, AND WHY IT'S NOT SPECULATION
----------------------------------------------------
While auditing alert_evaluator.py itself (everything else this session
fixed only fed data INTO it; it had never been directly audited), its
core query joins thresholds to resources with:
    JOIN thresholds t ON t.resource_type = r.resource_type
This is on the critical path of EVERY scheduled evaluation cycle, not
an occasional manual check.

Traced where thresholds.resource_type actually gets its value, end to
end, not assumed:
  - app/api/settings.py's POST /api/settings/thresholds (manual save)
    and POST /api/settings/thresholds/seed (the "seed default
    thresholds" button) are the ONLY two places that ever write
    thresholds.resource_type.
  - seed_default_thresholds() writes it directly from
    metric_catalog.service (`m["service"]`).
  - app/aws/metric_catalog_data.py confirms metric_catalog.service for
    Application/Network Load Balancer metrics is literally "alb" / "nlb"
    (two DIFFERENT catalog entries).
  - app/collector/discovery/runner.py confirms resources.resource_type
    for ALL Elastic Load Balancing v2 resources (both ALB and NLB --
    the AWS API doesn't distinguish them at discovery time in this
    codebase) is uniformly "elb".
  - So thresholds.resource_type ends up "alb" or "nlb", but
    resources.resource_type is always "elb" -- t.resource_type =
    r.resource_type can NEVER be true for either. Confirmed by reading
    every line involved, not inferred from behavior.

This is the SAME semantic mismatch Phase 5 (apply_check_thresholds_local_metrics.py)
already found and fixed for check_and_write_alerts() (the manual "Check
Thresholds Now" button) via an explicit LOCAL_RESOURCE_TYPE = {"alb":
"elb"} map -- but Phase 5 only fixed that ONE function. It never
occurred to check whether the SAME mismatch existed in the scheduled
evaluator too, because the scheduled evaluator wasn't part of this
session's VM-removal scope at the time. It is the same bug, in a
much more consequential place.

THE FIX
---------
1. app/api/settings.py: both write sites (upsert_threshold,
   seed_default_thresholds) now normalize resource_type through a
   small explicit alias map ({"alb": "elb", "nlb": "elb"}) before
   writing, matching what alert_evaluator.py's core JOIN actually
   needs. metric_catalog.service itself is UNCHANGED -- "alb"/"nlb"
   remain the correct catalog/display/UI values; only the value
   PERSISTED into thresholds.resource_type changes.
2. upsert_threshold's ON DUPLICATE KEY UPDATE now also updates
   resource_type on conflict (it didn't before), so re-saving an
   existing threshold self-heals a stale value instead of leaving it
   wrong forever.
3. A one-time backfill: existing thresholds rows already stored with
   resource_type IN ('alb','nlb') are updated to 'elb' -- without this,
   any ALB/NLB threshold anyone has ever configured stays permanently
   broken even after the write-path fix, since nothing re-writes
   resource_type for a row that already exists otherwise.
4. Confirmed this does NOT need any change in
   check_and_write_alerts() (Phase 5): that function already resolves
   `svc` from `t.get("service") or t.get("resource_type")`, preferring
   service (== metric_catalog.service, always "alb"/"nlb", unaffected
   by this fix) -- its own LOCAL_RESOURCE_TYPE map already handles the
   svc->resource_type translation independently. Confirmed by reading
   that function's code again, not assumed just because it seemed
   likely.
5. Confirmed the frontend (Settings.jsx) also prefers `t.service` over
   `t.resource_type` everywhere it groups/displays thresholds by
   service -- this fix is invisible to the UI, no frontend change
   needed.

TESTED: the normalization logic was extracted as a small pure function
and unit-tested directly (values that need remapping, values that
don't, case sensitivity). NOT tested: an actual live threshold
save/seed against a real database, or a live 5-minute evaluator cycle
picking up a newly-fixed ALB threshold -- no server/DB access available
here; verify per the checklist below, ideally by configuring an
obviously-already-breached ALB threshold and confirming a real alert
appears after the next standard-tier cycle, not just after a manual
check.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_alb_nlb_threshold_resource_type.py --dry-run
    python3 apply_fix_alb_nlb_threshold_resource_type.py --apply
The backfill step needs DB credentials from .env -- run as whichever
user can read it directly (per this session's earlier .env ownership
fix, that should now be your normal login user, no sudo needed).
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime

SETTINGS_IMPORT_OLD = '''from app.threshold_defaults import DEFAULT_THRESHOLDS, FALLBACK_THRESHOLD
import datetime, json, logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["Settings"])'''

SETTINGS_IMPORT_NEW = '''from app.threshold_defaults import DEFAULT_THRESHOLDS, FALLBACK_THRESHOLD
import datetime, json, logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["Settings"])

# metric_catalog.service ("alb", "nlb") is the correct catalog/display
# value and is NOT changed by this map -- but
# app/collector/discovery/runner.py stores ALL Elastic Load Balancing v2
# resources (both ALB and NLB; this codebase doesn't distinguish them at
# discovery time) under resources.resource_type = "elb" uniformly.
# alert_evaluator.py's core scheduled evaluation JOINs
# thresholds.resource_type directly against resources.resource_type with
# no service-name fallback (unlike check_and_write_alerts() /
# app/aws/collector_direct.py, which already has its own separate
# LOCAL_RESOURCE_TYPE map for this same translation -- see Phase 5,
# apply_check_thresholds_local_metrics.py). Without this normalization,
# ALB/NLB thresholds are silently unevaluable by the scheduled evaluator
# forever, no matter what value they're set to. See
# apply_fix_alb_nlb_threshold_resource_type.py for the full story.
_THRESHOLD_RESOURCE_TYPE_ALIASES = {"alb": "elb", "nlb": "elb"}


def _normalize_threshold_resource_type(value):
    return _THRESHOLD_RESOURCE_TYPE_ALIASES.get(value, value)'''

UPSERT_OLD = '''    account_id     = int(payload.get("account_id", 3))
    metric_id      = payload["metric_id"]
    resource_type  = payload.get("resource_type", "ec2")
    warning_value  = float(payload["warning_value"])
    critical_value = float(payload["critical_value"])
    comparison     = payload.get("comparison", ">")
    eval_period    = int(payload.get("evaluation_period", 5))
    enabled        = int(payload.get("enabled", 1))

    conn = get_connection(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO thresholds
          (aws_account_id, resource_type, metric_id, warning_value,
           critical_value, comparison, evaluation_period, enabled)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          warning_value     = VALUES(warning_value),
          critical_value    = VALUES(critical_value),
          comparison        = VALUES(comparison),
          evaluation_period = VALUES(evaluation_period),
          enabled           = VALUES(enabled)
    """, (account_id, resource_type, metric_id, warning_value,
          critical_value, comparison, eval_period, enabled))'''

UPSERT_NEW = '''    account_id     = int(payload.get("account_id", 3))
    metric_id      = payload["metric_id"]
    resource_type  = _normalize_threshold_resource_type(payload.get("resource_type", "ec2"))
    warning_value  = float(payload["warning_value"])
    critical_value = float(payload["critical_value"])
    comparison     = payload.get("comparison", ">")
    eval_period    = int(payload.get("evaluation_period", 5))
    enabled        = int(payload.get("enabled", 1))

    conn = get_connection(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO thresholds
          (aws_account_id, resource_type, metric_id, warning_value,
           critical_value, comparison, evaluation_period, enabled)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          resource_type     = VALUES(resource_type),
          warning_value     = VALUES(warning_value),
          critical_value    = VALUES(critical_value),
          comparison        = VALUES(comparison),
          evaluation_period = VALUES(evaluation_period),
          enabled           = VALUES(enabled)
    """, (account_id, resource_type, metric_id, warning_value,
          critical_value, comparison, eval_period, enabled))'''

SEED_OLD = '''        warn, crit, comp = DEFAULT_THRESHOLDS.get(m["metric_name"], FALLBACK_THRESHOLD)
        try:
            cur.execute("""
                INSERT IGNORE INTO thresholds
                  (aws_account_id, resource_type, metric_id,
                   warning_value, critical_value, comparison, evaluation_period, enabled)
                VALUES (%s,%s,%s,%s,%s,%s,5,1)
            """, (account_id, m["service"], m["id"], warn, crit, comp))'''

SEED_NEW = '''        warn, crit, comp = DEFAULT_THRESHOLDS.get(m["metric_name"], FALLBACK_THRESHOLD)
        try:
            cur.execute("""
                INSERT IGNORE INTO thresholds
                  (aws_account_id, resource_type, metric_id,
                   warning_value, critical_value, comparison, evaluation_period, enabled)
                VALUES (%s,%s,%s,%s,%s,%s,5,1)
            """, (account_id, _normalize_threshold_resource_type(m["service"]), m["id"], warn, crit, comp))'''


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


def _load_db_password():
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        "/opt/monitoring-hub/app/.env",
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DB_PASSWORD="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def backfill_existing_rows(dry_run):
    db_pass = _load_db_password()
    db_host = os.environ.get("DB_HOST", "127.0.0.1")
    db_user = os.environ.get("DB_USER", "monitor")
    db_name = os.environ.get("DB_NAME", "monitoring_hub")
    cmd = ["mysql", f"-u{db_user}", "-h", db_host, "-N", "-B"]
    if db_pass:
        cmd.append(f"-p{db_pass}")
    cmd.append(db_name)

    try:
        count_result = subprocess.run(
            cmd, input="SELECT COUNT(*) FROM thresholds WHERE resource_type IN ('alb','nlb');",
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        print("\n[WARNING] `mysql` client not found on PATH -- skipping the backfill check/run. "
              "Run this manually once mysql is available:\n"
              "  mysql -u monitor -p monitoring_hub -e \"UPDATE thresholds SET resource_type='elb' WHERE resource_type IN ('alb','nlb');\"")
        return
    if count_result.returncode != 0:
        print(f"[WARNING] Could not check for existing alb/nlb threshold rows: {count_result.stderr}")
        return
    try:
        count = int(count_result.stdout.strip())
    except ValueError:
        print(f"[WARNING] Unexpected output checking existing rows: {count_result.stdout!r}")
        return

    print(f"\nExisting thresholds rows with resource_type IN ('alb','nlb'): {count}")
    if count == 0:
        print("Nothing to backfill.")
        return

    if dry_run:
        print("[DRY-RUN] would run: UPDATE thresholds SET resource_type='elb' WHERE resource_type IN ('alb','nlb');")
        return

    update_result = subprocess.run(
        cmd, input="UPDATE thresholds SET resource_type='elb' WHERE resource_type IN ('alb','nlb');",
        capture_output=True, text=True,
    )
    if update_result.returncode != 0:
        print(f"[WARNING] Backfill UPDATE failed: {update_result.stderr}")
    else:
        print(f"Backfilled {count} existing threshold row(s) to resource_type='elb'.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="(default; kept for backward compatibility)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    repo_root = find_repo_root()
    path = os.path.join(repo_root, "app", "api", "settings.py")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    content, note = prepare_patch(
        path, "app/api/settings.py",
        [
            (SETTINGS_IMPORT_OLD, SETTINGS_IMPORT_NEW),
            (UPSERT_OLD, UPSERT_NEW),
            (SEED_OLD, SEED_NEW),
        ],
        "_THRESHOLD_RESOURCE_TYPE_ALIASES",
    )
    print(f"\nFile patch plan:\n  {note}")

    backfill_existing_rows(dry_run=not apply_)

    if content is None:
        print("\nNothing to patch in app/api/settings.py (already applied).")
        if not apply_:
            print("[dry-run] Re-run with --apply to run the backfill for real.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"Patched app/api/settings.py")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub
       sudo journalctl -u monitoring-hub -n 40 --no-pager

  B) THE REAL TEST -- this is the important one. Configure (or find an
     existing) ALB threshold for a metric you know is already breached
     (e.g. set warning_value very low on RequestCount, or use a load
     balancer you know has unhealthy hosts on HealthyHostCount), save
     it via Settings, and WAIT for the next standard-tier scheduled
     cycle (~5 min, not a manual "Check Now" click) -- confirm a REAL
     alert appears in Alerts without you manually triggering anything:
       sudo journalctl -u monitoring-hub --since "-6min" --no-pager | grep -i "alert evaluation complete"
     Compare "new:" count before and after -- it should go from 0 to 1
     (or more) once a real ALB breach is configured, which was
     structurally impossible before this fix.

  C) Confirm the backfill worked, if it found any rows:
       mysql -u monitor -p monitoring_hub -e \\
         "SELECT resource_type, COUNT(*) FROM thresholds GROUP BY resource_type;"
     Should show no 'alb' or 'nlb' rows anymore -- only 'elb'.

  D) Review, commit, push:
       git status
       git diff app/api/settings.py
       git add app/api/settings.py apply_fix_alb_nlb_threshold_resource_type.py
       git commit -m "fix(alerts): ALB/NLB thresholds could never fire via the scheduled alert_evaluator.py -- thresholds.resource_type was written as 'alb'/'nlb' (metric_catalog's service key) but resources.resource_type is uniformly 'elb', and the scheduled evaluator's core JOIN has no service-name fallback (unlike check_and_write_alerts(), which Phase 5 already fixed separately for this same mismatch). Normalized at both write sites, backfilled existing rows, self-heals on re-save."
       git push origin main

  This is likely the highest-impact bug found this entire session --
  unlike everything else, which affected specific edge cases or manual
  actions, this one silently disabled ALB/NLB alerting via the PRIMARY,
  always-running evaluation path, for every account, since this
  seeding mechanism was introduced.
""")


if __name__ == "__main__":
    main()
