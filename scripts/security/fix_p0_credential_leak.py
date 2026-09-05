#!/usr/bin/env python3
"""
fix_p0_credential_leak.py
==========================
Monitoring Hub — P0 remediation script (run ON THE DEV SERVER, 13.200.102.131).

WHAT THIS FIXES
---------------
The public GitHub repo (vinayhirap/monitoring-hub-multi-cloud) has these
files tracked in git history right now:

    cookies.txt              -> live admin JWT session cookie
    cookies_editor.txt       -> live editor JWT session cookie
    login.json               -> plaintext admin/admin123
    login_editor.json        -> plaintext editor/editor123
    grant_editor_scope.json  -> test fixture (low risk, cleaned up anyway)
    create_viewer_bad.json   -> test fixture (low risk, cleaned up anyway)
    create_viewer_ok.json    -> test fixture (low risk, cleaned up anyway)

Because the repo is PUBLIC, those tokens/passwords are exposed to anyone.
This script:

  1. Backs up the 7 files to a local, git-ignored folder (in case you want
     the values for reference) then removes them from the working tree
     and un-tracks them from git (git rm --cached).
  2. Extends .gitignore so these patterns can never be committed again.
  3. Sanitizes .env.production.example so it no longer carries a
     real-looking password value.
  4. Rotates JWT_SECRET in .env.production (generates a new random 64-char
     hex secret). This alone invalidates every existing session token,
     including the two leaked ones — no user can use the old cookies.txt
     token after this + a service restart.
  5. Prompts you (interactively, not via CLI args, so passwords never
     land in shell history) for new admin/editor passwords, hashes them
     with the SAME bcrypt scheme app/auth/security.py uses
     (bcrypt.hashpw(password[:72].encode(), bcrypt.gensalt())), and
     updates users.password_hash directly in MySQL for those two
     accounts.
  6. Prints the exact remaining manual steps (git commit/push, service
     restart, git-history purge) — those are NOT run automatically
     because they're either irreversible (force-push rewrites history)
     or require credentials this script deliberately never touches.

WHAT THIS DELIBERATELY DOES NOT DO
-----------------------------------
  - It does NOT git push (needs your git credentials/SSH key).
  - It does NOT purge old commits from git history (needs a deliberate
    force-push decision from you — commands are printed at the end).
  - It does NOT touch production (35.154.149.94). Dev only.
  - It does NOT guess your DB password. It reads DB_HOST/DB_PORT/DB_USER/
    DB_PASSWORD/DB_NAME from .env.production, same as app/db.py does.

USAGE (on the dev server, as hcsadmin, from inside the repo directory)
------------------------------------------------------------------------
    cd /path/to/monitoring-hub-multi-cloud      # wherever it's checked out
    python3 fix_p0_credential_leak.py --dry-run     # see what would happen
    python3 fix_p0_credential_leak.py --apply       # actually do it

Safe to re-run: every step checks current state before acting.
"""

import argparse
import getpass
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime

SENSITIVE_FILES = [
    "cookies.txt",
    "cookies_editor.txt",
    "login.json",
    "login_editor.json",
    "grant_editor_scope.json",
    "create_viewer_bad.json",
    "create_viewer_ok.json",
]

GITIGNORE_ADDITIONS = [
    "# --- added by fix_p0_credential_leak.py ---",
    "cookies*.txt",
    "login*.json",
    "create_viewer*.json",
    "grant_*_scope.json",
    "*_scope.json",
]

ACCOUNTS_TO_ROTATE = ["admin", "editor"]


def run(cmd, check=True, capture=False):
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, check=check, text=True,
                             capture_output=capture)
    return result


def die(msg):
    print(f"\n[ABORT] {msg}", file=sys.stderr)
    sys.exit(1)


def find_repo_root():
    """We must be run from inside (or under) the monitoring-hub-multi-cloud
    checkout. Walk upward looking for a .git dir whose origin matches."""
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            try:
                remote = subprocess.run(
                    ["git", "-C", cur, "remote", "get-url", "origin"],
                    text=True, capture_output=True, check=True
                ).stdout.strip()
            except subprocess.CalledProcessError:
                remote = ""
            if "monitoring-hub-multi-cloud" in remote or True:
                # Even if remote name differs, a .git here is good enough
                # as long as the known files exist alongside it.
                if os.path.exists(os.path.join(cur, "app", "auth", "security.py")):
                    return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            die("Could not locate the monitoring-hub-multi-cloud repo root "
                "(looked for a .git dir alongside app/auth/security.py). "
                "cd into the repo checkout on the dev server and re-run.")
        cur = parent


def step_backup_and_untrack(repo_root, apply_):
    print("\n[1/6] Backing up and un-tracking leaked credential files")
    backup_dir = os.path.join(
        repo_root, "..", f"leaked_creds_backup_{datetime.now():%Y%m%d_%H%M%S}"
    )
    backup_dir = os.path.abspath(backup_dir)

    found = [f for f in SENSITIVE_FILES if os.path.exists(os.path.join(repo_root, f))]
    if not found:
        print("  Nothing to do — none of the known sensitive files are present.")
        return

    print(f"  Found: {', '.join(found)}")
    if not apply_:
        print(f"  [dry-run] would back up to {backup_dir} then git rm --cached + delete")
        return

    os.makedirs(backup_dir, exist_ok=True)
    for f in found:
        shutil.copy2(os.path.join(repo_root, f), os.path.join(backup_dir, f))
    print(f"  Backed up to: {backup_dir}  (this folder is OUTSIDE the repo, not tracked)")

    tracked = subprocess.run(
        ["git", "-C", repo_root, "ls-files"] + found,
        text=True, capture_output=True
    ).stdout.split()
    if tracked:
        run(["git", "-C", repo_root, "rm", "--cached", "-q"] + tracked)
    for f in found:
        full = os.path.join(repo_root, f)
        if os.path.exists(full):
            os.remove(full)
    print("  Removed from working tree and un-staged from git tracking.")


def step_gitignore(repo_root, apply_):
    print("\n[2/6] Hardening .gitignore")
    gi_path = os.path.join(repo_root, ".gitignore")
    existing = ""
    if os.path.exists(gi_path):
        with open(gi_path, "r", encoding="utf-8", errors="ignore") as fh:
            existing = fh.read()

    missing = [line for line in GITIGNORE_ADDITIONS if line not in existing]
    if not missing:
        print("  .gitignore already has the needed patterns.")
        return

    print(f"  Will append {len(missing)} line(s) to .gitignore")
    if not apply_:
        print("  [dry-run] would append:\n    " + "\n    ".join(missing))
        return

    with open(gi_path, "a", encoding="utf-8") as fh:
        fh.write("\n" + "\n".join(missing) + "\n")
    print("  .gitignore updated.")


def step_sanitize_env_example(repo_root, apply_):
    print("\n[3/6] Sanitizing .env.production.example")
    path = os.path.join(repo_root, ".env.production.example")
    if not os.path.exists(path):
        print("  .env.production.example not found, skipping.")
        return

    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    new_content, n = re.subn(
        r"^DB_PASSWORD=.*$", "DB_PASSWORD=changeme_do_not_use_default",
        content, flags=re.MULTILINE
    )
    if n == 0:
        print("  No DB_PASSWORD line found to sanitize, skipping.")
        return

    print("  Will replace the example DB_PASSWORD value with a clearly-fake placeholder.")
    if not apply_:
        print("  [dry-run] no changes written.")
        return

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("  .env.production.example sanitized.")


def find_env_production(repo_root):
    candidates = [
        os.path.join(repo_root, ".env.production"),
        os.path.join(repo_root, ".env"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def parse_env_file(path):
    env = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def step_rotate_jwt_secret(repo_root, apply_):
    print("\n[4/6] Rotating JWT_SECRET")
    env_path = find_env_production(repo_root)
    if not env_path:
        print("  [WARN] No .env.production or .env found in repo root.")
        print("         Tell me its actual path and I'll adjust the script,")
        print("         or set JWT_SECRET manually using:")
        print('           python3 -c "import secrets; print(secrets.token_hex(32))"')
        return None

    new_secret = secrets.token_hex(32)
    print(f"  Env file: {env_path}")
    if not apply_:
        print("  [dry-run] would generate a new JWT_SECRET and update/insert it there.")
        return None

    backup_path = env_path + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(env_path, backup_path)
    print(f"  Backed up existing env file to {backup_path}")

    with open(env_path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith("JWT_SECRET="):
            lines[i] = f"JWT_SECRET={new_secret}\n"
            replaced = True
            break
    if not replaced:
        lines.append(f"\nJWT_SECRET={new_secret}\n")

    with open(env_path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)

    print("  JWT_SECRET rotated. Every existing session (including the two")
    print("  leaked cookies) will stop validating as soon as the service restarts.")
    return env_path


def step_rotate_passwords(env_path, apply_):
    print("\n[5/6] Rotating admin/editor passwords in the database")
    if not env_path:
        print("  Skipped: no env file was found in step 4, so DB creds are unknown.")
        return
    if not os.path.exists(env_path):
        print("  Skipped: env file path no longer exists.")
        return

    env = parse_env_file(env_path)
    db_host = env.get("DB_HOST", "127.0.0.1")
    db_port = int(env.get("DB_PORT", 3306))
    db_user = env.get("DB_USER", "root")
    db_password = env.get("DB_PASSWORD", "")
    db_name = env.get("DB_NAME", "monitoring_hub")

    if not apply_:
        print(f"  [dry-run] would connect to {db_user}@{db_host}:{db_port}/{db_name}")
        print(f"  [dry-run] would prompt for new passwords for: {', '.join(ACCOUNTS_TO_ROTATE)}")
        return

    try:
        import bcrypt
        import mysql.connector
    except ImportError as e:
        die(f"Missing dependency ({e}). Run inside the app's venv where "
            f"requirements.txt is installed (bcrypt, mysql-connector-python).")

    new_hashes = {}
    for username in ACCOUNTS_TO_ROTATE:
        while True:
            pw1 = getpass.getpass(f"  New password for '{username}' (input hidden): ")
            pw2 = getpass.getpass(f"  Confirm password for '{username}': ")
            if pw1 != pw2:
                print("  Passwords didn't match, try again.")
                continue
            if len(pw1) < 12:
                print("  Use at least 12 characters.")
                continue
            break
        new_hashes[username] = bcrypt.hashpw(pw1[:72].encode(), bcrypt.gensalt()).decode()

    try:
        conn = mysql.connector.connect(
            host=db_host, port=db_port, user=db_user,
            password=db_password, database=db_name,
            connection_timeout=10,
        )
        cur = conn.cursor()
        for username, pw_hash in new_hashes.items():
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE username = %s",
                (pw_hash, username),
            )
            print(f"  {username}: {cur.rowcount} row(s) updated.")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        die(f"Database update failed: {e}\n"
            f"No changes to JWT_SECRET or files were rolled back — "
            f"fix DB access and re-run just this step, or update "
            f"password_hash manually with the printed bcrypt hashes below:\n"
            + "\n".join(f"    {u}: {h}" for u, h in new_hashes.items()))

    print("  Passwords rotated. Old admin123/editor123 no longer work.")


def step_print_manual_followup(repo_root, apply_):
    print("\n[6/6] Manual follow-up (do these yourself)")
    print("""
  A) Restart the backend so the new JWT_SECRET takes effect immediately:
       sudo systemctl restart monitoring-hub
       sudo systemctl status monitoring-hub --no-pager

  B) Review what's staged, then commit and push from the dev server
     (or from wherever you hold push credentials for this repo):
       git status
       git diff --cached
       git commit -m "security: remove leaked session cookies/credentials, rotate JWT_SECRET, harden .gitignore"
       git push origin <your-branch>

  C) The leaked tokens/passwords are still visible in OLD commits even
     after this push, because git history isn't rewritten by a normal
     commit. Since the repo is public, treat step (D)/(E) below as
     mandatory, not optional:

  D) Purge history (choose one, both require a force-push):
       # Option 1: git-filter-repo (recommended, install via pip)
       pip install git-filter-repo
       git filter-repo --path cookies.txt --path cookies_editor.txt \\
         --path login.json --path login_editor.json \\
         --path grant_editor_scope.json --path create_viewer_bad.json \\
         --path create_viewer_ok.json --invert-paths
       git push origin --force --all
       git push origin --force --tags

       # Option 2: BFG Repo-Cleaner (simpler CLI, needs Java)
       #   bfg --delete-files cookies.txt --delete-files cookies_editor.txt \\
       #       --delete-files login.json --delete-files login_editor.json .
       #   git reflog expire --expire=now --all && git gc --prune=now --aggressive
       #   git push origin --force --all

     WARNING: force-push rewrites shared history. If anyone else has
     cloned this repo, they'll need to re-clone after this.

  E) Because the repo has been public with these tokens in it, also
     assume the JWT_SECRET rotation done in step 4 was necessary but
     not sufficient by itself for peace of mind — the admin/editor
     PASSWORDS were exposed in plaintext (login.json/login_editor.json),
     so rotating them (step 5) was the real fix for those two accounts,
     independent of the JWT_SECRET rotation.
""")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                         help="Actually make changes. Without this, runs in dry-run mode.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Explicitly request dry-run (default if neither flag given).")
    args = parser.parse_args()
    apply_ = args.apply and not args.dry_run

    repo_root = find_repo_root()
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    step_backup_and_untrack(repo_root, apply_)
    step_gitignore(repo_root, apply_)
    step_sanitize_env_example(repo_root, apply_)
    env_path = step_rotate_jwt_secret(repo_root, apply_)
    step_rotate_passwords(env_path, apply_)
    step_print_manual_followup(repo_root, apply_)

    if not apply_:
        print("\nThis was a DRY RUN. Re-run with --apply to make real changes.")


if __name__ == "__main__":
    main()
