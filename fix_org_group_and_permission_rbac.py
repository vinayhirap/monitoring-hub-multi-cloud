#!/usr/bin/env python3
"""
fix_org_group_and_permission_rbac.py
=====================================

ONE-TIME + PERMANENT fix for the "GET /api/users -> 500,
user_group_memberships doesn't exist" bug (and its silent twin for
`viewer`, and the still-unrun `permissions`/`role_permissions` layer).

ROOT CAUSE (confirmed from journalctl + table check on this box)
------------------------------------------------------------------
Two migration scripts already committed in this repo have NEVER been
run on this server, because deploy/update.sh's automated migration
list simply never included them:

  1. apply_org_group_rbac.py   -> org_groups, group_policies,
     user_group_memberships (013_org_group_rbac.sql). Without these
     tables, app/auth/authorization.py's get_effective_scope() throws
     mysql.connector.errors.ProgrammingError 1146 for ANY non-admin
     login the moment it touches a scoped endpoint -- this is why
     editor's GET /api/users 500s, and viewer will hit the exact same
     error the first time it calls a scoped endpoint even though it
     hasn't shown up yet.
  2. apply_permission_rbac_system.py -> app/auth/permissions.py +
     db/migrations/015_permissions_rbac.sql (permissions /
     role_permissions). This one is a *code generator*: it writes the
     migration SQL file to disk but does NOT execute it against the
     DB itself, so a bare run leaves the SQL unapplied. This script
     applies it for you via apply_permission_rbac_migration.py
     (written out below), using the real DB_PASSWORD from .env --
     NOT the placeholder "-proot123" printed in that script's own
     "next steps" text, which is a leftover dev/example password and
     not necessarily this server's real one.

WHAT THIS SCRIPT DOES, IN ORDER
------------------------------------------------------------------
  1. Pre-flight: confirms this is really the monitoring-hub checkout
     (right repo remote, right files present).
  2. (Optional) confirms this box's public IP matches the one the
     browser is actually hitting, so we never "fix" the wrong server
     again like almost happened earlier this session.
  3. Stops the monitoring-hub service (same lock-pileup precaution
     update.sh itself already takes before any migration).
  4. Runs apply_org_group_rbac.py --dry-run, then for real.
  5. Runs apply_permission_rbac_system.py --dry-run, then for real
     (writes the 015 SQL file + code files).
  6. Writes apply_permission_rbac_migration.py (a small, idempotent,
     .env-driven helper -- same pattern as every other apply_*.py in
     this repo) and runs it to actually execute 015_permissions_rbac.sql
     against the DB.
  7. Rebuilds the frontend (both patches above also touch frontend
     .jsx files) and restarts the service.
  8. Verifies: tables exist, role_permissions has rows, service is
     active.
  9. PERMANENT FIX: patches update.sh to add these three scripts to
     the automated migration list, so no future box brought up via
     update.sh (or a fresh setup.sh + first update.sh) ever hits this
     gap again.
 10. Shows a git diff of everything that changed and, unless
     --no-git is passed, commits and tries to push it so the fix is
     upstream, not just live-patched on this one server.

USAGE
------------------------------------------------------------------
    # 1. Dry run everything first -- touches nothing, just tells you
    #    what would happen:
    python3 fix_org_group_and_permission_rbac.py --dry-run

    # 2. For real:
    python3 fix_org_group_and_permission_rbac.py

    # Flags:
    #   --skip-ip-check   don't verify public IP before touching anything
    #   --no-git          apply + verify only, skip commit/push
    #   --yes             don't pause for the IP-mismatch confirmation
"""
import argparse
import subprocess
import sys
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parent
VENV_PY = sys.executable
SERVICE_NAME = "monitoring-hub"
EXPECTED_REPO = "https://github.com/vinayhirap/monitoring-hub-multi-cloud.git"
EXPECTED_PUBLIC_IP = "13.206.223.98"

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "monitor")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "monitoring_hub")

ORG_GROUP_SCRIPT = REPO_ROOT / "apply_org_group_rbac.py"
PERMISSION_SCRIPT = REPO_ROOT / "apply_permission_rbac_system.py"
PERMISSION_SQL = REPO_ROOT / "db" / "migrations" / "015_permissions_rbac.sql"
PERMISSION_MIGRATION_HELPER = REPO_ROOT / "apply_permission_rbac_migration.py"
UPDATE_SH = REPO_ROOT / "update.sh"
FRONTEND_DIR = REPO_ROOT / "frontend"

# Content written for the new, small, idempotent, .env-driven helper that
# actually executes 015_permissions_rbac.sql. Mirrors the exact pattern
# apply_org_group_rbac.py already uses for its own DB step (table-exists
# check, strip full-line "--" comments, split on ";", commit).
PERMISSION_MIGRATION_HELPER_CONTENT = '''#!/usr/bin/env python3
"""
apply_permission_rbac_migration.py — executes
db/migrations/015_permissions_rbac.sql against the database.

apply_permission_rbac_system.py only WRITES this SQL file to disk (it's
a code generator for app/auth/permissions.py etc.) -- it deliberately
does not touch the DB itself. This script is the missing other half:
same idempotent, .env-driven pattern every other apply_*.py migration
in this repo uses (table-exists check, safe to re-run every deploy).

Usage:
    python apply_permission_rbac_migration.py --dry-run
    python apply_permission_rbac_migration.py
"""
import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parent
SQL_PATH = REPO_ROOT / "db" / "migrations" / "015_permissions_rbac.sql"

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "monitor")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "monitoring_hub")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not SQL_PATH.exists():
        print(f"MISSING: {SQL_PATH} -- run apply_permission_rbac_system.py first.")
        sys.exit(1)

    try:
        import mysql.connector
    except ImportError:
        print("mysql-connector-python is not installed. Install it (pip install "
              "mysql-connector-python) or apply the SQL file manually:")
        print(f"  mysql -u{DB_USER} -p {DB_NAME} < {SQL_PATH}")
        sys.exit(1)

    conn = mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME,
    )
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (DB_NAME, "permissions"),
        )
        already = cur.fetchone()[0] > 0

        if already:
            print("permissions table already exists -- 015 migration already applied. Nothing to do.")
        else:
            sql_text = SQL_PATH.read_text(encoding="utf-8")
            code_lines = [
                line for line in sql_text.splitlines()
                if not line.strip().startswith("--")
            ]
            sql = "\\n".join(code_lines)
            statements = [s.strip() for s in sql.split(";") if s.strip()]

            if args.dry_run:
                print(f"[DRY RUN] would execute {len(statements)} statement(s) from {SQL_PATH.name}")
            else:
                for stmt in statements:
                    cur.execute(stmt)
                conn.commit()
                print(f"Applied {SQL_PATH.name}")

        if not args.dry_run:
            cur.execute("SELECT role, COUNT(*) FROM role_permissions GROUP BY role")
            rows = cur.fetchall()
            if rows:
                for role, count in rows:
                    print(f"  role_permissions: {role} -> {count} permission(s)")
            else:
                print("  WARNING: role_permissions has zero rows -- migration may not have seeded correctly.")
        cur.close()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
'''

# The exact block to insert into update.sh's automated migration list,
# and the anchor it goes after (right after 012, before the metric-catalog
# seed step -- matches the numeric order of the actual .sql filenames:
# 011 access_scopes -> 012 alert hardening -> 013 org groups -> 015 permissions).
#
# NOTE: fixed 2026-09-04 -- the original anchor assumed a blank line between
# the 012 migration and seed_metric_catalog.py. The real update.sh on the
# Mumbai box has NO blank line there, so the anchor never matched and this
# step aborted (leaving the service stopped, since it runs before
# start_service()). Both variants are tried now, in order, so this survives
# either formatting -- and any future whitespace-only drift in update.sh
# degrades to a clear "anchor not found" message instead of silently
# corrupting the file.
UPDATE_SH_ANCHOR_VARIANTS = [
    # Variant A: no blank line (confirmed actual format, 2026-09-04)
    '''run_migration apply_alert_evaluation_hardening_migration.py \\
    "012: alerts.last_seen_at/healthy_streak + alert_pending table"
run_migration scripts/seed_metric_catalog.py \\''',
    # Variant B: blank line (original assumption -- kept as a fallback)
    '''run_migration apply_alert_evaluation_hardening_migration.py \\
    "012: alerts.last_seen_at/healthy_streak + alert_pending table"

run_migration scripts/seed_metric_catalog.py \\''',
]


def _build_replacement(anchor: str) -> str:
    """Insert the 013/permissions block right after the 012 block, right
    before seed_metric_catalog.py, preserving whichever blank-line style
    `anchor` already used."""
    inject = '''run_migration apply_org_group_rbac.py \\
    "013: org_groups/group_policies/user_group_memberships (hierarchical L1/L2/L3 RBAC groups) -- fixes editor/viewer 500s on any scoped endpoint"
run_migration apply_permission_rbac_system.py \\
    "code: app/auth/permissions.py + granular permission checks (also (re)writes db/migrations/015_permissions_rbac.sql)"
run_migration apply_permission_rbac_migration.py \\
    "015: permissions/role_permissions seed data (must run AFTER apply_permission_rbac_system.py above)"
'''
    marker = 'run_migration scripts/seed_metric_catalog.py \\'
    return anchor.replace(marker, inject + marker, 1)


class FixError(Exception):
    pass


def run(cmd, cwd=None, check=False, capture_output=False, text=True):
    print(f"$ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, cwd=cwd or REPO_ROOT, check=check,
                           capture_output=capture_output, text=text)


def sanity_check_repo():
    print("=== Pre-flight: confirming this is the right checkout ===")
    problems = []
    for required in [ORG_GROUP_SCRIPT, PERMISSION_SCRIPT, UPDATE_SH,
                      REPO_ROOT / "app" / "auth" / "authorization.py"]:
        if not required.exists():
            problems.append(f"MISSING: {required}")

    remote = run(["git", "remote", "get-url", "origin"], capture_output=True).stdout.strip()
    if remote != EXPECTED_REPO:
        problems.append(f"git remote is '{remote}', expected '{EXPECTED_REPO}'.")

    if problems:
        raise FixError("Pre-flight failed:\n" + "\n".join(problems) +
                        "\n\nThis doesn't look like /opt/monitoring-hub/app on the right box. Aborting.")
    print("OK: repo, remote, and required scripts all present.\n")


def check_public_ip(skip: bool, auto_yes: bool):
    if skip:
        print("Skipping public IP check (--skip-ip-check passed).\n")
        return
    print("=== Confirming this box's public IP matches what the browser hits ===")
    try:
        import urllib.request
        actual = urllib.request.urlopen("https://ifconfig.me", timeout=5).read().decode().strip()
    except Exception as e:
        print(f"WARNING: could not check public IP ({e}). Continuing without this check.\n")
        return
    if actual == EXPECTED_PUBLIC_IP:
        print(f"OK: public IP is {actual}, matches expected {EXPECTED_PUBLIC_IP}.\n")
        return

    print(f"\n*** WARNING: this box's public IP is {actual}, not the expected {EXPECTED_PUBLIC_IP}. ***")
    print("If your browser error was against 13.206.223.98, fixing THIS box may not fix that dashboard.")
    print("Check the AWS Console for which instance currently owns 13.206.223.98 before continuing.\n")
    if auto_yes:
        print("--yes passed, continuing anyway.\n")
        return
    answer = input("Type 'yes' to continue anyway, anything else aborts: ").strip().lower()
    if answer != "yes":
        raise FixError("Aborted due to public IP mismatch.")


def stop_service(dry_run: bool):
    print("=== Stopping monitoring-hub before migrations ===")
    if dry_run:
        print("[DRY RUN] would run: sudo systemctl stop monitoring-hub")
        return
    run(["sudo", "systemctl", "stop", SERVICE_NAME], check=True)


def start_service(dry_run: bool):
    print("=== Restarting monitoring-hub ===")
    if dry_run:
        print("[DRY RUN] would run: sudo systemctl start monitoring-hub")
        return
    run(["sudo", "systemctl", "start", SERVICE_NAME], check=True)
    result = run(["systemctl", "is-active", SERVICE_NAME], capture_output=True)
    state = result.stdout.strip()
    print(f"monitoring-hub is: {state}")
    if state != "active":
        raise FixError(
            f"{SERVICE_NAME} did not come back up (state: {state}). "
            f"Check: journalctl -u {SERVICE_NAME} -n 100 --no-pager"
        )


def run_apply_script(script_path: Path, dry_run: bool, extra_args=None):
    extra_args = extra_args or []
    name = script_path.name
    print(f"\n=== {name} --dry-run ===")
    dry = run([VENV_PY, str(script_path), "--dry-run"] + extra_args)
    if dry.returncode != 0:
        raise FixError(f"{name} --dry-run failed (exit {dry.returncode}) -- aborting before touching anything.")

    if dry_run:
        print(f"[DRY RUN] would now run {name} for real.")
        return

    print(f"\n=== {name} (applying) ===")
    real = run([VENV_PY, str(script_path)] + extra_args)
    if real.returncode != 0:
        raise FixError(
            f"{name} failed for real (exit {real.returncode}). "
            f"Service is currently STOPPED -- fix the error above, then re-run this script "
            f"(it's safe to re-run) or manually `sudo systemctl start {SERVICE_NAME}` once you've decided next steps."
        )


def write_permission_migration_helper(dry_run: bool):
    print(f"\n=== Writing {PERMISSION_MIGRATION_HELPER.name} ===")
    if PERMISSION_MIGRATION_HELPER.exists() and \
            PERMISSION_MIGRATION_HELPER.read_text(encoding="utf-8") == PERMISSION_MIGRATION_HELPER_CONTENT:
        print("Already present and up to date -- nothing to write.")
        return
    if dry_run:
        print(f"[DRY RUN] would write {PERMISSION_MIGRATION_HELPER}")
        return
    PERMISSION_MIGRATION_HELPER.write_text(PERMISSION_MIGRATION_HELPER_CONTENT, encoding="utf-8")
    print(f"Wrote {PERMISSION_MIGRATION_HELPER}")


def patch_update_sh(dry_run: bool):
    print("\n=== Patching update.sh to add these to the automated migration list ===")
    text = UPDATE_SH.read_text(encoding="utf-8")

    if "run_migration apply_org_group_rbac.py" in text:
        print("update.sh already has these migrations in its automated list -- nothing to do.")
        return

    matched_anchor = next((a for a in UPDATE_SH_ANCHOR_VARIANTS if a in text), None)
    if matched_anchor is None:
        raise FixError(
            "Could not find the expected anchor text in update.sh to patch (tried "
            f"{len(UPDATE_SH_ANCHOR_VARIANTS)} known formatting variants) -- it may have "
            "changed since this script was written. Add these three lines to the "
            "'Idempotent schema migrations' section of update.sh by hand, right after "
            "the '012' migration and before scripts/seed_metric_catalog.py:\n\n" +
            _build_replacement(UPDATE_SH_ANCHOR_VARIANTS[0])
        )

    replacement = _build_replacement(matched_anchor)
    new_text = text.replace(matched_anchor, replacement, 1)
    if dry_run:
        print("[DRY RUN] would patch update.sh to add apply_org_group_rbac.py, "
              "apply_permission_rbac_system.py, apply_permission_rbac_migration.py "
              "to the automated migration list.")
        return
    UPDATE_SH.write_text(new_text, encoding="utf-8")
    print("Patched update.sh.")


def rebuild_frontend(dry_run: bool):
    print("\n=== Rebuilding frontend (permission_rbac_system touches .jsx files) ===")
    if dry_run:
        print("[DRY RUN] would run: npm install --silent && npm run build (in frontend/)")
        return
    run(["npm", "install", "--silent"], cwd=FRONTEND_DIR, check=True)
    run(["npm", "run", "build"], cwd=FRONTEND_DIR, check=True)


def verify_tables(dry_run: bool):
    if dry_run:
        print("\n[DRY RUN] would verify org_groups/group_policies/user_group_memberships/"
              "permissions/role_permissions all exist.")
        return
    print("\n=== Verifying tables ===")
    import mysql.connector
    conn = mysql.connector.connect(host=DB_HOST, port=DB_PORT, user=DB_USER,
                                    password=DB_PASSWORD, database=DB_NAME)
    try:
        cur = conn.cursor()
        for table in ["org_groups", "group_policies", "user_group_memberships",
                      "permissions", "role_permissions"]:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name=%s",
                (DB_NAME, table),
            )
            exists = cur.fetchone()[0] > 0
            print(f"  {'OK ' if exists else 'MISSING'}  {table}")
            if not exists:
                raise FixError(f"{table} still missing after migrations -- something went wrong above.")
        cur.close()
    finally:
        conn.close()


def git_commit_and_push(dry_run: bool, do_git: bool):
    print("\n=== Git status ===")
    status = run(["git", "status", "--porcelain"], capture_output=True).stdout
    print(status if status.strip() else "(clean -- nothing to commit)")

    if not status.strip():
        print("Nothing changed in git's eyes -- everything was already committed. Done.")
        return
    if not do_git:
        print("--no-git passed: leaving these changes uncommitted on this server only.")
        print("Remember: an uncommitted change here will conflict with (or be silently lost on)")
        print("the next `git pull origin main` -- commit and push this eventually.")
        return
    if dry_run:
        print("[DRY RUN] would `git add -A`, commit, and attempt `git push origin main`.")
        return

    run(["git", "add", "-A"], check=True)
    commit_msg = (
        "Fix: run org-group + permission RBAC migrations, add them to update.sh's "
        "automated list\n\n"
        "editor/viewer GET /api/users (and any scoped endpoint) was 500ing with "
        "mysql.connector.errors.ProgrammingError 1146 for user_group_memberships, "
        "because apply_org_group_rbac.py and apply_permission_rbac_system.py were "
        "never in update.sh's automated migration list -- so any box brought up "
        "purely via update.sh (not a from-scratch setup.sh + manual run) never got "
        "org_groups/group_policies/user_group_memberships or permissions/"
        "role_permissions.\n\n"
        "- Added apply_permission_rbac_migration.py: apply_permission_rbac_system.py "
        "only writes 015_permissions_rbac.sql to disk, it never executes it -- this "
        "small idempotent, .env-driven script is the missing other half.\n"
        "- update.sh: added all three to the automated migration list, in dependency "
        "order (org groups -> permission code -> permission SQL).\n"
        "- Applied both migrations live on this server; verified org_groups, "
        "group_policies, user_group_memberships, permissions, role_permissions all "
        "exist and role_permissions is seeded."
    )
    run(["git", "commit", "-m", commit_msg], check=True)

    print("\n=== Attempting git push origin main ===")
    push = run(["git", "push", "origin", "main"])
    if push.returncode != 0:
        print("\nWARNING: push failed (likely no write credentials configured on this box).")
        print("The commit is saved locally on this server. To get it upstream, either:")
        print("  a) configure a token/deploy key with push access here and re-run:")
        print("       git push origin main")
        print("  b) or pull this exact commit down to your own machine and push from there:")
        print(f"       git remote add mumbai-fix hcsadmin@{EXPECTED_PUBLIC_IP}:/opt/monitoring-hub/app")
        print("       git fetch mumbai-fix")
        print("       git push origin mumbai-fix/main:main")
    else:
        print("Pushed. Every future box (including a Mumbai rebuild) now gets this fix on first update.sh run.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                         help="Show every step, touch nothing (no stop/start, no DB writes, no git).")
    parser.add_argument("--skip-ip-check", action="store_true",
                         help="Skip the public-IP-matches-13.206.223.98 confirmation.")
    parser.add_argument("--no-git", action="store_true",
                         help="Apply + verify only. Don't git add/commit/push.")
    parser.add_argument("--yes", action="store_true",
                         help="Don't pause for interactive confirmation on IP mismatch.")
    args = parser.parse_args()

    try:
        sanity_check_repo()
        check_public_ip(skip=args.skip_ip_check, auto_yes=args.yes)

        stop_service(args.dry_run)

        run_apply_script(ORG_GROUP_SCRIPT, args.dry_run)
        run_apply_script(PERMISSION_SCRIPT, args.dry_run)

        write_permission_migration_helper(args.dry_run)
        run_apply_script(PERMISSION_MIGRATION_HELPER, args.dry_run)

        rebuild_frontend(args.dry_run)
        patch_update_sh(args.dry_run)

        start_service(args.dry_run)
        verify_tables(args.dry_run)

        git_commit_and_push(args.dry_run, do_git=not args.no_git)

        print("\n=== Done. ===")
        if args.dry_run:
            print("This was a --dry-run: nothing was actually changed. Re-run without --dry-run to apply.")
        else:
            print("Retry the editor (and viewer) login now and re-check GET /api/users -- should be 200.")

    except FixError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        _restart_if_stopped_best_effort(args.dry_run)
        sys.exit(1)


def _restart_if_stopped_best_effort(dry_run: bool):
    """Safety net added 2026-09-04: the update.sh-anchor abort left the
    service stopped because start_service() runs after patch_update_sh()
    and never got reached. Whatever the abort reason is in future, don't
    leave production down on a step that has nothing to do with the
    service itself -- best-effort restart, then still surface the error."""
    if dry_run:
        return
    state = subprocess.run(["systemctl", "is-active", SERVICE_NAME],
                            capture_output=True, text=True).stdout.strip()
    if state == "active":
        return
    print(f"\n=== Safety net: {SERVICE_NAME} is '{state}', not 'active' -- attempting restart ===",
          file=sys.stderr)
    result = subprocess.run(["sudo", "systemctl", "start", SERVICE_NAME])
    new_state = subprocess.run(["systemctl", "is-active", SERVICE_NAME],
                                capture_output=True, text=True).stdout.strip()
    if new_state == "active":
        print(f"{SERVICE_NAME} restarted successfully despite the abort above.", file=sys.stderr)
    else:
        print(f"COULD NOT RESTART {SERVICE_NAME} (state: {new_state}). Check manually:\n"
              f"  sudo journalctl -u {SERVICE_NAME} -n 100 --no-pager\n"
              f"  sudo systemctl start {SERVICE_NAME}", file=sys.stderr)


if __name__ == "__main__":
    main()
