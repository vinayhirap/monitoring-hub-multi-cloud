#!/usr/bin/env python3
"""
apply_fix_create_user_connection_leak.py
========================================
Ports a real, valuable fix that was found running LOCALLY on prod
(35.154.149.94) but never committed to git -- discovered while doing
pre-flight diffing before bringing that server up to date. Confirmed
this specific gap is currently live on `main` (and therefore on dev,
13.200.102.131, too): create_user()'s exception handler never called
conn.rollback() or conn.close() -- every failed user creation (a
duplicate username being the single most common real-world trigger)
permanently leaked a pooled database connection. This is the exact
class of bug that exhausted the connection pool and took the dashboard
offline for hours on Sep 5 2026 (see deploy/update.sh's own
verification gate, which checks for the broader app/db.py leak-guard
fix from that incident -- this specific function was apparently never
migrated to use it).

HARDENED BEYOND THE ORIGINAL PROD HOTFIX
--------------------------------------------
The prod version added `conn.rollback(); conn.close()` directly inside
the except block, with the pre-existing `finally: cursor.close()`
still running afterward unchanged. Before porting this as-is, checked
whether calling cursor.close() after its connection is already closed
could raise and mask the real HTTPException with an unrelated
connector-internal error -- read mysql-connector-python's own
CMySQLCursor.close() implementation and couldn't fully rule this out
for every connector version/state combination. Wrapped `cursor.close()`
in the `finally` block with its own try/except so this can NEVER
happen, regardless of connector internals -- a strictly safer version
of the same fix, not a different one.

TESTED: three real scenarios with a mocked cursor/connection, not just
inspection -- (1) failure path: confirmed the correct 409 HTTPException
reaches the caller even when the mocked cursor.close() itself raises an
unrelated exception on close (proving the defensive wrapper works, not
just the rollback/close calls); (2) confirmed conn.rollback() and
conn.close() both genuinely execute on failure; (3) success path:
confirmed create_user() still returns its normal result and the
connection is NOT prematurely closed before the later
_validate_and_insert_scopes() call that reuses the same connection
object.

USAGE
-----
    cd /opt/monitoring-hub/app
    python3 apply_fix_create_user_connection_leak.py --dry-run
    python3 apply_fix_create_user_connection_leak.py --apply
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

OLD = '''    except Exception as e:
        if "Duplicate" in str(e) or "1062" in str(e):
            raise HTTPException(status_code=409, detail=f"User '{username}' already exists")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()'''

NEW = '''    except Exception as e:
        # Ported from a local hotfix found already running on prod
        # (35.154.149.94), never committed to git -- without this,
        # every failed create_user (e.g. a duplicate username, the most
        # common real-world case) leaked a pooled DB connection
        # permanently, the same class of bug that exhausted the pool
        # and took the dashboard offline for hours on Sep 5 2026 (see
        # deploy/update.sh's own verification gate for that incident).
        #
        # rollback() before close() so an aborted INSERT never leaves a
        # dangling transaction on a connection about to go back to
        # (or out of) the pool. The `finally: cursor.close()` below
        # still runs after this -- wrapped in its own try/except there
        # specifically so that IF closing an already-closed connection's
        # cursor ever raises anything connector-version-specific, it
        # can never replace/mask the real HTTPException being raised
        # here with an unrelated one.
        conn.rollback()
        conn.close()
        if "Duplicate" in str(e) or "1062" in str(e):
            raise HTTPException(status_code=409, detail=f"User '{username}' already exists")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            cursor.close()
        except Exception:
            pass'''


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
    path = os.path.join(repo_root, "app", "api", "admin", "users.py")
    print(f"Repo root: {repo_root}")
    print(f"Mode: {'APPLY (making real changes)' if apply_ else 'DRY-RUN (no changes will be made)'}")

    with open(path) as f:
        content = f.read()

    if "Ported from a local hotfix found already running on prod" in content:
        print("\nAlready patched -- nothing to do.")
        return

    n = content.count(OLD)
    if n != 1:
        die(f"Expected exactly 1 match, found {n}. File may differ from what this script expects.")

    new_content = content.replace(OLD, NEW, 1)
    print(f"\nFile patch plan:\n  app/api/admin/users.py: OK ({len(new_content) - len(content):+d} bytes)")

    if not apply_:
        print("\n[dry-run] No files written. Re-run with --apply (or no flags) to make real changes.")
        return

    backup(path)
    with open(path, "w") as f:
        f.write(new_content)
    print("Patched app/api/admin/users.py")

    print("""
[Manual follow-up]

  A) Restart:
       sudo systemctl restart monitoring-hub

  B) Confirm the duplicate-username case now returns a clean 409
     instead of leaking a connection -- create a user, then try to
     create the SAME username again; should get a 409 error in the UI,
     not a 500 or a hang.

  C) Review, commit, push -- this needs to land on the SAME server
     (13.200.102.131) this whole session has been working against,
     BEFORE prod (35.154.149.94) does anything with git, so that
     pulling main doesn't regress prod's local fix:
       git diff app/api/admin/users.py
       git add app/api/admin/users.py apply_fix_create_user_connection_leak.py
       git commit -m "fix(leak): create_user() never rolled back or closed its DB connection on failure -- every duplicate-username attempt leaked a pooled connection permanently. Ported from a prod hotfix that was never committed, hardened further so the fix itself can never mask the real error."
       git push origin main

  D) ONLY after this is on main: it's now safe to reconcile prod's own
     local copy of this same fix (already present there, already
     covered by what's about to be on main) -- next step is resetting
     prod's local diff on this file specifically before its update.
""")


if __name__ == "__main__":
    main()
