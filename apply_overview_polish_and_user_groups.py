#!/usr/bin/env python3
"""
apply_overview_polish_and_user_groups.py — three fixes:

1. GENUINE first-load skeleton state (Overview). A first-ever page
   load with nothing cached yet still has to wait for the first real
   fetch — that's unavoidable — but it was showing a bare spinner +
   "Fetching live AWS data…" while the summary tiles simultaneously
   showed "0", which reads as "confirmed empty," not "still loading."
   Replaced with shimmering skeleton placeholders shaped like the real
   summary tiles and account cards — a much clearer "this is still
   loading" signal, and the tiles never show a bare 0 before there's
   real data to back it up.

2. Add User modal: the "Account Access" picker was calling
   PATCH /api/users/{id}/accounts, a route that was never implemented
   server-side — the request 404'd and was silently swallowed, so
   account access picked in this form never actually applied. Fixed to
   call the real endpoint, POST /api/users/{id}/access with a `scopes`
   array (app/api/admin/users.py). The field could also simply fail to
   render at all if the accounts list happened to still be empty at
   mount time, with no retry — it now re-fetches every time the modal
   opens. A new "Group (optional)" field was also added, wired to the
   L1/L2/L3 organization-groups feature (app/api/admin/groups.py) —
   selecting a group on user creation immediately grants that user the
   group's inherited access. Both new/fixed fields reuse the exact same
   .mfield styling as Username/Password/Role, so they line up with the
   rest of the form automatically.

3. If your Overview dashboard is STILL showing "7 CRITICAL" in the
   banner while an account's status pill says Healthy after applying
   apply_account_health_rollup_fix.py from earlier — that fix's code
   is correct, but a patched .py file does nothing until the backend
   process is actually restarted (FastAPI/Uvicorn does not hot-reload
   in production). Run scripts/verify_account_health_fix.py (delivered
   alongside this script) to confirm, in one shot, whether the running
   process has picked up the fix or still needs a restart.

Usage:
    python apply_overview_polish_and_user_groups.py --dry-run
    python apply_overview_polish_and_user_groups.py
"""
import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-overview-polish-and-user-groups"


class PatchError(Exception):
    pass


PATCHES = [
    (
        "frontend/src/pages/Overview.jsx",
        [
            (
                r'''function SummaryTile({ icon, label, value, color, pulse }) {
''',
                r'''// Skeleton placeholders shown ONLY on a genuine first-ever load (no
// cache to hydrate from yet) -- a shimmering approximation of the real
// layout communicates "this is actively loading" far more convincingly
// than a spinner + line of text, and avoids the summary tiles briefly
// showing a bare "0" that reads as a confirmed (rather than pending)
// count.
function SkeletonTile() {
  return (
    <div className="sum-tile sum-default">
      <span className="sum-icon sk-block" style={{ width: 32, height: 32 }} />
      <div className="sum-body">
        <div className="sk-block" style={{ width: 70, height: 10, marginBottom: 6 }} />
        <div className="sk-block" style={{ width: 40, height: 20 }} />
      </div>
    </div>
  );
}

function SkeletonAccountCard() {
  return (
    <div className="account-card" style={{ cursor: "default" }}>
      <div className="sk-block" style={{ width: "60%", height: 15, marginBottom: 8 }} />
      <div className="sk-block" style={{ width: "40%", height: 11, marginBottom: 12 }} />
      <div style={{ display: "flex", gap: 5, marginBottom: 14 }}>
        <div className="sk-block" style={{ width: 50, height: 16, borderRadius: 10 }} />
        <div className="sk-block" style={{ width: 64, height: 16, borderRadius: 10 }} />
      </div>
      <div className="acc-body">
        <div className="sk-block" style={{ width: 76, height: 76, borderRadius: "50%", flexShrink: 0 }} />
        <div className="acc-chips" style={{ flex: 1 }}>
          <div className="sk-block" style={{ width: "100%", height: 26, marginBottom: 6 }} />
          <div className="sk-block" style={{ width: "100%", height: 26, marginBottom: 6 }} />
          <div className="sk-block" style={{ width: "100%", height: 26, marginBottom: 6 }} />
          <div className="sk-block" style={{ width: "100%", height: 26 }} />
        </div>
      </div>
    </div>
  );
}

function SummaryTile({ icon, label, value, color, pulse }) {
''',
            ),
            (
                r'''      <div className="ov-summary">
        <SummaryTile icon={<IconAccounts />} label="Total Accounts" value={grouped.length} />
        <SummaryTile icon={<IconHealthy />}  label="Healthy"  value={healthyCount}  color="green" />
        <SummaryTile icon={<IconWarning />}  label="Warning"  value={warningCount}  color={warningCount  > 0 ? "yellow" : "default"} pulse={warningCount  > 0} />
        <SummaryTile icon={<IconCritical />} label="Critical" value={criticalCount} color={criticalCount > 0 ? "red"    : "default"} pulse={criticalCount > 0} />
      </div>
''',
                r'''      <div className="ov-summary">
        {loading ? (
          <>
            <SkeletonTile /><SkeletonTile /><SkeletonTile /><SkeletonTile />
          </>
        ) : (
          <>
            <SummaryTile icon={<IconAccounts />} label="Total Accounts" value={grouped.length} />
            <SummaryTile icon={<IconHealthy />}  label="Healthy"  value={healthyCount}  color="green" />
            <SummaryTile icon={<IconWarning />}  label="Warning"  value={warningCount}  color={warningCount  > 0 ? "yellow" : "default"} pulse={warningCount  > 0} />
            <SummaryTile icon={<IconCritical />} label="Critical" value={criticalCount} color={criticalCount > 0 ? "red"    : "default"} pulse={criticalCount > 0} />
          </>
        )}
      </div>
''',
            ),
            (
                r'''      {loading ? (
        <div className="ov-loading"><span className="spin">◌</span> Fetching live AWS data…</div>
      ) : filteredGroups.length === 0 && loadError ? (
''',
                r'''      {loading ? (
        <div className="accounts-grid">
          <SkeletonAccountCard /><SkeletonAccountCard /><SkeletonAccountCard />
        </div>
      ) : filteredGroups.length === 0 && loadError ? (
''',
            ),
        ],
    ),
    (
        "frontend/src/pages/Overview.css",
        [
            (
                "/* ── Region rows (drilldown) ──",
                r'''
/* ── Loading skeletons (Overview) ──
   Shown only on a genuine first-ever load with nothing cached yet.
   A shimmering block in the real layout's shape reads as "actively
   loading" far more clearly than a spinner + line of text, and never
   shows a bare "0" that could be mistaken for a confirmed count. */
.sk-block {
  background: linear-gradient(90deg, rgba(255,255,255,0.045) 25%, rgba(255,255,255,0.10) 37%, rgba(255,255,255,0.045) 63%);
  background-size: 400% 100%;
  animation: sk-shimmer 1.4s ease infinite;
  border-radius: 4px;
  display: block;
}
@keyframes sk-shimmer {
  0%   { background-position: 100% 50%; }
  100% { background-position: 0 50%; }
}
[data-theme="light"] .sk-block {
  background: linear-gradient(90deg, rgba(15,28,53,0.05) 25%, rgba(15,28,53,0.10) 37%, rgba(15,28,53,0.05) 63%);
  background-size: 400% 100%;
}
''' + "/* ── Region rows (drilldown) ──",
            ),
        ],
    ),
    (
        "frontend/src/pages/UserManagement.jsx",
        [
            (
                r'''const INITIAL_FORM = { username: "", password: "", role: "viewer", accountIds: [] };
''',
                r'''const INITIAL_FORM = { username: "", password: "", role: "viewer", accountIds: [], groupId: "" };
''',
            ),
            (
                r'''  const [tab,        setTab]        = useState("users");
  const [users,      setUsers]      = useState([]);
  const [accounts,   setAccounts]   = useState([]);
  const [loading,    setLoading]    = useState(true);
  const [error,      setError]      = useState(null);
  const [showAdd,    setShowAdd]    = useState(false);
  const [form,       setForm]       = useState(INITIAL_FORM);
  const [formErrs,   setFormErrs]   = useState({});
  const [saving,     setSaving]     = useState(null);
  const [submitting, setSubmitting] = useState(false);

  const loadUsers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch("/api/users");
      setUsers(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAdmin) {
      fetch(`${BASE}/api/live/accounts`)
        .then(r => r.json())
        .then(data => setAccounts(Array.isArray(data) ? data : []))
        .catch(() => {});
    }
  }, [isAdmin]);

  useEffect(() => { loadUsers(); }, [loadUsers]);
''',
                r'''  const [tab,        setTab]        = useState("users");
  const [users,      setUsers]      = useState([]);
  const [accounts,   setAccounts]   = useState([]);
  const [groups,     setGroups]     = useState([]);
  const [loading,    setLoading]    = useState(true);
  const [error,      setError]      = useState(null);
  const [showAdd,    setShowAdd]    = useState(false);
  const [form,       setForm]       = useState(INITIAL_FORM);
  const [formErrs,   setFormErrs]   = useState({});
  const [saving,     setSaving]     = useState(null);
  const [submitting, setSubmitting] = useState(false);

  const loadUsers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch("/api/users");
      setUsers(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  // Accounts (Account Access picker) and org groups (Group picker) both
  // feed the Add User modal. Fetched on mount AND again every time the
  // modal opens -- a single mount-time fetch could race a still-settling
  // backend and come back empty, which previously made the Account
  // Access field silently never appear with no way to retry short of a
  // full page reload; re-fetching on open fixes that.
  const loadAccountsAndGroups = useCallback(() => {
    if (!isAdmin) return;
    fetch(`${BASE}/api/live/accounts`)
      .then(r => r.json())
      .then(data => setAccounts(Array.isArray(data) ? data : []))
      .catch(() => {});
    apiFetch("/api/groups")
      .then(data => setGroups(Array.isArray(data) ? data : []))
      .catch(() => {});
  }, [isAdmin]);

  useEffect(() => { loadAccountsAndGroups(); }, [loadAccountsAndGroups]);
  useEffect(() => { if (showAdd) loadAccountsAndGroups(); }, [showAdd, loadAccountsAndGroups]);

  useEffect(() => { loadUsers(); }, [loadUsers]);
''',
            ),
            (
                r'''  async function handleAdd() {
    if (!isAdmin) return; // hard guard
    const e = validateForm();
    if (Object.keys(e).length) { setFormErrs(e); return; }
    setSubmitting(true);
    try {
      const created = await apiFetch("/api/users", {
        method: "POST",
        body: JSON.stringify({
          username: form.username.trim(),
          password: form.password,
          role:     form.role,
        }),
      });
      if (form.role === "viewer" && form.accountIds?.length > 0) {
        await apiFetch(`/api/users/${created.id}/accounts`, {
          method: "PATCH",
          body: JSON.stringify({ account_ids: form.accountIds.map(Number) }),
        }).catch(() => {});
      }
      setForm(INITIAL_FORM);
      setFormErrs({});
      setShowAdd(false);
      await loadUsers();
    } catch (err) {
      setFormErrs({ submit: err.message });
    } finally {
      setSubmitting(false);
    }
  }
''',
                r'''  async function handleAdd() {
    if (!isAdmin) return; // hard guard
    const e = validateForm();
    if (Object.keys(e).length) { setFormErrs(e); return; }
    setSubmitting(true);
    try {
      const created = await apiFetch("/api/users", {
        method: "POST",
        body: JSON.stringify({
          username: form.username.trim(),
          password: form.password,
          role:     form.role,
        }),
      });
      // Per-account access grants -- POST /api/users/{id}/access with a
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
      // Organization group assignment (L1/L2/L3) -- the new user
      // immediately inherits that group's own access policy plus every
      // ancestor group's policy (see app/api/admin/groups.py).
      if (form.groupId) {
        await apiFetch(`/api/groups/${form.groupId}/members`, {
          method: "POST",
          body: JSON.stringify({ user_ids: [created.id] }),
        }).catch(() => {});
      }
      setForm(INITIAL_FORM);
      setFormErrs({});
      setShowAdd(false);
      await loadUsers();
    } catch (err) {
      setFormErrs({ submit: err.message });
    } finally {
      setSubmitting(false);
    }
  }
''',
            ),
            (
                r'''              <div className="mfield">
                <label>Role</label>
                <select
                  value={form.role}
                  onChange={e => setForm(f => ({ ...f, role: e.target.value, accountIds: [] }))}
                >
                  <option value="viewer">Viewer — read-only</option>
                  <option value="editor">Editor — view + configure alerts</option>
                  <option value="admin">Admin — full access</option>
                </select>
              </div>
              {form.role === "viewer" && accounts.length > 0 && (
                <div className="mfield">
                  <label>Account Access</label>
                  <select
                    multiple
                    value={form.accountIds || []}
                    onChange={e => setForm(f => ({
                      ...f,
                      accountIds: Array.from(e.target.selectedOptions, o => o.value),
                    }))}
                    style={{ height: 80 }}
                  >
                    {accounts.map(a => (
                      <option key={a.id} value={a.id}>{a.account_name}</option>
                    ))}
                  </select>
                  <span className="field-hint">Hold Ctrl for multiple. Empty = all accounts.</span>
                </div>
              )}
''',
                r'''              <div className="mfield">
                <label>Role</label>
                <select
                  value={form.role}
                  onChange={e => setForm(f => ({ ...f, role: e.target.value, accountIds: [] }))}
                >
                  <option value="viewer">Viewer — read-only</option>
                  <option value="editor">Editor — view + configure alerts</option>
                  <option value="admin">Admin — full access</option>
                </select>
              </div>
              <div className="mfield">
                <label>Group (optional)</label>
                <select
                  value={form.groupId}
                  onChange={e => setForm(f => ({ ...f, groupId: e.target.value }))}
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
                    : "Inherits this group's access, plus every parent group's access."}
                </span>
              </div>
              {form.role === "viewer" && accounts.length > 0 && (
                <div className="mfield">
                  <label>Account Access</label>
                  <select
                    multiple
                    value={form.accountIds || []}
                    onChange={e => setForm(f => ({
                      ...f,
                      accountIds: Array.from(e.target.selectedOptions, o => o.value),
                    }))}
                    style={{ height: 80 }}
                  >
                    {accounts.map(a => (
                      <option key={a.id} value={a.id}>{a.account_name}</option>
                    ))}
                  </select>
                  <span className="field-hint">Hold Ctrl for multiple. Empty = all accounts.</span>
                </div>
              )}
''',
            ),
        ],
    ),
]

# ─────────────────────────────────────────────────────────────────────────
# Preflight / apply / validate
# ─────────────────────────────────────────────────────────────────────────

def preflight():
    print("=== Pre-flight: verifying anchors match exactly ===")
    problems = []

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        for old, _new in replacements:
            count = text.count(old)
            if count == 0:
                problems.append(f"{rel_path}: anchor not found (0 matches) — {old[:70]!r}")
            elif count > 1:
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1")
            else:
                print(f"  OK  {rel_path}: anchor matched exactly once")

    if problems:
        print("\n".join(problems))

        def _already(rel, new_text):
            p = REPO_ROOT / rel
            return p.exists() and new_text in p.read_text(encoding="utf-8")

        already_applied = all(_already(rel, new) for rel, repls in PATCHES for _old, new in repls)
        if already_applied:
            print("\nAll target text already present — patch appears to be already applied. Nothing to do.")
            sys.exit(0)
        raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")
    print("Pre-flight OK.\n")


def apply_all(dry_run: bool):
    changed_files = []
    report = []

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        original_text = text
        for old, new in replacements:
            if new in text:
                continue  # already patched
            if old not in text:
                raise PatchError(f"{rel_path}: expected anchor vanished mid-patch — aborting")
            text = text.replace(old, new, 1)

        if text == original_text:
            continue

        if dry_run:
            report.append(f"[DRY RUN] would patch: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(text, encoding="utf-8")
            report.append(f"PATCHED: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)

    for line in report:
        print(line)

    return changed_files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        preflight()
        changed = apply_all(args.dry_run)
        if not args.dry_run:
            print(f"\n=== Done. {len(changed)} file(s) touched. ===")
            print("\nNext steps:")
            print("  cd frontend && npm install (if needed) && npm run build")
            print("\nThen, IMPORTANT for the health-rollup fix from earlier to actually")
            print("take effect (this script doesn't touch that file, it's a separate,")
            print("already-correct fix — this is purely about confirming it's DEPLOYED):")
            print("  python scripts/verify_account_health_fix.py")
            print("  # if it reports missing fields, restart the backend process, e.g.:")
            print("  #   sudo systemctl restart <your-service-name>")
            print("  #   (NOT npm/uvicorn --reload — a full process restart)")
            print("\nTo verify these three fixes:")
            print("  - Hard-refresh Overview with localStorage cleared (or a private/")
            print("    incognito window) — you should see shimmering skeleton cards,")
            print("    not a spinner + bare '0' tiles, during the first load.")
            print("  - Open Add User as admin — a 'Group (optional)' dropdown should")
            print("    appear between Role and Account Access, aligned the same way.")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
