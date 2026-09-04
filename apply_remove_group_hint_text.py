#!/usr/bin/env python3
"""
apply_remove_group_hint_text.py — removes the long explanatory
sentence under the Group dropdown in Add New User ("Sets the role
below automatically (L1 = Viewer, L2 = Editor, L3 = Admin) and
inherits this group's access plus every parent group's access.") —
the Role field right below it already visibly locks/shows the same
information, so the sentence was redundant. The "No organization
groups set up yet" hint (only shown when there are genuinely zero
groups to pick from) is kept — that one's still useful, not
decorative.

Usage:
    python apply_remove_group_hint_text.py --dry-run
    python apply_remove_group_hint_text.py
"""
import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-remove-group-hint-text"

OLD = r'''                <label>Group (optional)</label>
                <select
                  value={form.groupId}
                  onChange={e => {
                    const groupId = e.target.value;
                    const selected = groups.find(g => String(g.id) === groupId);
                    const impliedRole = selected ? GROUP_LEVEL_ROLE[selected.level] : null;
                    setForm(f => ({
                      ...f,
                      groupId,
                      role: impliedRole || f.role,
                      accountIds: impliedRole ? [] : f.accountIds,
                    }));
                  }}
                >
                  <option value="">No group</option>
                  {groups.map(g => (
                    <option key={g.id} value={g.id}>
                      {"— ".repeat(g.level === "L3" ? 2 : g.level === "L2" ? 1 : 0)}{g.name} ({g.level})
                    </option>
                  ))}
                </select>
                <span className="field-hint">
                  {groups.length === 0
                    ? "No organization groups set up yet."
                    : "Sets the role below automatically (L1 = Viewer, L2 = Editor, L3 = Admin) and inherits this group's access plus every parent group's access."}
                </span>
              </div>
'''
NEW = r'''                <label>Group (optional)</label>
                <select
                  value={form.groupId}
                  onChange={e => {
                    const groupId = e.target.value;
                    const selected = groups.find(g => String(g.id) === groupId);
                    const impliedRole = selected ? GROUP_LEVEL_ROLE[selected.level] : null;
                    setForm(f => ({
                      ...f,
                      groupId,
                      role: impliedRole || f.role,
                      accountIds: impliedRole ? [] : f.accountIds,
                    }));
                  }}
                >
                  <option value="">No group</option>
                  {groups.map(g => (
                    <option key={g.id} value={g.id}>
                      {"— ".repeat(g.level === "L3" ? 2 : g.level === "L2" ? 1 : 0)}{g.name} ({g.level})
                    </option>
                  ))}
                </select>
                {groups.length === 0 && (
                  <span className="field-hint">No organization groups set up yet.</span>
                )}
              </div>
'''

TARGET = "frontend/src/pages/UserManagement.jsx"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    full_path = REPO_ROOT / TARGET
    if not full_path.exists():
        print(f"MISSING FILE: {TARGET}", file=sys.stderr)
        sys.exit(1)

    text = full_path.read_text(encoding="utf-8")
    if NEW in text:
        print("Already applied — nothing to do.")
        return
    if OLD not in text:
        print(f"ABORTED: anchor not found in {TARGET} — the file has "
              "changed since this script was written, refusing to guess.", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(f"[DRY RUN] would patch: {TARGET}")
        return

    backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
    if not backup_path.exists():
        shutil.copy2(full_path, backup_path)
    full_path.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print(f"PATCHED: {TARGET}  (backup: {backup_path.name})")
    print("\nNext: cd frontend && npm install && npm run build")


if __name__ == "__main__":
    main()
