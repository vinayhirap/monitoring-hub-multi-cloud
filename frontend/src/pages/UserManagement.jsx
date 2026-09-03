import { useState, useEffect, useCallback } from "react";
import { useAuth } from "../auth/AuthContext";
import "./UserManagement.css";
import {
  PlusIcon, XIcon, AlertTriangleIcon, UsersIcon, LockIcon,
  ToolIcon, EditIcon, EyeIcon, CheckIcon,
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
                <span className="field-hint">
                  {groups.length === 0
                    ? "No organization groups set up yet."
                    : "Sets the role below automatically (L1 = Viewer, L2 = Editor, L3 = Admin) and inherits this group's access plus every parent group's access."}
                </span>
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

      {/* Tabs */}
      <div className="tabs">
        <button className={`tab ${tab === "users" ? "active" : ""}`} onClick={() => setTab("users")}><UsersIcon size={14} /> Users</button>
        <button className={`tab ${tab === "roles" ? "active" : ""}`} onClick={() => setTab("roles")}><LockIcon size={14} /> Roles & Permissions</button>
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

      {/* Roles Tab */}
      {tab === "roles" && (
        <div className="roles-grid">
          <RoleCard title="Admin Role"  icon={ToolIcon} color="orange" desc="Unrestricted access to all platform features including account onboarding, user management, alert configuration, and audit logs." granted={ADMIN_PERMS} denied={[]} />
          <RoleCard title="Editor Role" icon={EditIcon} color="purple" desc="Monitor infrastructure, configure alerts, and onboard accounts. Cannot manage users or access audit logs."                        granted={EDITOR_PERMS} denied={EDITOR_DENIED} />
          <RoleCard title="Viewer Role" icon={EyeIcon}  color="blue"   desc="Monitor account health, view metrics, drill into services, and read alerts. Cannot modify configuration or onboard accounts."  granted={VIEWER_PERMS} denied={VIEWER_DENIED} />
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
