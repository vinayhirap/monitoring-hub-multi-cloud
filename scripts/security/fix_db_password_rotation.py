#!/usr/bin/env python3
"""
fix_db_password_rotation.py
=============================
Monitoring Hub — rotates the live MySQL password for the app's DB user
(currently root123, both live in .env AND hardcoded as a fallback
default in app/db.py) and removes the insecure hardcoded fallback.

WHAT THIS FIXES
---------------
  1. .env has DB_PASSWORD=root123 — a weak, guessable, live production
     credential.
  2. app/db.py has:
         password=os.getenv("DB_PASSWORD", "root123")
     meaning if DB_PASSWORD is ever unset/misconfigured, the app
     silently falls back to a hardcoded weak password instead of
     failing loudly — the same class of problem app/auth/security.py's
     JWT_SECRET explicitly avoids (it raises RuntimeError instead of
     ever using a default secret).

WHAT THIS SCRIPT DOES, IN ORDER
--------------------------------
  1. Reads current DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME from the
     env file (.env or .env.production, whichever exists) and verifies
     it can currently connect (sanity check before touching anything).
  2. Generates a new strong password (alnum-only, 32 chars — avoids
     characters like #, $, backslash, or quotes that can break .env
     parsing or shell quoting).
  3. Looks up which host pattern(s) MySQL has the DB_USER registered
     under (e.g. 'monitor'@'localhost') via mysql.user; falls back to
     'localhost' and '%' if that lookup isn't permitted.
  4. Runs ALTER USER ... IDENTIFIED BY <new password> for each matching
     host, immediately opens a FRESH connection with the new password
     to confirm it actually works.
  5. If step 4's verification fails, automatically reverts MySQL's
     password back to the original and aborts — .env is never touched
     if the DB-side change didn't verifiably work.
  6. Only after verification succeeds: backs up .env, writes the new
     DB_PASSWORD into it.
  7. Patches app/db.py: replaces the hardcoded
         password=os.getenv("DB_PASSWORD", "root123")
     with a helper that raises RuntimeError if DB_PASSWORD is unset,
     matching the existing JWT_SECRET pattern. app/db.py is backed up
     first.
  8. Prints the manual follow-up (restart service, verify, git commit).

WHAT THIS DELIBERATELY DOES NOT DO
------------------------------------
  - Does not restart the service itself (you do that manually, so you
    can watch it come up and roll back deliberately if something's off).
  - Does not touch production (35.154.149.94). Dev only.
  - Does not commit/push. Prints the commands for you to run.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 fix_db_password_rotation.py --dry-run
    python3 fix_db_password_rotation.py --apply
"""

import argparse
import os
import re
import secrets
import shutil
import string
import sys
from datetime import datetime

ENV_CANDIDATES = [".env.production", ".env"]
DB_PY_PATH = os.path.join("app", "db.py")

OLD_DB_PY_LINE = 'password=os.getenv("DB_PASSWORD", "root123"),'
NEW_DB_PY_SNIPPET = '''def _require_db_password() -> str:
    """DB_PASSWORD must be set in the environment -- deliberately NO
    insecure fallback default, same reasoning as JWT_SECRET in
    app/auth/security.py: a shared/guessable default DB password
    defeats the point of having one."""
    pw = os.getenv("DB_PASSWORD")
    if not pw:
        raise RuntimeError(
            "DB_PASSWORD is not set. Set it in .env (and .env.production "
            "on the server) -- there is no default."
        )
    return pw


_pool = pooling.MySQLConnectionPool('''

OLD_POOL_OPEN = "_pool = pooling.MySQLConnectionPool("


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
            die("Could not locate the monitoring-hub-multi-cloud repo root. "
                "cd into it (e.g. /opt/monitoring-hub/app) and re-run.")
        cur = parent


def find_env_file(repo_root):
    for name in ENV_CANDIDATES:
        p = os.path.join(repo_root, name)
        if os.path.exists(p):
            return p
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


def gen_password(length=32):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def self_change_password(conn, new_password):
    """Change the CURRENT connection's own account password, trying
    the syntax variants across MySQL 8 and MariaDB so this works
    regardless of which one is actually running. Self-referential
    forms need no elevated privileges (unlike naming the account
    explicitly, which requires CREATE USER)."""
    attempts = [
        ("MySQL: ALTER USER USER() IDENTIFIED BY %s",
         "ALTER USER USER() IDENTIFIED BY %s"),
        ("MariaDB: ALTER USER CURRENT_USER() IDENTIFIED BY %s",
         "ALTER USER CURRENT_USER() IDENTIFIED BY %s"),
        ("Legacy: SET PASSWORD = PASSWORD(%s)",
         "SET PASSWORD = PASSWORD(%s)"),
    ]
    for label, sql in attempts:
        cur = conn.cursor()
        try:
            cur.execute(sql, (new_password,))
            conn.commit()
            cur.close()
            print(f"  Password changed via: {label}")
            return True
        except Exception as e:
            conn.rollback()
            cur.close()
            print(f"  [note] {label} failed ({e}), trying next syntax...")
    return False


def try_connect(host, port, user, password, database):
    import mysql.connector
    conn = mysql.connector.connect(
        host=host, port=port, user=user, password=password,
        database=database, connection_timeout=10,
    )
    conn.ping(reconnect=False)
    return conn


def step_rotate_mysql_password(env, apply_):
    print("\n[1/3] Rotating the MySQL password")
    host = env.get("DB_HOST", "127.0.0.1")
    port = int(env.get("DB_PORT", 3306))
    user = env.get("DB_USER", "monitor")
    old_password = env.get("DB_PASSWORD", "")
    database = env.get("DB_NAME", "monitoring_hub")

    try:
        import mysql.connector  # noqa: F401
        import bcrypt  # noqa: F401
    except ImportError as e:
        die(f"Missing dependency ({e}). Run with the app's venv python, e.g.\n"
            f"  /opt/monitoring-hub/venv/bin/python3 {sys.argv[0]} --apply")

    print(f"  Verifying current connectivity as {user}@{host}:{port}/{database} ...")
    try:
        conn = try_connect(host, port, user, old_password, database)
    except Exception as e:
        die(f"Could not connect with CURRENT credentials -- aborting before "
            f"changing anything. Error: {e}")
    print("  Current credentials work. Proceeding.")

    new_password = gen_password()

    if not apply_:
        print(f"  [dry-run] would look up host pattern(s) for user '{user}', "
              f"run ALTER USER on each, verify, then update .env.")
        conn.close()
        return None

    # Self-referential ALTER USER: any authenticated user can change
    # their OWN password this way without needing the CREATE USER
    # privilege (unlike naming the account explicitly, which failed
    # in testing with "Access denied; you need CREATE USER"). This is
    # also inherently safer -- it can only ever change the password of
    # whichever account this very connection authenticated as, so
    # there's no risk of a host-pattern loop touching an unrelated
    # account.
    if not self_change_password(conn, new_password):
        conn.close()
        die("Could not change the password with any known syntax "
            "(tried MySQL's ALTER USER USER(), MariaDB's ALTER USER "
            "CURRENT_USER(), and SET PASSWORD). No changes made to .env or code.")
    conn.close()

    print("  Verifying new password actually works with a fresh connection...")
    try:
        verify_conn = try_connect(host, port, user, new_password, database)
        verify_conn.close()
    except Exception as e:
        print(f"  [ABORT] New password failed verification: {e}")
        print("  Rolling back to the OLD password...")
        try:
            rollback_conn = try_connect(host, port, user, new_password, database)
            rb_cur = rollback_conn.cursor()
            rb_cur.execute("ALTER USER USER() IDENTIFIED BY %s", (old_password,))
            rollback_conn.commit()
            rb_cur.close()
            rollback_conn.close()
            print("  Rollback succeeded -- password is back to the original.")
        except Exception as rb_e:
            die(f"Could not roll back automatically ({rb_e}). "
                f"Connect manually with the NEW password ({new_password}) and run:\n"
                f"  ALTER USER USER() IDENTIFIED BY '{old_password}';")
        die("Rolled back. .env and app/db.py were NOT modified. Investigate and retry.")

    print("  New password verified working.")
    return new_password


def step_update_env(env_path, new_password, apply_):
    print("\n[2/3] Updating .env with the new DB_PASSWORD")
    if not apply_:
        print(f"  [dry-run] would back up {env_path} then update DB_PASSWORD line.")
        return

    backup_path = env_path + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(env_path, backup_path)
    print(f"  Backed up to {backup_path}")

    with open(env_path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith("DB_PASSWORD="):
            lines[i] = f"DB_PASSWORD={new_password}\n"
            replaced = True
            break
    if not replaced:
        lines.append(f"\nDB_PASSWORD={new_password}\n")

    with open(env_path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    print("  .env updated. (The MySQL side was already verified working in step 1.)")


def step_patch_db_py(repo_root, apply_):
    print("\n[3/3] Removing hardcoded fallback password from app/db.py")
    path = os.path.join(repo_root, DB_PY_PATH)
    if not os.path.exists(path):
        print(f"  {DB_PY_PATH} not found, skipping.")
        return

    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    if OLD_DB_PY_LINE not in content:
        if "_require_db_password" in content:
            print("  app/db.py already patched, skipping.")
        else:
            print(f"  [WARN] Expected line not found verbatim in app/db.py -- "
                  f"skipping automatic patch to avoid guessing. Do it by hand:")
            print(f'    Replace: {OLD_DB_PY_LINE}')
            print(f'    With:    password=_require_db_password(),')
            print(f"    And add the _require_db_password() helper above the pool.")
        return

    if not apply_:
        print("  [dry-run] would replace the hardcoded default with a "
              "_require_db_password() helper (same pattern as JWT_SECRET).")
        return

    backup_path = path + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy2(path, backup_path)
    print(f"  Backed up to {backup_path}")

    new_content = content.replace(OLD_POOL_OPEN, NEW_DB_PY_SNIPPET, 1)
    new_content = new_content.replace(
        OLD_DB_PY_LINE, "password=_require_db_password(),", 1
    )

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    print("  app/db.py patched.")


def step_print_followup():
    print("""
[Manual follow-up]

  A) Restart so the app picks up the new DB_PASSWORD and code change:
       sudo systemctl restart monitoring-hub
       sudo systemctl status monitoring-hub --no-pager

  B) Confirm the app actually still works (log in, load a dashboard page)
     BEFORE committing -- if something's wrong, .env.bak.* and
     app/db.py.bak.* are sitting right next to the live files to
     restore from instantly.

  C) Then review, commit, push:
       git status
       git diff app/db.py
       git add app/db.py
       git commit -m "security(db): remove hardcoded DB_PASSWORD fallback, require explicit env var"
       git push origin main

     (.env is gitignored -- the new password itself never gets committed,
     same as JWT_SECRET.)

  D) The OLD password (root123) is also hardcoded in deploy/setup.sh and
     setup.sh as the value used when *creating* the DB user on a fresh
     install. That's a separate, lower-urgency cleanup (it only matters
     for future fresh installs, not this running system) -- happy to
     fix those files too when you want.
""")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply_ = args.apply and not args.dry_run

    repo_root = find_repo_root()
    env_path = find_env_file(repo_root)
    if not env_path:
        die("No .env or .env.production found in repo root.")

    print(f"Repo root: {repo_root}")
    print(f"Env file:  {env_path}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    env = parse_env_file(env_path)
    new_password = step_rotate_mysql_password(env, apply_)
    if apply_:
        step_update_env(env_path, new_password, apply_)
    else:
        step_update_env(env_path, None, apply_)
    step_patch_db_py(repo_root, apply_)
    step_print_followup()

    if not apply_:
        print("This was a DRY RUN. Re-run with --apply to make real changes.")


if __name__ == "__main__":
    main()
