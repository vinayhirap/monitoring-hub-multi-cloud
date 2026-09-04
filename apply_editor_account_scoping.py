#!/usr/bin/env python3
"""
apply_editor_account_scoping.py — lets editors get the same
"Account Access" scoping viewers already have in Add New User. The
backend (POST /api/users/{id}/access) already supports scoping any
role — this was purely a frontend gate that only showed/submitted the
Account Access field when role === "viewer".

Without this, a newly created editor always has unrestricted access
to every AWS account the app knows about (editors can configure
alerts and onboard accounts), which is rarely the intent for an
editor who should only work within one team's accounts.

Usage:
    python apply_editor_account_scoping.py --dry-run
    python apply_editor_account_scoping.py
"""
import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-editor-account-scoping"
TARGET = "frontend/src/pages/UserManagement.jsx"

PATCHES = [
    (r'''      // Per-account access grants -- POST /api/users/{id}/access with a
      // `scopes` array is the real RBAC endpoint (app/api/admin/users.py).
      // This used to PATCH /api/users/{id}/accounts, a route that was
      // never implemented server-side, so account access silently never
      // applied no matter what was selected here (the request 404'd and
      // was swallowed by .catch(() => {})).
      if (form.role === "viewer" && form.accountIds?.length > 0) {
        await apiFetch(`/api/users/${created.id}/access`, {
          method: "POST",
          body: JSON.stringify({
            scopes: form.accountIds.map(id => ({ cloud: "aws", account_ref_id: Number(id) })),
          }),
        }).catch(() => {});
      }
''', r'''      // Per-account access grants -- POST /api/users/{id}/access with a
      // `scopes` array is the real RBAC endpoint (app/api/admin/users.py).
      // This used to PATCH /api/users/{id}/accounts, a route that was
      // never implemented server-side, so account access silently never
      // applied no matter what was selected here (the request 404'd and
      // was swallowed by .catch(() => {})).
      // Editors are scoped the same way viewers are -- an editor with
      // no scope restriction can configure alerts/onboard accounts
      // across every AWS account this app knows about, which is rarely
      // the intent; the backend (add_user_access) has always supported
      // scoping any role, this was purely a frontend gate.
      if ((form.role === "viewer" || form.role === "editor") && form.accountIds?.length > 0) {
        await apiFetch(`/api/users/${created.id}/access`, {
          method: "POST",
          body: JSON.stringify({
            scopes: form.accountIds.map(id => ({ cloud: "aws", account_ref_id: Number(id) })),
          }),
        }).catch(() => {});
      }
'''),
    (r'''              {form.role === "viewer" && accounts.length > 0 && (
                <div className="mfield">
                  <label>Account Access</label>
''', r'''              {(form.role === "viewer" || form.role === "editor") && accounts.length > 0 && (
                <div className="mfield">
                  <label>Account Access</label>
'''),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    full_path = REPO_ROOT / TARGET
    if not full_path.exists():
        print(f"MISSING FILE: {TARGET}", file=sys.stderr)
        sys.exit(1)

    text = full_path.read_text(encoding="utf-8")
    original = text
    problems = []
    for old, new in PATCHES:
        if new in text:
            continue
        if old not in text:
            problems.append(old[:70])
            continue
        text = text.replace(old, new, 1)

    if problems:
        for p in problems:
            print(f"ABORTED: anchor not found — {p!r}", file=sys.stderr)
        sys.exit(1)

    if text == original:
        print("Already applied — nothing to do.")
        return

    if args.dry_run:
        print(f"[DRY RUN] would patch: {TARGET}")
        return

    backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
    if not backup_path.exists():
        shutil.copy2(full_path, backup_path)
    full_path.write_text(text, encoding="utf-8")
    print(f"PATCHED: {TARGET}  (backup: {backup_path.name})")
    print("\nNext: cd frontend && npm install && npm run build")


if __name__ == "__main__":
    main()
