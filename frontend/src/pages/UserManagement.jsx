import { useState, useEffect, useCallback } from "react";
import { useAuth } from "../auth/AuthContext";
import "./UserManagement.css";
import {
  PlusIcon, XIcon, AlertTriangleIcon, UsersIcon, LockIcon,
  ToolIcon, EditIcon, EyeIcon, CheckIcon, LayersIcon,
} from "../components/icons";

const BASE = "";

async function apiFetch(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `Request failed: ${res.status}`);
  }
  return res.json();
}

const ADMIN_PERMS   = ["Onboard Accounts","Manage Users","View All Accounts","Configure Alerts","Audit Logs","Service Drilldown"];
const EDITOR_PERMS  = ["View All Accounts","Configure Alerts","Onboard Accounts","Service Drilldown"];
const EDITOR_DENIED = ["Manage Users","Audit Logs"];
const VIEWER_PERMS  = ["View All Accounts","View Alerts","Service Drilldown"];
const VIEWER_DENIED = ["Onboard Accounts","Manage Users","Audit Logs","Configure Alerts"];

const INITIAL_FORM = { username: "", password: "", email: "", role: "viewer", accountIds: [], groupId: "" };

// Mirrors app/auth/authorization.py's GROUP_LEVEL_ROLE exactly -- the
// role a user is given automatically when assigned to a group at each
// level. Kept in sync here purely so the Role dropdown can show/lock
// to the right value the instant a group is picked, without waiting
// on a round trip; the backend applies the same mapping authoritatively
// when the membership is actually created, so this can never drift
// into being the source of truth.
const GROUP_LEVEL_ROLE = { L1: "viewer", L2: "editor", L3: "admin" };

export default function UserManagement() {
  const { user: currentUser } = useAuth();
  const currentRole = (currentUser?.role || "viewer").toLowerCase();
  const isAdmin  = currentRole === "admin";
  const isEditor = currentRole === "editor";

  const [tab,        setTab]        = useState("users");
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

  // Accounts feed only the Add User modal's AWS-account picker, so
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

  useEffect(() => { loadUsers(); }, [loadUsers]);

  // Roles & Permissions tab -- the real permission catalog + per-role
  // grant matrix from the backend (GET /api/permissions), not a
  // hardcoded frontend list. is_internal permissions (SMTP, system
  // config) are excluded server-side before this ever arrives -- see
  // app/api/permissions.py -- so there's nothing to filter here.
  const [permCategories, setPermCategories] = useState([]);
  useEffect(() => {
    if (tab === "roles" && isAdmin) {
      apiFetch("/api/permissions").then(setPermCategories).catch(() => setPermCategories([]));
    }
  }, [tab, isAdmin]);

  function validateForm() {
    const e = {};
    if (!form.username.trim()) e.username = "Username required";
    if (!form.password.trim()) e.password = "Password required";
    if (form.password.length > 0 && form.password.length < 6)
      e.password = "Min 6 characters";
    return e;
  }

  async function handleAdd() {
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
          email:    form.email.trim() || undefined,
        }),
      });
      // Per-account access grants -- POST /api/users/{id}/access with a
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

  async function handleRemove(id, username) {
    if (!isAdmin) return; // hard guard
    if (!window.confirm(`Remove "${username}"? Cannot be undone.`)) return;
    setSaving(id);
    try {
      await apiFetch(`/api/users/${id}`, { method: "DELETE" });
      await loadUsers();
    } catch (err) {
      alert("Remove failed: " + err.message);
    } finally {
      setSaving(null);
    }
  }

  async function handleRoleChange(id, newRole) {
    if (!isAdmin) return; // hard guard — editors/viewers cannot change roles
    // Prevent self-elevation (belt-and-suspenders, backend also blocks)
    const targetUser = users.find(u => u.id === id);
    if (targetUser?.username === currentUser?.username) return;
    setSaving(id);
    try {
      await apiFetch(`/api/users/${id}/role`, {
        method: "PATCH",
        body: JSON.stringify({ role: newRole }),
      });
      await loadUsers();
    } catch (err) {
      alert("Role update failed: " + err.message);
    } finally {
      setSaving(null);
    }
  }

  // Which parent level a group at this level must have -- mirrors
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

  function formatDate(iso) {
    if (!iso) return "Never";
    try { return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }); }
    catch { return iso; }
  }

  return (
    <div className="users-page">
      <div className="page-header">
        <div>
          <h1>User <span className="accent">Management</span></h1>
          <p className="subtitle">Manage access and permissions for CloudOps users</p>
        </div>
        {/* Only admins see Add User */}
        {isAdmin && (
          <button className="btn-primary" onClick={() => { setShowAdd(true); setFormErrs({}); setForm(INITIAL_FORM); }}>
            <PlusIcon size={14} /> Add User
          </button>
        )}
      </div>

      {/* Add User Modal — admin only */}
      {showAdd && isAdmin && (
        <div className="modal-backdrop" onClick={e => e.target === e.currentTarget && setShowAdd(false)}>
          <div className="modal-card">
            <div className="modal-header">
              <span>Add New User</span>
              <button className="modal-close" onClick={() => setShowAdd(false)}><XIcon size={16} /></button>
            </div>
            <div className="modal-body">
              <div className={`mfield ${formErrs.username ? "merr" : ""}`}>
                <label>Username *</label>
                <input
                  value={form.username}
                  onChange={e => setForm(f => ({ ...f, username: e.target.value }))}
                  placeholder="e.g. john.doe"
                  autoComplete="off"
                />
                {formErrs.username && <span className="err-msg">{formErrs.username}</span>}
              </div>
              <div className={`mfield ${formErrs.password ? "merr" : ""}`}>
                <label>Password *</label>
                <input
                  type="password"
                  value={form.password}
                  onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
                  placeholder="Min 6 characters"
                  autoComplete="new-password"
                />
                {formErrs.password && <span className="err-msg">{formErrs.password}</span>}
              </div>
              <div className="mfield">
                <label>Email (optional)</label>
                <input
                  type="email"
                  value={form.email}
                  onChange={e => setForm(f => ({ ...f, email: e.target.value }))}
                  placeholder="e.g. john.doe@aurionpro.com"
                  autoComplete="off"
                />
                <span className="field-hint">If SMTP is configured, a set-your-password link is emailed here.</span>
              </div>
              <div className="mfield">
                <label>Group (optional)</label>
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
              <div className="mfield">
                <label>Role{form.groupId ? " (set by group)" : ""}</label>
                <select
                  value={form.role}
                  disabled={!!form.groupId}
                  onChange={e => setForm(f => ({ ...f, role: e.target.value, accountIds: [] }))}
                >
                  <option value="viewer">Viewer — read-only</option>
                  <option value="editor">Editor — view + configure alerts</option>
                  <option value="admin">Admin — full access</option>
                </select>
                {form.groupId && (
                  <span className="field-hint">
                    Role is locked to this group's level. Choose "No group" above to set a role manually instead.
                  </span>
                )}
              </div>
              {(form.role === "viewer" || form.role === "editor") && accounts.length > 0 && (
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
              {formErrs.submit && <div className="mfield-error"><AlertTriangleIcon size={13} /> {formErrs.submit}</div>}
            </div>
            <div className="modal-footer">
              <button className="btn-ghost" onClick={() => setShowAdd(false)}>Cancel</button>
              <button className="btn-primary" onClick={handleAdd} disabled={submitting}>
                {submitting ? "Creating…" : "Add User"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Add Group Modal — admin only */}
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
                  placeholder="Group name"
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
      <div className="tabs">
        <button className={`tab ${tab === "users" ? "active" : ""}`} onClick={() => setTab("users")}><UsersIcon size={14} /> Users</button>
        <button className={`tab ${tab === "roles" ? "active" : ""}`} onClick={() => setTab("roles")}><LockIcon size={14} /> Roles & Permissions</button>
        {(isAdmin || isEditor) && (
          <button className={`tab ${tab === "groups" ? "active" : ""}`} onClick={() => setTab("groups")}><LayersIcon size={14} /> Groups</button>
        )}
      </div>

      {/* Users Tab */}
      {tab === "users" && (
        <>
          {loading && <div className="users-loading">Loading users…</div>}
          {error   && <div className="users-error"><AlertTriangleIcon size={14} /> {error} <button onClick={loadUsers}>Retry</button></div>}
          {!loading && !error && (
            <div className="users-list">
              {users.length === 0 ? (
                <div className="users-empty">No users found.</div>
              ) : users.map(u => {
                const isYou   = u.username === currentUser?.username;
                const role    = (u.role || "viewer").toUpperCase();
                // Admins can modify others. Editors/viewers: read-only list.
                const canEdit = isAdmin && !isYou;
                return (
                  <div key={u.id} className="user-row">
                    <div
                      className="user-avatar"
                      style={{ background:
                        role === "ADMIN"  ? "rgba(249,115,22,.2)"  :
                        role === "EDITOR" ? "rgba(168,85,247,.2)"  :
                                            "rgba(59,130,246,.2)"
                      }}
                    >
                      {role === "ADMIN" ? <ToolIcon size={18} /> : role === "EDITOR" ? <EditIcon size={18} /> : <EyeIcon size={18} />}
                    </div>
                    <div className="user-info">
                      <div className="user-name">
                        {u.username}
                        {isYou && <span className="you-tag">(you)</span>}
                      </div>
                      <div className="user-meta">
                        Role: <strong>{role}</strong> · Created: {formatDate(u.created_at)}
                      </div>
                    </div>
                    <div className="user-actions">
                      <span className={`role-badge role-${role.toLowerCase()}`}>{role}</span>
                      {canEdit ? (
                        <>
                          <select
                            className="role-select"
                            value={role.toLowerCase()}
                            disabled={saving === u.id}
                            onChange={e => handleRoleChange(u.id, e.target.value)}
                          >
                            <option value="viewer">Viewer</option>
                            <option value="editor">Editor</option>
                            <option value="admin">Admin</option>
                          </select>
                          <button
                            className="btn-sm-danger"
                            disabled={saving === u.id}
                            onClick={() => handleRemove(u.id, u.username)}
                          >
                            {saving === u.id ? "…" : "Remove"}
                          </button>
                        </>
                      ) : (
                        // Non-admin or self: show role label only, no controls
                        !isYou && (
                          <span className="role-readonly">{role}</span>
                        )
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}

      {/* Roles Tab -- real permission catalog from the backend
          (GET /api/permissions), grouped by category, showing exactly
          which of Viewer(L1)/Editor(L2)/Admin(L3) each permission is
          granted to. This is a READ-ONLY view of role_permissions in
          this pass -- changing what a role grants is a database
          change (db/migrations/015_permissions_rbac.sql), not yet
          editable from here. */}
      {tab === "roles" && (
        <div className="roles-grid" style={{ gridTemplateColumns: "1fr 1fr", alignItems: "start" }}>
          {permCategories.length === 0 ? (
            <div style={{ gridColumn: "1 / -1", padding: 24, color: "var(--text-muted)", fontSize: 13 }}>
              Loading permission catalog…
            </div>
          ) : (
            permCategories.map(cat => (
              <div key={cat.category} className="role-card">
                <div className="role-card-header">
                  <span className="role-card-title">{cat.category}</span>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  {cat.permissions.map(p => (
                    <div key={p.code} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }} title={p.description || ""}>
                      <span style={{ fontSize: 12, fontWeight: 600 }}>{p.label}</span>
                      <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                        <span className={`perm-chip ${p.roles.viewer ? "granted" : "denied"}`}>L1</span>
                        <span className={`perm-chip ${p.roles.editor ? "granted" : "denied"}`}>L2</span>
                        <span className={`perm-chip ${p.roles.admin ? "granted" : "denied"}`}>L3</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))
          )}
        </div>
      )}

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

function RoleCard({ title, icon: Icon, color, desc, granted, denied }) {
  const subMap = { orange: "Full platform access", purple: "View + configure alerts", blue: "Read-only monitoring access" };
  return (
    <div className={`role-card role-card-${color}`}>
      <div className="role-card-header">
        <span className="role-card-icon"><Icon size={20} /></span>
        <div>
          <div className="role-card-title">{title}</div>
          <div className="role-card-sub">{subMap[color]}</div>
        </div>
      </div>
      <p className="role-card-desc">{desc}</p>
      <div className="perms-label">PERMISSIONS</div>
      <div className="perms-list">
        {granted.map(p => <span key={p} className="perm-chip granted"><CheckIcon size={11} /> {p}</span>)}
        {denied.map(p  => <span key={p} className="perm-chip denied"><XIcon size={11} /> {p}</span>)}
      </div>
    </div>
  );
}
