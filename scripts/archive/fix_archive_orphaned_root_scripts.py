#!/usr/bin/env python3
"""
fix_archive_orphaned_root_scripts.py
=========================================
Monitoring Hub -- archives root-level one-off scripts/patches that nothing
references anymore.

BACKGROUND
----------
The repo root has ~90 loose apply_*.py / fix_*.py / *.patch / *.ps1 files
accumulated over the project's history. Checked EVERY one of them against
the whole tracked repo (all file types, not just .sh/.py/.md) for any
reference anywhere -- another script importing/calling it, a shell script
invoking it, documentation mentioning it, anything:

    grep -rl -- "<filename>" .   (excluding .git)

53 of them have ZERO references anywhere outside their own filename. They
are not called by setup.sh, deploy.sh, or update.sh (root or deploy/), not
imported or shelled out to by any other script, not mentioned in any .md,
.yml, or config file. They are one-off historical fixes/patches that were
already applied by hand at some point and never cleaned up.

This list is deliberately CONSERVATIVE: a second cluster of ~15-20 files
exists that reference EACH OTHER (e.g. one script's docstring mentions a
follow-up script by name) but are still not called from any live deploy
path -- those are NOT included here and need a closer, dedicated look
before archiving, since establishing they're truly dead requires tracing
each small reference chain individually rather than a single grep pass.
Left in place for a follow-up pass.

WHAT THIS SCRIPT DOES
-----------------------
  1. Backs up every file about to be moved (byte-for-byte copies) into a
     single timestamped local backup folder -- belt-and-braces on top of
     git history, which already has every one of these files.
  2. Moves (not deletes) all 53 files into scripts/archive/, preserving
     them for reference. Nothing is deleted.
  3. Writes scripts/archive/MANIFEST.md documenting why each file is here
     and how "no references anywhere" was verified.

WHAT THIS SCRIPT DOES NOT DO
--------------------------------
  - Does NOT delete anything.
  - Does NOT touch the ~15-20 files in the "referenced only by other dead
    files" cluster -- flagged for a follow-up pass, not touched here.
  - Does NOT touch setup.sh/update.sh/deploy/ or any live apply_*.py
    script still wired into the real migration chain.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_archive_orphaned_root_scripts.py --dry-run
    python3 fix_archive_orphaned_root_scripts.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

ARCHIVE_DIR = "scripts/archive"

ORPHANED_FILES = [
    'add_ap_south_2_region.py', 'apply-metric-selector-redesign.ps1',
    'apply_alert_deeplink_fix.py', 'apply_alert_stale_tab_fix.py',
    'apply_alert_toast_account_region_fix.py',
    'apply_backend_shutdown_partition_fix.py', 'apply_branding.py',
    'apply_cloud_selector_onboarding_shell.py',
    'apply_console_account_locked_signin.py', 'apply_console_fix.py',
    'apply_console_scope_and_dynamic_resources_fix_1.py',
    'apply_console_self_federation.py',
    'apply_console_url_consolidation_frontend.py',
    'apply_core_only_tiles.py', 'apply_dashboard_data_cache.py',
    'apply_directory_tier_resource_counts_fix.py',
    'apply_ec2_memory_disk_metrics.py', 'apply_editor_account_scoping.py',
    'apply_fast_resource_counts_fix.py',
    'apply_fix_slow_s3_bucket_detail_calls.py',
    'apply_group_role_sync_and_smtp.py',
    'apply_icons_sidebar_noc_removal.py',
    'apply_multi_cloud_providers_backend.py',
    'apply_multicloud_step4_7_collectors.py',
    'apply_overview_polish_and_user_groups.py',
    'apply_overview_reliability_fix.py',
    'apply_phase0b_frontend_session_auth.py',
    'apply_provider_abstraction_layer.py',
    'apply_region_bug_and_discovery_expansion.py',
    'apply_remove_group_hint_text.py', 'apply_service_route_fix.py',
    'apply_servicelist_reconcile.py', 'apply_stale_alert_fix.ps1',
    'apply_strict_resource_filter.py', 'apply_timezone_selector.py',
    'auto-detect-metrics.patch', 'check_metrics.ps1',
    'cleanup_backend.py', 'fix-broken-main.patch', 'fix_mojibake.py',
    'fix_org_group_and_permission_rbac.py',
    'full-multicloud-discovery.patch', 'metric-catalog-bugfixes.patch',
    'metric-catalog-feature.patch', 'metric-catalog-seed-fix2.patch',
    'multi-cloud-auto-detect-resolved.patch',
    'multi-cloud-auto-detect.patch',
    'onboarding-wizard-auto-detect-ui.patch',
    'patch_ec2_network_stat.py', 'patch_seed_script_dotenv.py',
    'sync_cloudwatch_alarm_thresholds_.py', 'test_query.ps1',
    'validate_yace_namespaces.sh',
]

MANIFEST_TEMPLATE = """# Archived scripts

These {count} files were moved out of the repo root on {date} because a
repo-wide search (`grep -rl -- "<filename>" .`, all file types, excluding
.git) found zero references to any of them anywhere in the tracked repo --
not called by setup.sh/deploy.sh/update.sh (root or deploy/), not imported
or shelled out to by any other script, not mentioned in any doc or config.

They are one-off historical fixes/patches, already applied by hand to
whatever environment they were written for, at some point in the past.
Nothing is deleted -- full git history is preserved via this move, and a
local backup was also taken before moving (see the fix script's own
console output for the backup path).

A second, smaller cluster of scripts that reference EACH OTHER (but are
still not called from any live deploy path) was deliberately left at the
repo root -- confirming those are truly dead requires tracing each small
reference chain individually, which wasn't done as part of this pass.

## Archived files
{file_list}
"""


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

    archive_path = os.path.join(repo_root, ARCHIVE_DIR)

    to_move = []
    missing = []
    already_archived = []
    for fname in ORPHANED_FILES:
        src = os.path.join(repo_root, fname)
        dst = os.path.join(archive_path, fname)
        if os.path.exists(dst) and not os.path.exists(src):
            already_archived.append(fname)
            continue
        if not os.path.exists(src):
            missing.append(fname)
            continue
        to_move.append((fname, src, dst))

    print(f"\n{len(to_move)} file(s) to archive, "
          f"{len(already_archived)} already archived, "
          f"{len(missing)} not found (already moved/removed elsewhere).")
    if missing:
        print("  Not found (skipping):", ", ".join(missing))
    if already_archived:
        print("  Already archived (skipping):", ", ".join(already_archived))

    if not to_move:
        print("\nNothing to do.")
        return

    print("\nWould move into scripts/archive/:")
    for fname, src, dst in to_move:
        print(f"  {fname}")

    if not apply_:
        print("\n[dry-run] No files moved. Re-run with --apply to make real changes.")
        return

    # Backup: byte-for-byte copies of everything about to move, on top of
    # git history, into a single timestamped local folder.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(repo_root, "db_backups", f"archived_scripts_backup_{ts}")
    os.makedirs(backup_dir, exist_ok=True)
    for fname, src, _ in to_move:
        shutil.copy2(src, os.path.join(backup_dir, fname))
    print(f"\nBacked up {len(to_move)} file(s) to {backup_dir}")

    os.makedirs(archive_path, exist_ok=True)
    for fname, src, dst in to_move:
        shutil.move(src, dst)
    print(f"Moved {len(to_move)} file(s) into {ARCHIVE_DIR}/")

    manifest_path = os.path.join(archive_path, "MANIFEST.md")
    all_archived_now = sorted(
        f for f in os.listdir(archive_path) if f != "MANIFEST.md"
    )
    file_list = "\n".join(f"- `{f}`" for f in all_archived_now)
    manifest = MANIFEST_TEMPLATE.format(
        count=len(all_archived_now),
        date=datetime.now().strftime("%Y-%m-%d"),
        file_list=file_list,
    )
    with open(manifest_path, "w", encoding="utf-8") as fh:
        fh.write(manifest)
    print(f"Wrote {ARCHIVE_DIR}/MANIFEST.md")

    print(f"""
[Manual follow-up]

  A) Nothing on the running server needs a restart -- these files were
     never imported or executed by the running app; this is a pure
     repo-organization change.

  B) Review, commit, push (use `git mv`-equivalent tracking -- git will
     detect these as renames automatically since content is unchanged):
       git status
       git add -A
       git status   # confirm it shows renames, not delete+add-as-new
       git commit -m "chore: archive {len(all_archived_now)} orphaned one-off scripts/patches into scripts/archive/ (zero references found anywhere in repo)"
       git push origin main

  C) Local backup also at: db_backups/archived_scripts_backup_{ts}/
     (on top of git history -- delete it once you're confident, it's not
     meant to be a permanent second copy).
""")


if __name__ == "__main__":
    main()
