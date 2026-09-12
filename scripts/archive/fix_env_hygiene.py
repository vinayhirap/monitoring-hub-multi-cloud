#!/usr/bin/env python3
"""
fix_env_hygiene.py
========================================
Closes HANDOVER.md #4's two smallest open items -- permanently, not with
a workaround -- plus a small honesty fix in two error messages.

ITEM 1: .env permissions (600, owned by hcsadmin-only)
-------------------------------------------------------
This is NOT a case of "decide on a new permission model" (the handover's
own phrasing made it sound like an open design choice, e.g. "640 owned
by cloudops:hcsadmin"). It's a plain ownership DRIFT bug with a known
cause already documented in HANDOVER.md #2d: the teammate's uncoordinated
incident-response session ran `chown hcsadmin ... .env` (visible in that
session's own history) while fixing a credential leak, and it was never
set back afterward.

deploy/deploy.sh's OWN logic proves what the correct state is supposed
to be: REAL_USER="${SUDO_USER:-$USER}" is used for BOTH the systemd
`User=` directive AND `.env`'s owner/mode (`chown $REAL_USER .env`,
`chmod 600`) -- i.e. this app's design is "the process owns its own
env file, self-contained, no separate group needed." On this server
`User=cloudops` (confirmed via systemctl show), so `.env` should be
owned by cloudops, exactly like deploy.sh would produce on a fresh
install. It just isn't, because of the incident. This script restores
that -- chown to cloudops:cloudops, keep chmod 600 -- which is BOTH the
minimal fix AND the one that won't drift again on a future deploy.sh
run (a new one would reproduce exactly this state). deploy/update.sh
(the routine, non-destructive path actually used day to day) never
touches .env's ownership at all, so this fix persists.

After this, every script in HANDOVER.md's "standard command pattern"
that reads .env directly can go back to `sudo -u cloudops python3 ...`
-- the `sudo python3 ...` (root) workaround this session's Phase 1-3
scripts needed is no longer necessary for anything written after this.

ITEM 2: .env.production
-------------------------
Confirmed unused by the running service (systemd's EnvironmentFile=
points only at .env; verified again here). It's gitignored, so this is
a pure server-side deletion, no repo change for the file itself.

BUT: it's not quite "never read by anything" as HANDOVER.md #4 framed
it -- app/auth/security.py and app/db.py's own error messages tell an
admin who hits a missing JWT_SECRET/DB_PASSWORD to "also add it to
.env.production on the server", and several of this session's own
scripts (fix_p0_credential_leak.py, fix_db_password_rotation.py, and
this session's own apply_direct_gmd_metrics_revival.py /
apply_azure_direct_metrics_fetch.py / apply_gcp_direct_metrics_fetch.py)
list it as a fallback candidate path. Those FALLBACK candidate lists are
left alone -- they degrade safely (os.path.isfile() check) when the file
is simply absent, and rewriting already-shipped one-off scripts isn't
worth the risk. But the two live error MESSAGES are user-facing guidance
that would become actively wrong the moment the file is deleted, so this
script fixes those two strings too, not just the file.

USAGE
-----
    cd /opt/monitoring-hub/app
    sudo python3 fix_env_hygiene.py --dry-run
    sudo python3 fix_env_hygiene.py --apply
(needs root: chown requires it, and the read-back verification needs to
check the cloudops-owned file works before this script declares success)
"""

import argparse
import grp
import os
import pwd
import shutil
import subprocess
import sys
from datetime import datetime

SECURITY_OLD = '''            "JWT_SECRET is not set. Generate one and add it to your .env file:\\n"
            "  python -c \\"import secrets; print(secrets.token_hex(32))\\"\\n"
            "then add JWT_SECRET=<the printed value> to .env (and .env.production on the server)."'''

SECURITY_NEW = '''            "JWT_SECRET is not set. Generate one and add it to your .env file:\\n"
            "  python -c \\"import secrets; print(secrets.token_hex(32))\\"\\n"
            "then add JWT_SECRET=<the printed value> to .env."'''

DB_OLD = '''            "DB_PASSWORD is not set. Set it in .env (and .env.production "
            "on the server) -- there is no default."'''

DB_NEW = '''            "DB_PASSWORD is not set. Set it in .env -- there is no default."'''


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


def prepare_patch(path, label, old, new, done_marker):
    if not os.path.exists(path):
        die(f"{label} not found at {path}.")
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    if done_marker in content:
        return None, f"{label} already patched -- skipping."
    n = content.count(old)
    if n != 1:
        die(f"{label}: expected exactly 1 match, found {n}. File may differ from what this script expects.")
    return content.replace(old, new, 1), f"{label}: OK"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="(default; kept for backward compatibility)")
    parser.add_argument("--dry-run", action="store_true", help="preview only, make no changes")
    args = parser.parse_args()
    apply_ = not args.dry_run

    if apply_ and os.geteuid() != 0:
        die("This needs to run as root (chown + verifying the fix requires it). Use: sudo python3 fix_env_hygiene.py --apply")

    repo_root = find_repo_root()          # /opt/monitoring-hub/app
    app_parent = os.path.dirname(repo_root)  # /opt/monitoring-hub
    env_path = os.path.join(repo_root, ".env")
    env_production_path = os.path.join(app_parent, ".env.production")

    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    # ── Item 1: .env ownership ──────────────────────────────────
    if not os.path.exists(env_path):
        die(f".env not found at {env_path} -- this doesn't look like a real deployment.")

    st = os.stat(env_path)
    current_owner = pwd.getpwuid(st.st_uid).pw_name
    current_mode = oct(st.st_mode)[-3:]
    print(f"\n.env current state: owner={current_owner}, mode={current_mode}")

    try:
        cloudops_uid = pwd.getpwnam("cloudops").pw_uid
        cloudops_gid = grp.getgrnam("cloudops").gr_gid
    except KeyError:
        die("cloudops user/group not found on this system -- HANDOVER.md's "
            "documented app-owner user doesn't exist here. Aborting rather "
            "than guessing a different owner.")

    needs_chown = current_owner != "cloudops"
    needs_chmod = current_mode != "600"

    if needs_chown:
        print(f"  -> will chown to cloudops:cloudops (matches deploy.sh's own REAL_USER contract)")
    if needs_chmod:
        print(f"  -> will chmod 600")
    if not needs_chown and not needs_chmod:
        print("  -> already correct, nothing to do")

    # ── Item 2: .env.production ─────────────────────────────────
    env_production_exists = os.path.isfile(env_production_path)
    print(f"\n{env_production_path}: {'exists -- will delete' if env_production_exists else 'already gone, nothing to do'}")

    # ── Repo patches: error message wording ─────────────────────
    security_path = os.path.join(repo_root, "app", "auth", "security.py")
    db_path = os.path.join(repo_root, "app", "db.py")

    security_content, security_note = prepare_patch(
        security_path, "app/auth/security.py", SECURITY_OLD, SECURITY_NEW,
        'to .env."'
    )
    db_content, db_note = prepare_patch(
        db_path, "app/db.py", DB_OLD, DB_NEW,
        '"DB_PASSWORD is not set. Set it in .env -- there is no default."'
    )
    print(f"\nFile patch plan:")
    print(f"  {security_note}")
    print(f"  {db_note}")

    if not apply_:
        print("\n[dry-run] No changes made. Re-run with --apply (or no flags) to apply.")
        return

    # ── Apply Item 1 ─────────────────────────────────────────────
    if needs_chown:
        os.chown(env_path, cloudops_uid, cloudops_gid)
        print(f"chowned {env_path} -> cloudops:cloudops")
    if needs_chmod:
        os.chmod(env_path, 0o600)
        print(f"chmod 600 {env_path}")

    verify = subprocess.run(
        ["sudo", "-u", "cloudops", "python3", "-c",
         f"open('{env_path}').read(); print('READ_OK')"],
        capture_output=True, text=True,
    )
    if "READ_OK" not in verify.stdout:
        die(f".env ownership fix applied, but cloudops still can't read it "
            f"directly -- investigate before relying on this:\n{verify.stderr}")
    print("Verified: cloudops can now read .env directly (no more `sudo python3` workaround needed for scripts that read it).")

    # ── Apply Item 2 ─────────────────────────────────────────────
    if env_production_exists:
        backup_path = env_production_path + f".deleted.{datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(env_production_path, backup_path)
        os.remove(env_production_path)
        print(f"Deleted {env_production_path} (backup kept at {backup_path} -- delete that "
              f"manually once you're confident nothing needed it)")

    # ── Apply repo patches ───────────────────────────────────────
    for path, content, label in [
        (security_path, security_content, "app/auth/security.py"),
        (db_path, db_content, "app/db.py"),
    ]:
        if content is None:
            continue
        backup(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"Patched {label}")

    print("""
[Manual follow-up]

  A) No restart needed -- .env's CONTENTS are unchanged, only its owner/
     mode changed, and the two patched files are just error-message text
     that only fires when a secret is missing (won't fire in normal
     operation). Restart anyway if you want a completely clean log:
       sudo systemctl restart monitoring-hub

  B) From now on, scripts that read .env directly can go back to the
     documented pattern:
       sudo -u cloudops python3 <script>.py --dry-run
     instead of the `sudo python3 ...` (root) workaround HANDOVER.md #4
     called out and this session's Phase 1-3 scripts needed.

  C) Review, commit, push:
       git status
       git diff app/auth/security.py app/db.py
       git add app/auth/security.py app/db.py fix_env_hygiene.py
       git commit -m "fix(ops): restore .env ownership to cloudops (drift from the Sep 7-8 incident, not a real permission-model question), delete confirmed-dead .env.production, and stop telling admins to edit a file that no longer exists"
       git push origin main
""")


if __name__ == "__main__":
    main()
