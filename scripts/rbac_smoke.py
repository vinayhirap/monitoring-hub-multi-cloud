#!/usr/bin/env python3
"""
Runtime check that the RBAC matrix matches what a live server really does (audit E5).

Run it against DEV (or a staging copy), with one throw-away test user per role you want to check:

    python3 scripts/rbac_smoke.py --base http://127.0.0.1 --roles viewer,editor
    # prompts for each user's name and password (nothing is put on the command line or in shell history);
    # or set RBAC_VIEWER_USER / RBAC_VIEWER_PASSWORD, RBAC_EDITOR_USER / RBAC_EDITOR_PASSWORD

What it does, per role:
  1. signs in, then asks the server which permissions that user has (GET /api/permissions/me);
  2. for every GET route in the matrix that this user must NOT reach, calls it and expects 401/403.
     These calls are refused by the guard before the endpoint runs, so they cannot change anything. If one returns
     200 the guard is missing: that is the finding.
  3. with --positive, also calls the routes the user SHOULD reach and expects anything except 401/403. Off by default
     because a permitted call really runs (some start AWS lookups); a short skip list excludes the obvious ones.

State-changing routes (POST/PUT/PATCH/DELETE) are never called. Exit status is 1 if any mismatch is found.
"""
import argparse
import getpass
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

SKIP_POSITIVE = re.compile(r"/check$|console|export|/live/|/explain|/rca|/search$|download|/reports?/")


def expected_allowed(route, perms, role_is_admin):
    """Mirror of the guards: public/authenticated pass; permission needs ALL listed codes (admin holds all)."""
    if route["level"] in ("public", "authenticated"):
        return True
    if role_is_admin:
        return True
    if route["level"] == "permission":
        return all(p in perms for p in route["permissions"])
    if route["level"] == "role":
        return None                      # role-gated: cannot be derived from permissions alone
    return None                          # custom guard: reviewed by hand


def login(session, base, user, password):
    r = session.post(f"{base}/api/auth/login", json={"username": user, "password": password}, timeout=15)
    r.raise_for_status()
    me = session.get(f"{base}/api/permissions/me", timeout=15)
    me.raise_for_status()
    return me.json()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1")
    ap.add_argument("--roles", required=True, help="comma-separated labels, e.g. viewer,editor")
    ap.add_argument("--positive", action="store_true", help="also call permitted GET routes (they really execute)")
    args = ap.parse_args()

    from rbac_matrix import build_matrix
    routes = [r for r in build_matrix() if "GET" in r["methods"] and "{" not in r["path"] and r["level"] != "public"]
    print(f"{len(routes)} parameterless authenticated GET routes in the matrix")

    failures = 0
    for label in [x.strip() for x in args.roles.split(",") if x.strip()]:
        key = label.upper()
        user = os.environ.get(f"RBAC_{key}_USER") or input(f"{label} username: ")
        password = os.environ.get(f"RBAC_{key}_PASSWORD") or getpass.getpass(f"{label} password: ")
        s = requests.Session()
        try:
            me = login(s, args.base, user, password)
        except Exception as e:
            print(f"[{label}] sign-in failed: {e}")
            failures += 1
            continue
        perms, role = set(me.get("permissions", [])), str(me.get("role", "")).lower()
        is_admin = role == "admin"
        print(f"\n[{label}] role={role} with {len(perms)} permissions")
        checked = bad = 0
        for r in routes:
            allowed = expected_allowed(r, perms, is_admin)
            if allowed is None:
                continue
            if allowed and not args.positive:
                continue
            if allowed and SKIP_POSITIVE.search(r["path"]):
                continue
            resp = s.get(f"{args.base}{r['path']}", timeout=30, allow_redirects=False)
            checked += 1
            blocked = resp.status_code in (401, 403)
            if allowed == blocked:
                bad += 1
                want = "allowed" if allowed else "blocked (401/403)"
                print(f"  MISMATCH {r['path']}: expected {want}, got HTTP {resp.status_code}")
        print(f"  checked {checked} routes, {bad} mismatch(es)")
        failures += bad
    print("\nOK: server matches the matrix" if not failures else f"\n{failures} problem(s) found")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
