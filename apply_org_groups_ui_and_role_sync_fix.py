#!/usr/bin/env python3
"""
apply_org_groups_ui_and_role_sync_fix.py — two fixes:

1. CRASH FIX (backend): app/api/admin/groups.py's add_group_members()
   calls authz.GROUP_LEVEL_ROLE.get(g["level"]) to sync a new member's
   role to their group's level (L1->viewer, L2->editor, L3->admin).
   That dict is referenced in THREE places (groups.py, permissions.py's
   docstring, and db/migrations/015_permissions_rbac.sql's comments)
   but was never actually defined anywhere in app/auth/authorization.py.
   Every call to POST /api/groups/{id}/members has always thrown
   AttributeError: module 'app.auth.authorization' has no attribute
   'GROUP_LEVEL_ROLE' -- which is exactly what fires the instant an
   admin creates a user with a Group selected in the Add User modal
   (UserManagement.jsx already calls this endpoint right after user
   creation), or the instant anyone tries to add a member to a group
   at all. This has been silently swallowed by a .catch(() => {}) on
   the frontend call, so the user got created but the group membership
   (and therefore the whole point of picking a group) silently never
   happened. Fixed by defining the dict where the code already expects
   it, using the mapping confirmed correct against the live product
   requirement: L1 = Viewer (least access), L2 = Editor (mid), L3 =
   Admin (full access) -- a straight chain, matching GROUP_PARENT_LEVEL
   (L1 root -> L2 -> L3 leaf) one-for-one.

2. MISSING UI (frontend): the Users page has never had a way to
   actually create/view/manage L1/L2/L3 groups themselves -- only the
   Add User modal's "Group (optional)" dropdown existed, which needs
   groups to already exist to be useful. Adds a full "Groups" tab:
     - Tree list of every group, color-coded by level, showing the
       role each level implies.
     - Add Group modal (admin-only): name, level, parent (auto-
       filtered to the one valid parent level for L2/L3; disabled for
       L1), description.
     - Click a group to expand it: shows its current members (added
       via a dropdown of existing users, removable with one click) and
       its own directly-attached access policies (read-only display in
       this pass -- attaching NEW account/region policies to a group
       still has to be done via the API directly for now; the display
       here already reads real data via GET /api/groups/{id}, nothing
       mocked). Delete-group button relies on the backend's existing
       409 "has children" guard, surfaced as a plain error alert.
   Also fixes a related bug while in this file: loadAccountsAndGroups
   was gated entirely behind `if (!isAdmin) return`, so an editor
   (who list_groups explicitly permits: require_role("admin","editor"))
   never even fetched /api/groups and would see an empty Groups tab
   with no way to know why. Split into loadAccounts (admin-only, feeds
   the Add User modal's AWS-account picker) and loadGroups
   (admin-or-editor, feeds both the Add User modal's Group dropdown
   AND the new Groups tab).

3. MISSING CSS (frontend): four classes were already referenced in
   UserManagement.jsx (.field-hint, .users-loading, .users-empty,
   .role-readonly) but never defined in UserManagement.css, so they
   rendered as unstyled plain text. Added, plus the new classes the
   Groups tab needs (.group-tree, .group-node, .member-chip, etc.),
   all built from the same CSS variables (--bg-card, --border,
   --accent-orange, ...) already used throughout the file so nothing
   looks bolted-on.

Usage:
    python apply_org_groups_ui_and_role_sync_fix.py --dry-run
    python apply_org_groups_ui_and_role_sync_fix.py
"""
import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-org-groups-ui-and-role-sync-fix"


class PatchError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────
# 1. Backend crash fix
# ─────────────────────────────────────────────────────────────────────
AUTHZ_OLD = r'''GROUP_LEVELS = ("L1", "L2", "L3")
# What level a group's parent MUST be, keyed by the child's own level.
# L1 -> None (top of the tree, no parent allowed).
GROUP_PARENT_LEVEL = {"L1": None, "L2": "L1", "L3": "L2"}
'''
AUTHZ_NEW = r'''GROUP_LEVELS = ("L1", "L2", "L3")
# What level a group's parent MUST be, keyed by the child's own level.
# L1 -> None (top of the tree, no parent allowed).
GROUP_PARENT_LEVEL = {"L1": None, "L2": "L1", "L3": "L2"}

# The role a user is given automatically when added as a member of a
# group at each level. L1 = Viewer (least access), L2 = Editor (mid),
# L3 = Admin (full access) -- referenced by app/api/admin/groups.py's
# add_group_members() and mirrored client-side in UserManagement.jsx
# purely for instant UI feedback; this dict here is the one and only
# authoritative source. (Previously referenced from three places in
# this codebase but never actually defined -- every group-membership
# write has been crashing with AttributeError until this fix.)
GROUP_LEVEL_ROLE = {"L1": "viewer", "L2": "editor", "L3": "admin"}
'''

# ─────────────────────────────────────────────────────────────────────
# 2. Frontend: imports, state, data loading
# ─────────────────────────────────────────────────────────────────────
JSX_IMPORT_OLD = r'''import {
  PlusIcon, XIcon, AlertTriangleIcon, UsersIcon, LockIcon,
  ToolIcon, EditIcon, EyeIcon, CheckIcon,
} from "../components/icons";'''
JSX_IMPORT_NEW = r'''import {
  PlusIcon, XIcon, AlertTriangleIcon, UsersIcon, LockIcon,
  ToolIcon, EditIcon, EyeIcon, CheckIcon, LayersIcon,
} from "../components/icons";'''

JSX_STATE_OLD = r'''  const [saving,     setSaving]     = useState(null);
  const [submitting, setSubmitting] = useState(false);
'''
JSX_STATE_NEW = r'''  const [saving,     setSaving]     = useState(null);
  const [submitting, setSubmitting] = useState(false);

  // Groups tab (L1/L2/L3 organization groups) -- separate state from
  // the Add User modal's own use of `groups`/`users`, but reuses both
  // lists rather than re-fetching them a second time.
  const [showAddGroup,     setShowAddGroup]     = useState(false);
  const [groupForm,        setGroupForm]        = useState({ name: "", level: "L1", parentGroupId: "", description: "" });
  const [groupFormErrs,    setGroupFormErrs]    = useState({});
  const [groupSubmitting,  setGroupSubmitting]  = useState(false);
  const [expandedGroupId,  setExpandedGroupId]  = useState(null);
  const [groupDetails,     setGroupDetails]     = useState({}); // group id -> detail payload from GET /api/groups/{id}
  const [groupDetailLoad,  setGroupDetailLoad]  = useState(null);
'''

JSX_LOAD_OLD = r'''  // Accounts (Account Access picker) and org groups (Group picker) both
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
'''
JSX_LOAD_NEW = r'''  // Accounts feed only the Add User modal's AWS-account picker, so
  // stay admin-only (only admins can open that modal). Groups feed
  // BOTH the Add User modal's Group dropdown AND the Groups tab below,
  // and GET /api/groups is explicitly allowed for admin OR editor
  // (see require_role("admin","editor") on list_groups) -- gating this
  // fetch to admin-only, as it used to be, meant an editor opening the
  // Groups tab saw a permanently empty list with no way to know why.
  // Fetched on mount AND again every time the Add User modal opens --
  // a single mount-time fetch could race a still-settling backend and
  // come back empty, which previously made the Account Access field
  // silently never appear with no way to retry short of a full page
  // reload; re-fetching on open fixes that.
  const loadAccounts = useCallback(() => {
    if (!isAdmin) return;
    fetch(`${BASE}/api/live/accounts`)
      .then(r => r.json())
      .then(data => setAccounts(Array.isArray(data) ? data : []))
      .catch(() => {});
  }, [isAdmin]);

  const loadGroups = useCallback(() => {
    if (!isAdmin && !isEditor) return;
    apiFetch("/api/groups")
      .then(data => setGroups(Array.isArray(data) ? data : []))
      .catch(() => {});
  }, [isAdmin, isEditor]);

  useEffect(() => { loadAccounts(); loadGroups(); }, [loadAccounts, loadGroups]);
  useEffect(() => { if (showAdd) { loadAccounts(); loadGroups(); } }, [showAdd, loadAccounts, loadGroups]);
'''

# ─────────────────────────────────────────────────────────────────────
# 3. Frontend: group handlers (create/delete/expand/members)
# ─────────────────────────────────────────────────────────────────────
JSX_HANDLERS_OLD = r'''  function formatDate(iso) {'''
JSX_HANDLERS_NEW = r'''  // Which parent level a group at this level must have -- mirrors
  // app.auth.authorization.GROUP_PARENT_LEVEL exactly (L1 -> none,
  // L2 -> L1, L3 -> L2), purely to filter the parent dropdown and
  // give an inline error before the backend has to reject it.
  const GROUP_PARENT_LEVEL = { L1: null, L2: "L1", L3: "L2" };

  function validGroupParents(level) {
    const parentLevel = GROUP_PARENT_LEVEL[level];
    if (!parentLevel) return [];
    return groups.filter(g => g.level === parentLevel);
  }

  function validateGroupForm() {
    const e = {};
    if (!groupForm.name.trim()) e.name = "Name required";
    const parentLevel = GROUP_PARENT_LEVEL[groupForm.level];
    if (parentLevel && !groupForm.parentGroupId) {
      e.parentGroupId = `Select a parent ${parentLevel} group`;
    }
    return e;
  }

  async function handleAddGroup() {
    if (!isAdmin) return; // hard guard
    const e = validateGroupForm();
    if (Object.keys(e).length) { setGroupFormErrs(e); return; }
    setGroupSubmitting(true);
    try {
      await apiFetch("/api/groups", {
        method: "POST",
        body: JSON.stringify({
          name: groupForm.name.trim(),
          level: groupForm.level,
          parent_group_id: groupForm.parentGroupId ? Number(groupForm.parentGroupId) : null,
          description: groupForm.description.trim() || undefined,
        }),
      });
      setGroupForm({ name: "", level: "L1", parentGroupId: "", description: "" });
      setGroupFormErrs({});
      setShowAddGroup(false);
      await loadGroups();
    } catch (err) {
      setGroupFormErrs({ submit: err.message });
    } finally {
      setGroupSubmitting(false);
    }
  }

  async function handleDeleteGroup(id, name) {
    if (!isAdmin) return; // hard guard
    // Backend refuses (409) if this group still has children -- surfaced
    // as-is rather than trying to pre-compute it client-side, since the
    // backend is the single source of truth for the tree's real shape.
    if (!window.confirm(`Delete group "${name}"? This cannot be undone.`)) return;
    try {
      await apiFetch(`/api/groups/${id}`, { method: "DELETE" });
      setGroupDetails(d => { const next = { ...d }; delete next[id]; return next; });
      if (expandedGroupId === id) setExpandedGroupId(null);
      await loadGroups();
    } catch (err) {
      alert("Delete failed: " + err.message);
    }
  }

  async function toggleGroupExpand(id) {
    if (expandedGroupId === id) { setExpandedGroupId(null); return; }
    setExpandedGroupId(id);
    if (groupDetails[id]) return; // already cached from a previous expand
    setGroupDetailLoad(id);
    try {
      const detail = await apiFetch(`/api/groups/${id}`);
      setGroupDetails(d => ({ ...d, [id]: detail }));
    } catch (err) {
      setGroupDetails(d => ({ ...d, [id]: { error: err.message } }));
    } finally {
      setGroupDetailLoad(null);
    }
  }

  async function refreshGroupDetail(id) {
    try {
      const detail = await apiFetch(`/api/groups/${id}`);
      setGroupDetails(d => ({ ...d, [id]: detail }));
    } catch (err) {
      setGroupDetails(d => ({ ...d, [id]: { error: err.message } }));
    }
  }

  async function handleAddGroupMember(groupId, userId) {
    if (!isAdmin) return; // hard guard
    try {
      await apiFetch(`/api/groups/${groupId}/members`, {
        method: "POST",
        body: JSON.stringify({ user_ids: [Number(userId)] }),
      });
      await refreshGroupDetail(groupId);
      await loadUsers(); // the member's role may have just been synced to the group's level
    } catch (err) {
      alert("Add member failed: " + err.message);
    }
  }

  async function handleRemoveGroupMember(groupId, userId) {
    if (!isAdmin) return; // hard guard
    try {
      await apiFetch(`/api/groups/${groupId}/members/${userId}`, { method: "DELETE" });
      await refreshGroupDetail(groupId);
    } catch (err) {
      alert("Remove member failed: " + err.message);
    }
  }

  function formatDate(iso) {'''

# ─────────────────────────────────────────────────────────────────────
# 4. Frontend: tab button
# ─────────────────────────────────────────────────────────────────────
JSX_TABBTN_OLD = r'''        <button className={`tab ${tab === "roles" ? "active" : ""}`} onClick={() => setTab("roles")}><LockIcon size={14} /> Roles & Permissions</button>
      </div>'''
JSX_TABBTN_NEW = r'''        <button className={`tab ${tab === "roles" ? "active" : ""}`} onClick={() => setTab("roles")}><LockIcon size={14} /> Roles & Permissions</button>
        {(isAdmin || isEditor) && (
          <button className={`tab ${tab === "groups" ? "active" : ""}`} onClick={() => setTab("groups")}><LayersIcon size={14} /> Groups</button>
        )}
      </div>'''

# ─────────────────────────────────────────────────────────────────────
# 5. Frontend: Add Group modal (inserted right after the Add User modal,
#    before the Tabs bar)
# ─────────────────────────────────────────────────────────────────────
JSX_ADDGROUPMODAL_OLD = r'''      {/* Tabs */}
      <div className="tabs">'''
JSX_ADDGROUPMODAL_NEW = r'''      {/* Add Group Modal — admin only */}
      {showAddGroup && isAdmin && (
        <div className="modal-backdrop" onClick={e => e.target === e.currentTarget && setShowAddGroup(false)}>
          <div className="modal-card">
            <div className="modal-header">
              <span>Add Organization Group</span>
              <button className="modal-close" onClick={() => setShowAddGroup(false)}><XIcon size={16} /></button>
            </div>
            <div className="modal-body">
              <div className={`mfield ${groupFormErrs.name ? "merr" : ""}`}>
                <label>Name *</label>
                <input
                  value={groupForm.name}
                  onChange={e => setGroupForm(f => ({ ...f, name: e.target.value }))}
                  placeholder="e.g. APAC, India-NOC, L3-OnCall"
                  autoComplete="off"
                />
                {groupFormErrs.name && <span className="err-msg">{groupFormErrs.name}</span>}
              </div>
              <div className="mfield">
                <label>Level</label>
                <select
                  value={groupForm.level}
                  onChange={e => setGroupForm(f => ({ ...f, level: e.target.value, parentGroupId: "" }))}
                >
                  <option value="L1">L1 — top level, Viewer access</option>
                  <option value="L2">L2 — mid level, Editor access</option>
                  <option value="L3">L3 — leaf level, Admin access</option>
                </select>
                <span className="field-hint">Sets the role any member of this group is given automatically.</span>
              </div>
              {groupForm.level !== "L1" && (
                <div className={`mfield ${groupFormErrs.parentGroupId ? "merr" : ""}`}>
                  <label>Parent Group *</label>
                  <select
                    value={groupForm.parentGroupId}
                    onChange={e => setGroupForm(f => ({ ...f, parentGroupId: e.target.value }))}
                  >
                    <option value="">Select a parent…</option>
                    {validGroupParents(groupForm.level).map(g => (
                      <option key={g.id} value={g.id}>{g.name}</option>
                    ))}
                  </select>
                  {groupFormErrs.parentGroupId && <span className="err-msg">{groupFormErrs.parentGroupId}</span>}
                  {validGroupParents(groupForm.level).length === 0 && (
                    <span className="field-hint">
                      No {GROUP_PARENT_LEVEL[groupForm.level]} groups exist yet — create one first.
                    </span>
                  )}
                </div>
              )}
              <div className="mfield">
                <label>Description (optional)</label>
                <input
                  value={groupForm.description}
                  onChange={e => setGroupForm(f => ({ ...f, description: e.target.value }))}
                  placeholder="What this group is for"
                  autoComplete="off"
                />
              </div>
              {groupFormErrs.submit && <div className="mfield-error"><AlertTriangleIcon size={13} /> {groupFormErrs.submit}</div>}
            </div>
            <div className="modal-footer">
              <button className="btn-ghost" onClick={() => setShowAddGroup(false)}>Cancel</button>
              <button className="btn-primary" onClick={handleAddGroup} disabled={groupSubmitting}>
                {groupSubmitting ? "Creating…" : "Add Group"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="tabs">'''

# ─────────────────────────────────────────────────────────────────────
# 6. Frontend: Groups tab content (inserted at the very end of the
#    component, right before the closing brace / RoleCard helper)
# ─────────────────────────────────────────────────────────────────────
JSX_GROUPSTAB_OLD = r'''      )}
    </div>
  );
}

function RoleCard({ title, icon: Icon, color, desc, granted, denied }) {'''
JSX_GROUPSTAB_NEW = r'''      )}

      {/* Groups Tab -- L1/L2/L3 organization groups: create/delete
          (admin-only), expand to manage membership and see directly-
          attached access policies. Attaching NEW policies to a group
          still has to go through the API directly for now; this reads
          real data (GET /api/groups/{id}) but doesn't yet write new
          group_policies rows from the UI. */}
      {tab === "groups" && (isAdmin || isEditor) && (
        <div className="groups-panel">
          <div className="groups-panel-header">
            <p className="subtitle" style={{ margin: 0 }}>
              L1 (Viewer) → L2 (Editor) → L3 (Admin). Each level inherits its parent group's access plus whatever is granted to it directly.
            </p>
            {isAdmin && (
              <button
                className="btn-primary"
                onClick={() => { setShowAddGroup(true); setGroupFormErrs({}); setGroupForm({ name: "", level: "L1", parentGroupId: "", description: "" }); }}
              >
                <PlusIcon size={14} /> Add Group
              </button>
            )}
          </div>
          {groups.length === 0 ? (
            <div className="users-empty">No organization groups set up yet.</div>
          ) : (
            <div className="group-tree">
              {groups.map(g => {
                const detail = groupDetails[g.id];
                const isOpen = expandedGroupId === g.id;
                return (
                  <div key={g.id} className="group-node">
                    <div className="group-node-row" onClick={() => toggleGroupExpand(g.id)}>
                      <span className={`group-level-badge level-${g.level.toLowerCase()}`}>{g.level}</span>
                      <span className="group-name">{g.name}</span>
                      {g.description && <span className="group-desc">{g.description}</span>}
                      <span className="group-role-hint">{{ L1: "viewer", L2: "editor", L3: "admin" }[g.level]}</span>
                      {isAdmin && (
                        <button
                          className="btn-sm-danger"
                          onClick={e => { e.stopPropagation(); handleDeleteGroup(g.id, g.name); }}
                        >
                          Delete
                        </button>
                      )}
                    </div>
                    {isOpen && (
                      <div className="group-node-detail">
                        {groupDetailLoad === g.id && <div className="field-hint">Loading…</div>}
                        {detail?.error && <div className="mfield-error"><AlertTriangleIcon size={13} /> {detail.error}</div>}
                        {detail && !detail.error && (
                          <>
                            <div className="group-detail-section">
                              <div className="perms-label">MEMBERS</div>
                              <div className="member-chips">
                                {detail.members.length === 0 && <span className="field-hint">No members yet.</span>}
                                {detail.members.map(m => (
                                  <span key={m.id} className="member-chip">
                                    {m.username}
                                    {isAdmin && (
                                      <button className="chip-x" onClick={() => handleRemoveGroupMember(g.id, m.id)}>
                                        <XIcon size={10} />
                                      </button>
                                    )}
                                  </span>
                                ))}
                              </div>
                              {isAdmin && (
                                <select
                                  className="role-select"
                                  value=""
                                  onChange={e => { if (e.target.value) handleAddGroupMember(g.id, e.target.value); }}
                                >
                                  <option value="">+ Add member…</option>
                                  {users
                                    .filter(u => !detail.members.some(m => m.id === u.id))
                                    .map(u => <option key={u.id} value={u.id}>{u.username}</option>)}
                                </select>
                              )}
                            </div>
                            <div className="group-detail-section">
                              <div className="perms-label">ACCESS POLICIES (own — not counting inherited)</div>
                              {detail.own_policies.length === 0 ? (
                                <span className="field-hint">
                                  No policies attached directly to this group yet — it only inherits from its parent.
                                </span>
                              ) : (
                                <ul className="policy-list">
                                  {detail.own_policies.map(p => (
                                    <li key={p.id}>
                                      {p.cloud.toUpperCase()}{p.account_ref_id ? ` · account #${p.account_ref_id}` : " · all accounts"}
                                      {p.regions?.length ? ` · ${p.regions.join(", ")}` : ""}
                                    </li>
                                  ))}
                                </ul>
                              )}
                            </div>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function RoleCard({ title, icon: Icon, color, desc, granted, denied }) {'''

# ─────────────────────────────────────────────────────────────────────
# 7. CSS: missing classes already referenced in the JSX, plus new
#    Groups-tab classes, all built from the same design-token vars
#    used throughout this file.
# ─────────────────────────────────────────────────────────────────────
CSS_OLD = r'''.err-msg { font-size: 11px; color: var(--red); }
'''
CSS_NEW = r'''.err-msg { font-size: 11px; color: var(--red); }

/* Previously referenced in UserManagement.jsx but never defined */
.field-hint    { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
.users-loading { padding: 24px 0; color: var(--text-secondary); font-size: 13px; text-align: center; }
.users-empty   { padding: 24px 0; color: var(--text-muted); font-size: 13px; text-align: center; }
.role-readonly {
  font-size: 11px; color: var(--text-muted); font-family: var(--font-mono);
  padding: 5px 10px;
}

/* Groups Tab */
.groups-panel-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 18px; gap: 16px; }
.group-tree { display: flex; flex-direction: column; gap: 8px; }
.group-node {
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: var(--radius-lg); overflow: hidden;
}
.group-node-row {
  display: flex; align-items: center; gap: 12px; padding: 14px 18px; cursor: pointer;
  transition: background .15s;
}
.group-node-row:hover { background: var(--bg-card-hover); }
.group-level-badge {
  font-size: 10px; font-weight: 700; padding: 3px 8px; border-radius: 4px;
  font-family: var(--font-mono); flex-shrink: 0;
}
.group-level-badge.level-l1 { background: rgba(59,130,246,.12);  color: var(--accent-blue);   border: 1px solid rgba(59,130,246,.25); }
.group-level-badge.level-l2 { background: rgba(168,85,247,.15);  color: #c084fc;               border: 1px solid rgba(168,85,247,.3); }
.group-level-badge.level-l3 { background: rgba(249,115,22,.15);  color: var(--accent-orange);  border: 1px solid rgba(249,115,22,.3); }
.group-name { font-weight: 600; font-size: 14px; }
.group-desc { font-size: 12px; color: var(--text-secondary); flex: 1; min-width: 0; }
.group-role-hint {
  font-size: 11px; color: var(--text-muted); font-family: var(--font-mono);
  text-transform: uppercase; letter-spacing: .05em; flex-shrink: 0;
}
.group-node-detail {
  border-top: 1px solid var(--border); padding: 16px 18px;
  display: flex; flex-direction: column; gap: 16px; background: var(--bg-input);
}
.group-detail-section { display: flex; flex-direction: column; gap: 8px; }
.member-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.member-chip {
  font-size: 12px; padding: 4px 6px 4px 10px; border-radius: 999px;
  background: var(--bg-card); border: 1px solid var(--border);
  display: inline-flex; align-items: center; gap: 6px;
}
.chip-x {
  background: none; border: none; color: var(--text-muted); cursor: pointer;
  display: flex; align-items: center; padding: 2px; border-radius: 50%;
}
.chip-x:hover { color: var(--red); background: rgba(239,68,68,.1); }
.policy-list { margin: 0; padding-left: 18px; font-size: 12px; color: var(--text-secondary); line-height: 1.8; }
'''

PATCHES = [
    ("app/auth/authorization.py", [(AUTHZ_OLD, AUTHZ_NEW)]),
    ("frontend/src/pages/UserManagement.jsx", [
        (JSX_IMPORT_OLD, JSX_IMPORT_NEW),
        (JSX_STATE_OLD, JSX_STATE_NEW),
        (JSX_LOAD_OLD, JSX_LOAD_NEW),
        (JSX_HANDLERS_OLD, JSX_HANDLERS_NEW),
        (JSX_TABBTN_OLD, JSX_TABBTN_NEW),
        (JSX_ADDGROUPMODAL_OLD, JSX_ADDGROUPMODAL_NEW),
        (JSX_GROUPSTAB_OLD, JSX_GROUPSTAB_NEW),
    ]),
    ("frontend/src/pages/UserManagement.css", [(CSS_OLD, CSS_NEW)]),
]


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
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1 — {old[:70]!r}")
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
            print("\nNext steps on the server:")
            print("  1) sudo systemctl restart monitoring-hub   # picks up authorization.py fix")
            print("     (FastAPI/Uvicorn does not hot-reload in production)")
            print("  2) cd frontend && npm install --silent && npm run build")
            print("  3) Hard-refresh the browser (frontend is a static build, browser may cache old JS)")
    except PatchError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
