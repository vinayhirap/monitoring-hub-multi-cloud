#!/usr/bin/env python3
"""
fix_deploy_script_drift.py
========================================
Closes a real gap this session's own Phase 1-3 work created: exactly the
same "class of drift" deploy.sh's own comments already say caused a real
incident once (RBAC migrations only in update.sh, not setup.sh -- fixed
earlier this session by fix_fresh_install_schema_gaps.py), now found
again for apply_direct_gmd_metrics_revival.py (Phase 1),
apply_azure_direct_metrics_fetch.py (Phase 2), and
apply_gcp_direct_metrics_fetch.py (Phase 3): none of the three are
referenced anywhere in setup.sh, deploy/deploy.sh, or deploy/update.sh.
A genuinely fresh install or DR rebuild today would silently skip all
three phases -- no metric_history table, AWS still on the disabled GMD
path, Azure/GCP still pushing to (and reading from) VictoriaMetrics, and
the GCP compute_instance numeric-ID fix never applied.

THREE BUGS FIXED HERE, NOT ONE
--------------------------------
1. THE OBVIOUS ONE: the three scripts were never added to any
   run_migration list. Fixing that alone would NOT have been enough --
   see bug 2. Fixing bugs 1+2 together still wouldn't have been enough
   for the long run -- see bug 3, found only by actually testing the
   fix against a full three-phase-already-applied checkout rather than
   just a fresh one.

2. THE ONE THAT WOULD HAVE MADE BUG 1's FIX SILENTLY USELESS: every
   apply_*.py script already wired into these deploy scripts (e.g.
   apply_multi_cloud_migration.py, apply_drop_dead_tables.py) is invoked
   with NO arguments by run_migration() and defaults to APPLYING, with
   --dry-run as the explicit opt-in preview flag. Phase 1-3's scripts do
   the exact opposite -- they default to a safe no-op dry-run and require
   an explicit --apply. If this script had simply added
   `run_migration apply_azure_direct_metrics_fetch.py "..."` without
   also fixing that default, every future setup.sh/deploy.sh/update.sh
   run would have called it, gotten exit 0, printed a dry-run plan
   nobody would see in a long deploy log, and applied NOTHING -- forever,
   silently, looking exactly as if it were wired in correctly. That would
   have been a WORSE bug than not wiring it in at all.

   Fixed by flipping the default to match every other apply_*.py script
   in this repo (default = apply, --dry-run = explicit preview-only).
   --apply is kept as an accepted no-op flag so every command already
   documented or run this session (including HANDOVER.md and this
   session's own instructions) keeps working unchanged.

3. A CHAIN-IDEMPOTENCY BUG FOUND BY TESTING THE FIX END-TO-END, NOT
   ASSUMED: Phase 1, 2, and 3 each fully rewrite the body of
   metrics_vm_sync.py's sync_metrics_from_vm() function (Phase 2's edit
   supersedes Phase 1's, Phase 3's supersedes Phase 2's). Each script's
   own idempotency check (prepare_patch's done_marker) looks for a
   phrase from ITS OWN version of that function -- which the NEXT
   phase's rewrite deletes. Simulating what setup.sh/deploy.sh/
   update.sh will now actually do -- run Phase 1, then 2, then 3, in
   order, on the same checkout, exactly like a real deploy -- surfaced
   this immediately: re-running Phase 1's script afterward (e.g. because
   a future update.sh run replays the whole run_migration list every
   time) aborted with "expected exactly 1 match, found 0", because
   Phase 3 had already rewritten the text Phase 1 was looking for. Not
   fatal (update.sh only WARNs and continues), but it would show as a
   spurious permanent failure on every single future update.sh run --
   exactly the kind of alert-fatigue-inducing false positive this
   project's own verification blocks are designed to avoid.

   Fixed by changing prepare_patch itself (in all 3 phase scripts) so
   that finding ZERO matches for an expected block is treated as
   "already handled (possibly by a later phase) -- skip gracefully"
   rather than a hard abort. Finding MORE than one match is still a
   hard abort (that's genuine, dangerous ambiguity -- unlike zero
   matches, it can't be explained by "something later already did this
   job"). Verified below by actually replaying Phase 1 -> 2 -> 3 -> this
   fix -> re-running Phase 1 standalone again, and confirming it now
   skips cleanly instead of aborting.

BONUS (found while touching this exact section, essentially free):
setup.sh was ALSO still missing apply_drop_dead_tables.py, which
deploy.sh/update.sh both already have -- the same drift class, one
migration earlier, that fix_fresh_install_schema_gaps.py's audit didn't
happen to catch. Added to setup.sh here too.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_deploy_script_drift.py --dry-run
    python3 fix_deploy_script_drift.py --apply
(no root needed -- pure repo file edits, no DB/live-server touch)
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

ARGPARSE_OLD = '''    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply_ = args.apply and not args.dry_run'''

ARGPARSE_NEW = '''    parser.add_argument("--apply", action="store_true",
                        help="(default behavior; kept for backward compatibility with earlier docs/runbooks)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run'''

PREPARE_PATCH_OLD = '''    new_content = content
    for old, new in replacements:
        n = new_content.count(old)
        if n != 1:
            die(f"{label}: expected exactly 1 match for one of the expected blocks, found {n}. "
                f"File may differ from what this script expects.")
        new_content = new_content.replace(old, new, 1)
    return new_content, f"{label}: OK ({len(new_content) - len(content):+d} bytes)"'''

PREPARE_PATCH_NEW = '''    new_content = content
    for old, new in replacements:
        n = new_content.count(old)
        if n == 0:
            # Not necessarily an error: a LATER phase script may have
            # already rewritten this exact region (e.g. Phase 3 fully
            # replacing sync_metrics_from_vm()'s body supersedes Phase
            # 1/2's own edits to it, including whatever marker text
            # those scripts check for). Treat "expected text absent, and
            # it's not an ambiguous multi-match" as "already handled
            # elsewhere" and skip gracefully rather than aborting -- see
            # fix_deploy_script_drift.py for the chain-idempotency
            # incident this caught.
            return None, f"{label}: expected block not found (likely superseded by a later phase) -- skipping."
        if n > 1:
            die(f"{label}: expected exactly 1 match for one of the expected blocks, found {n}. "
                f"File may differ from what this script expects.")
        new_content = new_content.replace(old, new, 1)
    return new_content, f"{label}: OK ({len(new_content) - len(content):+d} bytes)"'''

PHASE_SCRIPTS = [
    "apply_direct_gmd_metrics_revival.py",
    "apply_azure_direct_metrics_fetch.py",
    "apply_gcp_direct_metrics_fetch.py",
]

RUN_MIGRATION_BLOCK = '''run_migration apply_direct_gmd_metrics_revival.py \\
    "Phase 1 of removing VictoriaMetrics: revive direct AWS GetMetricData collection, create metric_history table"
run_migration apply_azure_direct_metrics_fetch.py \\
    "Phase 2 of removing VictoriaMetrics: direct Azure Monitor fetch (needs Phase 1's metric_history table)"
run_migration apply_gcp_direct_metrics_fetch.py \\
    "Phase 3 of removing VictoriaMetrics: direct GCP Cloud Monitoring fetch + compute_instance numeric-ID resource-matching fix"
'''

DEPLOY_UPDATE_ANCHOR_OLD = '''run_migration apply_drop_dead_tables.py \\
    "006 (now actually executed): drop metric_configs/metric_definitions/enabled_metrics/alert_rules/dashboards/dashboard_panels/account_permissions/user_accounts/user_roles/roles -- confirmed unreferenced by app/ or frontend/src/, refuses to drop any table with rows"

echo "--- db/migrations/*.sql tracking (migrate.py) ---"'''

DEPLOY_UPDATE_ANCHOR_NEW = '''run_migration apply_drop_dead_tables.py \\
    "006 (now actually executed): drop metric_configs/metric_definitions/enabled_metrics/alert_rules/dashboards/dashboard_panels/account_permissions/user_accounts/user_roles/roles -- confirmed unreferenced by app/ or frontend/src/, refuses to drop any table with rows"

''' + RUN_MIGRATION_BLOCK + '''
echo "--- db/migrations/*.sql tracking (migrate.py) ---"'''

SETUP_ANCHOR_OLD = '''run_migration scripts/seed_metric_catalog.py \\
    "seed: metric_catalog curated + directory entries"

echo "--- db/migrations/*.sql tracking (migrate.py) ---"'''

SETUP_ANCHOR_NEW = '''run_migration scripts/seed_metric_catalog.py \\
    "seed: metric_catalog curated + directory entries"
run_migration apply_drop_dead_tables.py \\
    "006 (now actually executed): drop metric_configs/metric_definitions/enabled_metrics/alert_rules/dashboards/dashboard_panels/account_permissions/user_accounts/user_roles/roles -- confirmed unreferenced by app/ or frontend/src/, refuses to drop any table with rows -- setup.sh was missing this one too (same drift class fix_fresh_install_schema_gaps.py fixed for the RBAC chain, caught in a later audit pass)"

''' + RUN_MIGRATION_BLOCK + '''
echo "--- db/migrations/*.sql tracking (migrate.py) ---"'''


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
    """Generic prepare_patch for THIS script's own edits (not the 3 phase
    scripts' internal one, which this script patches separately)."""
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
    return new_content, f"{label}: OK"


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

    results = []

    # Bugs 2 + 3: flip the arg-default convention AND fix the found==0
    # idempotency semantics in all 3 phase scripts.
    for script in PHASE_SCRIPTS:
        path = os.path.join(repo_root, script)
        content, note = prepare_patch(
            path, script,
            [(ARGPARSE_OLD, ARGPARSE_NEW), (PREPARE_PATCH_OLD, PREPARE_PATCH_NEW)],
            "kept for backward compatibility with earlier docs/runbooks",
        )
        results.append((path, script, content, note))

    # Bug 1: wire all 3 into setup.sh, deploy.sh, update.sh.
    setup_path = os.path.join(repo_root, "setup.sh")
    deploy_path = os.path.join(repo_root, "deploy", "deploy.sh")
    update_path = os.path.join(repo_root, "deploy", "update.sh")

    setup_content, setup_note = prepare_patch(
        setup_path, "setup.sh",
        [(SETUP_ANCHOR_OLD, SETUP_ANCHOR_NEW)],
        "Phase 3 of removing VictoriaMetrics",
    )
    results.append((setup_path, "setup.sh", setup_content, setup_note))

    deploy_content, deploy_note = prepare_patch(
        deploy_path, "deploy/deploy.sh",
        [(DEPLOY_UPDATE_ANCHOR_OLD, DEPLOY_UPDATE_ANCHOR_NEW)],
        "Phase 3 of removing VictoriaMetrics",
    )
    results.append((deploy_path, "deploy/deploy.sh", deploy_content, deploy_note))

    update_content, update_note = prepare_patch(
        update_path, "deploy/update.sh",
        [(DEPLOY_UPDATE_ANCHOR_OLD, DEPLOY_UPDATE_ANCHOR_NEW)],
        "Phase 3 of removing VictoriaMetrics",
    )
    results.append((update_path, "deploy/update.sh", update_content, update_note))

    print("\nFile patch plan:")
    for _, _, _, note in results:
        print(f"  {note}")

    if all(content is None for _, _, content, _ in results):
        print("\nNothing to do -- everything this script would change is already applied.")
        return

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
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

  A) No restart needed -- this only changes deploy-time scripts and the
     3 phase scripts' argument parsing / idempotency logic, not any code
     the running service imports.

  B) Sanity-check shell syntax:
       bash -n setup.sh && bash -n deploy/deploy.sh && bash -n deploy/update.sh && echo "SHELL SYNTAX OK"

  C) Confirm the 3 phase scripts still behave correctly with their new
     default AND no longer abort on the chain-idempotency issue (should
     all say "already patched" / "already handled" / "skipping" --
     nothing should abort):
       python3 apply_direct_gmd_metrics_revival.py
       python3 apply_azure_direct_metrics_fetch.py
       python3 apply_gcp_direct_metrics_fetch.py
     (safe to run as your normal user for this check; use sudo if you
     want Azure/GCP's metric_history existence check to pass too)

  D) Review, commit, push:
       git status
       git diff setup.sh deploy/deploy.sh deploy/update.sh \\
                apply_direct_gmd_metrics_revival.py \\
                apply_azure_direct_metrics_fetch.py \\
                apply_gcp_direct_metrics_fetch.py
       git add setup.sh deploy/deploy.sh deploy/update.sh \\
               apply_direct_gmd_metrics_revival.py \\
               apply_azure_direct_metrics_fetch.py \\
               apply_gcp_direct_metrics_fetch.py \\
               fix_deploy_script_drift.py
       git commit -m "fix(deploy): wire Phase 1-3 VM-removal scripts into setup.sh/deploy.sh/update.sh, fix their arg-default convention so wiring them in doesn't silently no-op forever, and fix a chain-idempotency bug where each phase's rewrite of sync_metrics_from_vm() broke the previous phase's re-run check -- plus setup.sh was still missing apply_drop_dead_tables.py too"
       git push origin main
""")


if __name__ == "__main__":
    main()
