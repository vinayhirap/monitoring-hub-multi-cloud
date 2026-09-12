#!/usr/bin/env python3
"""
apply_fix_alb_nlb_backfill_robustness.py
========================================
Two related fixes, needed together before recommending update.sh as the
path to bring a far-behind production server (running the old
VictoriaMetrics/YACE architecture) up to current main.

1. CRITICAL BUG FOUND WHILE PREPARING update.sh FOR THIS: re-ran
   apply_fix_alb_nlb_threshold_resource_type.py against current main and
   found it hard-aborts with a fatal error (exit code 1) BEFORE its own
   backfill step ever runs. Root cause: its "already applied?" check
   looks for literal code that used to live in app/api/settings.py, but
   a LATER fix (apply_fix_threshold_resource_type_everywhere.py) moved
   that logic out to app/threshold_defaults.py entirely -- so on current
   main, the exact-text match this script relies on no longer exists at
   all, and it dies before reaching the one-time data backfill
   (`UPDATE thresholds SET resource_type='elb' WHERE resource_type IN
   ('alb','nlb')`) that's still genuinely needed on ANY server that
   might have pre-existing bad rows. Confirmed by actually running it:
   crashed with "[ABORT] ... found 0" and never touched the database.
   This would have made update.sh's run_migration call for this script
   silently do nothing useful on every future run, on every server --
   exactly the kind of gap update.sh's own migrate.py was built to catch
   for .sql files, but this is a Python apply_*.py script instead.

   FIXED: the backfill now runs FIRST, unconditionally, before the
   (now-obsolete) code-patch attempt. A mismatch in the code-patch part
   is now treated as "the code moved on, nothing to patch here anymore"
   -- printed and handled gracefully -- instead of a fatal abort.
   Confirmed with a real subprocess test: exit code 0, and the backfill
   logic genuinely invokes mysql with the right connection args even
   when the code-patch text no longer matches anything.

2. Added a new line to deploy/update.sh's existing run_migration list
   for this now-fixed script, positioned right after the Phase 1-3
   VictoriaMetrics-removal migrations (which it logically depends on --
   ALB/NLB thresholds only matter once AWS metrics are being collected
   directly at all). This is the ONE genuinely necessary addition needed
   for update.sh to fully bring a far-behind server up to current main
   -- every other fix from this session is either pure code (covered by
   `git pull`) or a metric_catalog data addition (covered by update.sh's
   EXISTING, already-idempotent `scripts/seed_metric_catalog.py` call,
   confirmed by reading it: uses INSERT ... ON DUPLICATE KEY UPDATE).

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_alb_nlb_backfill_robustness.py --dry-run
    python3 apply_fix_alb_nlb_backfill_robustness.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

MAIN_OLD = '''def main():
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
    print(f"\\nFile patch plan:\\n  {note}")

    backfill_existing_rows(dry_run=not apply_)

    if content is None:
        print("\\nNothing to patch in app/api/settings.py (already applied).")
        if not apply_:
            print("[dry-run] Re-run with --apply to run the backfill for real.")
        return

    if not apply_:
        print("\\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"Patched app/api/settings.py")'''

MAIN_NEW = '''def main():
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

    # CORRECTED: this script is meant to be safely re-runnable forever
    # (deploy/update.sh calls it on every deploy, on every server, for
    # exactly this reason -- the one-time data backfill below needs to
    # run on ANY server that might still have pre-existing bad
    # 'alb'/'nlb' rows, regardless of how far its code has moved on).
    # It was NOT actually safe: main() called prepare_patch() (which
    # matches exact literal text in settings.py) BEFORE the backfill,
    # and later work (apply_fix_threshold_resource_type_everywhere.py)
    # moved this normalization logic out of settings.py entirely --
    # meaning prepare_patch() now hard-ABORTS via die()/sys.exit(1) on
    # current main, and the backfill after it never even runs. Running
    # this from update.sh on a fully-updated server would have silently
    # skipped the one thing it actually needed to do. Fixed by running
    # the backfill FIRST and unconditionally, and treating a
    # prepare_patch() mismatch as "the code has moved on, nothing to
    # patch here anymore" instead of a fatal error.
    backfill_existing_rows(dry_run=not apply_)

    try:
        content, note = prepare_patch(
            path, "app/api/settings.py",
            [
                (SETTINGS_IMPORT_OLD, SETTINGS_IMPORT_NEW),
                (UPSERT_OLD, UPSERT_NEW),
                (SEED_OLD, SEED_NEW),
            ],
            "_THRESHOLD_RESOURCE_TYPE_ALIASES",
        )
    except SystemExit:
        print("\\napp/api/settings.py: code has moved on since this script was written "
              "(the normalization logic now lives elsewhere, e.g. app/threshold_defaults.py) "
              "-- nothing to patch here anymore. The backfill above is what actually matters "
              "on repeat runs; this is expected, not an error.")
        return
    print(f"\\nFile patch plan:\\n  {note}")

    if content is None:
        print("\\nNothing to patch in app/api/settings.py (already applied).")
        if not apply_:
            print("[dry-run] Re-run with --apply to run the backfill for real.")
        return

    if not apply_:
        print("\\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"Patched app/api/settings.py")'''

UPDATE_SH_OLD = '''run_migration apply_gcp_direct_metrics_fetch.py \\
    "Phase 3 of removing VictoriaMetrics: direct GCP Cloud Monitoring fetch + compute_instance numeric-ID resource-matching fix"
'''

UPDATE_SH_NEW = '''run_migration apply_gcp_direct_metrics_fetch.py \\
    "Phase 3 of removing VictoriaMetrics: direct GCP Cloud Monitoring fetch + compute_instance numeric-ID resource-matching fix"
run_migration apply_fix_alb_nlb_threshold_resource_type.py \\
    "ALB/NLB thresholds.resource_type data backfill -- one-time cleanup for any pre-existing 'alb'/'nlb' rows (should be 'elb', matching resources.resource_type) left over from before this fix existed. The code portion is already in main and this script correctly no-ops on it; only the backfill UPDATE actually matters here, and it runs unconditionally every time regardless of whether the code patch was already applied."
'''


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
    script_path = os.path.join(repo_root, "apply_fix_alb_nlb_threshold_resource_type.py")
    update_sh_path = os.path.join(repo_root, "deploy", "update.sh")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    with open(script_path) as f:
        script_content = f.read()
    script_done = "code has moved on since this script was written" in script_content
    if script_done:
        script_new = None
        script_note = "apply_fix_alb_nlb_threshold_resource_type.py: already patched -- skipping"
    else:
        if script_content.count(MAIN_OLD) != 1:
            die("apply_fix_alb_nlb_threshold_resource_type.py: anchor not found -- file may differ from what this script expects.")
        script_new = script_content.replace(MAIN_OLD, MAIN_NEW, 1)
        script_note = f"apply_fix_alb_nlb_threshold_resource_type.py: OK ({len(script_new) - len(script_content):+d} bytes)"

    with open(update_sh_path) as f:
        sh_content = f.read()
    sh_done = "apply_fix_alb_nlb_threshold_resource_type.py" in sh_content
    if sh_done:
        sh_new = None
        sh_note = "deploy/update.sh: already patched -- skipping"
    else:
        if sh_content.count(UPDATE_SH_OLD) != 1:
            die("deploy/update.sh: anchor not found -- file may differ from what this script expects.")
        sh_new = sh_content.replace(UPDATE_SH_OLD, UPDATE_SH_NEW, 1)
        sh_note = f"deploy/update.sh: OK ({len(sh_new) - len(sh_content):+d} bytes)"

    print(f"\nFile patch plan:\n  {script_note}\n  {sh_note}")

    if script_new is None and sh_new is None:
        print("\nNothing to do.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    if script_new is not None:
        backup(script_path)
        with open(script_path, "w") as f:
            f.write(script_new)
        print("Patched apply_fix_alb_nlb_threshold_resource_type.py")
    if sh_new is not None:
        backup(update_sh_path)
        with open(update_sh_path, "w") as f:
            f.write(sh_new)
        print("Patched deploy/update.sh")

    print("""
[Manual follow-up]

  A) Sanity-check the shell script still parses:
       bash -n deploy/update.sh && echo OK

  B) Confirm the backfill script now runs cleanly against current main:
       python3 apply_fix_alb_nlb_threshold_resource_type.py --dry-run
     Should print a real backfill row-count check (not "[ABORT]"), and
     a graceful "code has moved on" note -- NOT a crash.

  C) Review, commit, push -- this needs to land on the SAME server
     (13.200.102.131) this whole session has been working against, so
     that a `git pull` on ANY other server (including
     35.154.149.94) picks it up:
       git diff apply_fix_alb_nlb_threshold_resource_type.py deploy/update.sh
       git add apply_fix_alb_nlb_threshold_resource_type.py deploy/update.sh apply_fix_alb_nlb_backfill_robustness.py
       git commit -m "fix(deploy): ALB/NLB backfill script hard-aborted before its own backfill ran, against current main -- fixed to run the backfill first and unconditionally. Added to update.sh's migration list so any far-behind server gets this data cleanup automatically."
       git push origin main

  D) THIS enables the next step: bringing 35.154.149.94 up to date via
     update.sh -- see the accompanying pre-flight checklist for that
     server before running it there.
""")


if __name__ == "__main__":
    main()
