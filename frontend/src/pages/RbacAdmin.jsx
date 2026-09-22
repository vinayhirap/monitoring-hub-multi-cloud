// src/pages/RbacAdmin.jsx
//
// Frontend for app/api/admin/roles.py, rbac_scopes.py, bindings.py --
// the RBAC v2 administration API built in the same audit pass as this
// page. Admin-only in the permission catalog (041: roles.*, rbac.*,
// permissions.manage are never granted to editor), so this is its own
// page gated by roles: ["admin"] in Layout.jsx's NAV_ITEMS, not a tab
// bolted onto UserManagement.jsx -- that file is already 790 lines
// covering a materially different concern (which accounts/groups a
// user belongs to, v1's access_scopes), where this page covers a
// different, newer system (custom roles + scoped bindings, v2's
// role_bindings) that most users will never open.
//
// Reuses UserManagement.css's shared classes (.page-header, .tabs,
// .modal-*, .mfield*, .perm-chip, .btn-*) rather than redefining them --
// this file's own CSS only adds what's genuinely new to this page.
import { useState, useEffect, useCallback } from "react";
import {
  getRoles, createRole, setRolePermissions, deleteRole,
  getRbacScopes, createRbacScope, deleteRbacScope,
  getBindings, createBinding, deleteBinding,
  getOverrides, createOverride, deleteOverride,
  getReviews, createReview,
  getUsersLite, getPermissionCatalog, getAccounts,
} from "../api/api";
import {
  PlusIcon, XIcon, TrashIcon, EditIcon, ShieldIcon, LockIcon,
  LayersIcon, KeyIcon, ClipboardIcon, CheckIcon, AlertTriangleIcon, SaveIcon,
} from "../components/icons";
import "./UserManagement.css";
import "./RbacAdmin.css";

const TABS = [
  { id: "roles",     label: "Roles",     icon: ShieldIcon },
  { id: "scopes",     label: "Scopes",    icon: LayersIcon },
  { id: "bindings",   label: "Bindings",  icon: KeyIcon },
  { id: "overrides",  label: "Overrides", icon: LockIcon },
  { id: "reviews",    label: "Access Reviews", icon: ClipboardIcon },
];

function ErrorBanner({ message, onDismiss }) {
  if (!message) return null;
  return (
    <div className="users-error">
      <AlertTriangleIcon size={14} /> {message}
      {onDismiss && <button className="btn-ghost" style={{ padding: "2px 8px" }} onClick={onDismiss}>Dismiss</button>}
    </div>
  );
}

function Modal({ title, onClose, children, footer }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          {title}
          <button className="modal-close" onClick={onClose}><XIcon size={18} /></button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-footer">{footer}</div>}
      </div>
    </div>
  );
}

function Field({ label, error, children }) {
  return (
    <div className={`mfield ${error ? "merr" : ""}`}>
      <label>{label}</label>
      {children}
      {error && <span className="err-msg">{error}</span>}
    </div>
  );
}

// A JSON array field (regions/services/resource_groups/resource_ids)
// authored as a comma-separated string in the UI, converted to a real
// array (or undefined, so the field stays "unrestricted" server-side
// rather than an empty array meaning something different) on submit.
function csvToList(s) {
  const parts = (s || "").split(",").map((x) => x.trim()).filter(Boolean);
  return parts.length ? parts : undefined;
}
function listToCsv(a) {
  return Array.isArray(a) ? a.join(", ") : "";
}

export default function RbacAdmin() {
  const [tab, setTab] = useState("roles");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const [roles, setRoles] = useState([]);
  const [scopes, setScopes] = useState([]);
  const [bindings, setBindings] = useState([]);
  const [overrides, setOverrides] = useState([]);
  const [reviews, setReviews] = useState([]);
  const [users, setUsers] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [permCatalog, setPermCatalog] = useState([]);
  const [reviewPrincipalId, setReviewPrincipalId] = useState("");

  // Shared reference data (users/accounts/permission catalog) is
  // fetched once on mount regardless of which tab is active -- every
  // tab's forms need at least one of these three, and they're small,
  // admin-only lists, so there's no cost to just always having them.
  // GET /api/permissions returns [{category, permissions: [{id, code,
  // label, description, roles}]}] -- shaped for the existing
  // permission-matrix cards in UserManagement.jsx, not a flat catalog.
  // Flatten it here rather than changing that endpoint's contract and
  // risking that other consumer.
  const loadShared = useCallback(async () => {
    try {
      const [u, a, p] = await Promise.all([getUsersLite(), getAccounts(), getPermissionCatalog()]);
      setUsers(Array.isArray(u) ? u : []);
      setAccounts(Array.isArray(a) ? a : []);
      const flat = Array.isArray(p)
        ? p.flatMap((cat) => (cat.permissions || []).map((perm) => ({ ...perm, category: cat.category })))
        : [];
      setPermCatalog(flat);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  const loadTab = useCallback(async (which) => {
    setLoading(true);
    setError(null);
    try {
      if (which === "roles") setRoles(await getRoles());
      else if (which === "scopes") setScopes(await getRbacScopes());
      else if (which === "bindings") setBindings(await getBindings());
      else if (which === "overrides") setOverrides(await getOverrides());
      else if (which === "reviews") setReviews(await getReviews(reviewPrincipalId || undefined));
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [reviewPrincipalId]);

  useEffect(() => { loadShared(); }, [loadShared]);
  useEffect(() => { loadTab(tab); }, [tab, loadTab]);

  const userLabel = (id) => {
    const u = users.find((x) => x.id === id);
    return u ? u.username : `#${id}`;
  };
  const accountLabel = (id) => {
    const a = accounts.find((x) => x.id === id);
    return a ? a.account_name : `#${id}`;
  };

  return (
    <div className="users-page">
      <div className="page-header">
        <div>
          <h1>RBAC <span className="accent">Administration</span></h1>
          <div className="subtitle">Custom roles, scoped bindings, deny overrides, and access reviews (v2 RBAC engine)</div>
        </div>
      </div>

      <div className="tabs">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button key={id} className={`tab ${tab === id ? "active" : ""}`} onClick={() => setTab(id)}>
            <Icon size={14} /> {label}
          </button>
        ))}
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />
      {loading && <div className="users-loading">Loading…</div>}

      {!loading && tab === "roles" && (
        <RolesTab roles={roles} permCatalog={permCatalog} reload={() => loadTab("roles")} setError={setError} />
      )}
      {!loading && tab === "scopes" && (
        <ScopesTab scopes={scopes} accounts={accounts} reload={() => loadTab("scopes")} setError={setError} accountLabel={accountLabel} />
      )}
      {!loading && tab === "bindings" && (
        <BindingsTab bindings={bindings} roles={roles} scopes={scopes} users={users}
                     reload={() => loadTab("bindings")} setError={setError} userLabel={userLabel} />
      )}
      {!loading && tab === "overrides" && (
        <OverridesTab overrides={overrides} users={users} scopes={scopes} permCatalog={permCatalog}
                      reload={() => loadTab("overrides")} setError={setError} userLabel={userLabel} />
      )}
      {!loading && tab === "reviews" && (
        <ReviewsTab reviews={reviews} users={users} bindings={bindings}
                    principalId={reviewPrincipalId} setPrincipalId={setReviewPrincipalId}
                    reload={() => loadTab("reviews")} setError={setError} />
      )}
    </div>
  );
}

// ── Roles ────────────────────────────────────────────────────────────
function RolesTab({ roles, permCatalog, reload, setError }) {
  const [showAdd, setShowAdd] = useState(false);
  const [form, setForm] = useState({ role_key: "", name: "", description: "", permissions: [] });
  const [saving, setSaving] = useState(false);
  const [editingId, setEditingId] = useState(null); // role whose permission chips are being edited
  const [editPerms, setEditPerms] = useState([]);
  const [savingPerms, setSavingPerms] = useState(false);

  const togglePerm = (list, code) =>
    list.includes(code) ? list.filter((c) => c !== code) : [...list, code];

  const submitAdd = async () => {
    if (!form.role_key.trim() || !form.name.trim()) { setError("Role key and name are required"); return; }
    setSaving(true);
    try {
      await createRole(form);
      setShowAdd(false);
      setForm({ role_key: "", name: "", description: "", permissions: [] });
      reload();
    } catch (e) { setError(e.message); } finally { setSaving(false); }
  };

  const startEditPerms = (role) => { setEditingId(role.id); setEditPerms(role.permissions || []); };
  const savePerms = async (roleId) => {
    setSavingPerms(true);
    try {
      await setRolePermissions(roleId, editPerms);
      setEditingId(null);
      reload();
    } catch (e) { setError(e.message); } finally { setSavingPerms(false); }
  };

  const remove = async (role) => {
    if (!confirm(`Delete role "${role.name}"? This cannot be undone.`)) return;
    try { await deleteRole(role.id); reload(); } catch (e) { setError(e.message); }
  };

  return (
    <div>
      <div className="groups-panel-header">
        <div className="subtitle">Builtin roles (admin/editor/viewer) can have their permissions edited here but not their name or key.</div>
        <button className="btn-primary" onClick={() => setShowAdd(true)}><PlusIcon size={14} /> New Role</button>
      </div>

      <div className="users-list">
        {roles.length === 0 && <div className="users-empty">No roles yet.</div>}
        {roles.map((r) => (
          <div className="user-row rbac-row" key={r.id} style={{ flexDirection: "column", alignItems: "stretch", gap: 10 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <span className={`role-badge role-${r.is_builtin ? "admin" : "viewer"}`}>{r.role_key}</span>
              <div className="user-info">
                <div className="user-name">{r.name} {r.is_builtin ? <span className="you-tag">builtin</span> : null}</div>
                {r.description && <div className="user-meta">{r.description}</div>}
              </div>
              <div className="user-actions">
                {editingId !== r.id && (
                  <button className="btn-ghost" onClick={() => startEditPerms(r)}><EditIcon size={13} /> Edit permissions</button>
                )}
                {!r.is_builtin && (
                  <button className="btn-ghost" onClick={() => remove(r)} title="Delete role"><TrashIcon size={13} /></button>
                )}
              </div>
            </div>

            {editingId !== r.id ? (
              <div className="perm-chip-row">
                {(r.permissions || []).length === 0
                  ? <span className="field-hint">No permissions granted</span>
                  : r.permissions.map((c) => <span className="perm-chip granted" key={c}>{c}</span>)}
              </div>
            ) : (
              <div className="rbac-perm-editor">
                {Object.entries(groupByCategory(permCatalog)).map(([cat, perms]) => (
                  <div key={cat} className="rbac-perm-category">
                    <div className="rbac-perm-cat-label">{cat}</div>
                    <div className="perm-chip-row">
                      {perms.map((p) => (
                        <button
                          key={p.code}
                          className={`perm-chip toggle ${editPerms.includes(p.code) ? "granted" : "denied"}`}
                          onClick={() => setEditPerms(togglePerm(editPerms, p.code))}
                          title={p.description}
                        >
                          {editPerms.includes(p.code) ? <CheckIcon size={11} /> : null} {p.code}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
                <div className="modal-footer" style={{ padding: "12px 0 0" }}>
                  <button className="btn-ghost" onClick={() => setEditingId(null)}>Cancel</button>
                  <button className="btn-primary" disabled={savingPerms} onClick={() => savePerms(r.id)}>
                    <SaveIcon size={13} /> {savingPerms ? "Saving…" : "Save permissions"}
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {showAdd && (
        <Modal title="New custom role" onClose={() => setShowAdd(false)} footer={
          <>
            <button className="btn-ghost" onClick={() => setShowAdd(false)}>Cancel</button>
            <button className="btn-primary" disabled={saving} onClick={submitAdd}>{saving ? "Creating…" : "Create role"}</button>
          </>
        }>
          <Field label="Role key (unique, lowercase, e.g. noc_l1)">
            <input value={form.role_key} onChange={(e) => setForm({ ...form, role_key: e.target.value.toLowerCase().replace(/\s+/g, "_") })} />
          </Field>
          <Field label="Display name">
            <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field label="Description">
            <input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </Field>
          <Field label="Initial permissions">
            <div className="rbac-perm-editor" style={{ maxHeight: 220, overflowY: "auto" }}>
              {Object.entries(groupByCategory(permCatalog)).map(([cat, perms]) => (
                <div key={cat} className="rbac-perm-category">
                  <div className="rbac-perm-cat-label">{cat}</div>
                  <div className="perm-chip-row">
                    {perms.map((p) => (
                      <button
                        key={p.code}
                        className={`perm-chip toggle ${form.permissions.includes(p.code) ? "granted" : "denied"}`}
                        onClick={() => setForm({ ...form, permissions: togglePerm(form.permissions, p.code) })}
                        title={p.description}
                      >
                        {form.permissions.includes(p.code) ? <CheckIcon size={11} /> : null} {p.code}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </Field>
        </Modal>
      )}
    </div>
  );
}

function groupByCategory(perms) {
  const out = {};
  for (const p of perms) {
    (out[p.category] = out[p.category] || []).push(p);
  }
  return out;
}

// ── Scopes ───────────────────────────────────────────────────────────
function ScopesTab({ scopes, accounts, reload, setError, accountLabel }) {
  const [showAdd, setShowAdd] = useState(false);
  const [saving, setSaving] = useState(false);
  const EMPTY = { label: "", cloud: "", account_ref_id: "", regions: "", services: "" };
  const [form, setForm] = useState(EMPTY);

  const submit = async () => {
    setSaving(true);
    try {
      await createRbacScope({
        label: form.label || undefined,
        cloud: form.cloud || undefined,
        account_ref_id: form.account_ref_id ? Number(form.account_ref_id) : undefined,
        regions: csvToList(form.regions),
        services: csvToList(form.services),
      });
      setShowAdd(false);
      setForm(EMPTY);
      reload();
    } catch (e) { setError(e.message); } finally { setSaving(false); }
  };

  const remove = async (s) => {
    if (!confirm(`Delete scope "${s.label || s.id}"?`)) return;
    try { await deleteRbacScope(s.id); reload(); } catch (e) { setError(e.message); }
  };

  return (
    <div>
      <div className="groups-panel-header">
        <div className="subtitle">A scope is a reusable (cloud, account, region, service) boundary a role binding can point at.</div>
        <button className="btn-primary" onClick={() => setShowAdd(true)}><PlusIcon size={14} /> New Scope</button>
      </div>

      <div className="users-list">
        {scopes.length === 0 && <div className="users-empty">No scopes yet.</div>}
        {scopes.map((s) => (
          <div className="user-row" key={s.id}>
            <div className="user-info">
              <div className="user-name">{s.label || <span className="field-hint">(unlabeled)</span>}</div>
              <div className="user-meta">
                {s.cloud ? <strong>{s.cloud}</strong> : "any cloud"}
                {" · "}{s.account_ref_id ? accountLabel(s.account_ref_id) : "any account"}
                {s.regions?.length ? ` · regions: ${listToCsv(s.regions)}` : ""}
                {s.services?.length ? ` · services: ${listToCsv(s.services)}` : ""}
              </div>
            </div>
            <div className="user-actions">
              <button className="btn-ghost" onClick={() => remove(s)}><TrashIcon size={13} /></button>
            </div>
          </div>
        ))}
      </div>

      {showAdd && (
        <Modal title="New scope" onClose={() => setShowAdd(false)} footer={
          <>
            <button className="btn-ghost" onClick={() => setShowAdd(false)}>Cancel</button>
            <button className="btn-primary" disabled={saving} onClick={submit}>{saving ? "Creating…" : "Create scope"}</button>
          </>
        }>
          <Field label="Label"><input value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} placeholder="e.g. Prod AWS ap-south-1" /></Field>
          <Field label="Cloud (leave blank for any)">
            <select value={form.cloud} onChange={(e) => setForm({ ...form, cloud: e.target.value, account_ref_id: "" })}>
              <option value="">Any cloud</option>
              <option value="aws">AWS</option>
              <option value="azure">Azure</option>
              <option value="gcp">GCP</option>
            </select>
          </Field>
          <Field label="Account (leave blank for any)">
            <select value={form.account_ref_id} onChange={(e) => setForm({ ...form, account_ref_id: e.target.value })}>
              <option value="">Any account</option>
              {accounts.filter((a) => !form.cloud || a.provider === form.cloud).map((a) => (
                <option key={a.id} value={a.id}>{a.account_name}</option>
              ))}
            </select>
          </Field>
          <Field label="Regions (comma-separated, blank = any)">
            <input value={form.regions} onChange={(e) => setForm({ ...form, regions: e.target.value })} placeholder="ap-south-1, us-east-1" />
          </Field>
          <Field label="Services (comma-separated, blank = any)">
            <input value={form.services} onChange={(e) => setForm({ ...form, services: e.target.value })} placeholder="ec2, rds" />
          </Field>
        </Modal>
      )}
    </div>
  );
}

// ── Bindings ─────────────────────────────────────────────────────────
function BindingsTab({ bindings, roles, scopes, users, reload, setError, userLabel }) {
  const [showAdd, setShowAdd] = useState(false);
  const [saving, setSaving] = useState(false);
  const EMPTY = { principal_type: "user", principal_id: "", role_id: "", scope_id: "", reason: "", expires_at: "" };
  const [form, setForm] = useState(EMPTY);

  const submit = async () => {
    if (!form.principal_id || !form.role_id || !form.scope_id) { setError("Principal, role, and scope are all required"); return; }
    setSaving(true);
    try {
      await createBinding({
        principal_type: form.principal_type,
        principal_id: Number(form.principal_id),
        role_id: Number(form.role_id),
        scope_id: Number(form.scope_id),
        reason: form.reason || undefined,
        expires_at: form.expires_at || undefined,
      });
      setShowAdd(false);
      setForm(EMPTY);
      reload();
    } catch (e) { setError(e.message); } finally { setSaving(false); }
  };

  const revoke = async (b) => {
    if (!confirm(`Revoke ${b.role_key} for ${b.principal_type} #${b.principal_id} at "${b.scope_label || b.scope_id}"?`)) return;
    try { await deleteBinding(b.id); reload(); } catch (e) { setError(e.message); }
  };

  return (
    <div>
      <div className="groups-panel-header">
        <div className="subtitle">Grants a role to a user or group at a specific scope. You can only grant a role/scope combination you yourself hold.</div>
        <button className="btn-primary" onClick={() => setShowAdd(true)}><PlusIcon size={14} /> New Binding</button>
      </div>

      <div className="users-list">
        {bindings.length === 0 && <div className="users-empty">No bindings yet.</div>}
        {bindings.map((b) => (
          <div className="user-row" key={b.id}>
            <div className="user-info">
              <div className="user-name">
                {b.principal_type === "user" ? userLabel(b.principal_id) : `Group #${b.principal_id}`}
                {" \u2192 "}<span className="role-badge role-admin">{b.role_key}</span>
              </div>
              <div className="user-meta">
                at <strong>{b.scope_label || `scope #${b.scope_id}`}</strong>
                {" · granted by "}{b.granted_by_username}
                {b.reason ? ` · ${b.reason}` : ""}
                {b.expires_at ? ` · expires ${new Date(b.expires_at).toLocaleDateString()}` : ""}
              </div>
            </div>
            <div className="user-actions">
              <button className="btn-ghost" onClick={() => revoke(b)}><TrashIcon size={13} /> Revoke</button>
            </div>
          </div>
        ))}
      </div>

      {showAdd && (
        <Modal title="New role binding" onClose={() => setShowAdd(false)} footer={
          <>
            <button className="btn-ghost" onClick={() => setShowAdd(false)}>Cancel</button>
            <button className="btn-primary" disabled={saving} onClick={submit}>{saving ? "Granting…" : "Grant binding"}</button>
          </>
        }>
          <Field label="Principal type">
            <select value={form.principal_type} onChange={(e) => setForm({ ...form, principal_type: e.target.value, principal_id: "" })}>
              <option value="user">User</option>
              <option value="group">Org group</option>
            </select>
          </Field>
          {form.principal_type === "user" ? (
            <Field label="User">
              <select value={form.principal_id} onChange={(e) => setForm({ ...form, principal_id: e.target.value })}>
                <option value="">Select a user…</option>
                {users.map((u) => <option key={u.id} value={u.id}>{u.username} ({u.role})</option>)}
              </select>
            </Field>
          ) : (
            <Field label="Group ID"><input value={form.principal_id} onChange={(e) => setForm({ ...form, principal_id: e.target.value })} placeholder="org_groups.id" /></Field>
          )}
          <Field label="Role">
            <select value={form.role_id} onChange={(e) => setForm({ ...form, role_id: e.target.value })}>
              <option value="">Select a role…</option>
              {roles.map((r) => <option key={r.id} value={r.id}>{r.name} ({r.role_key})</option>)}
            </select>
          </Field>
          <Field label="Scope">
            <select value={form.scope_id} onChange={(e) => setForm({ ...form, scope_id: e.target.value })}>
              <option value="">Select a scope…</option>
              {scopes.map((s) => <option key={s.id} value={s.id}>{s.label || `Scope #${s.id}`}</option>)}
            </select>
          </Field>
          <Field label="Reason (required for admin-rank roles)">
            <input value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} />
          </Field>
          <Field label="Expires (optional)">
            <input type="date" value={form.expires_at} onChange={(e) => setForm({ ...form, expires_at: e.target.value })} />
          </Field>
        </Modal>
      )}
    </div>
  );
}

// ── Overrides ────────────────────────────────────────────────────────
function OverridesTab({ overrides, users, scopes, permCatalog, reload, setError, userLabel }) {
  const [showAdd, setShowAdd] = useState(false);
  const [saving, setSaving] = useState(false);
  const EMPTY = { principal_type: "user", principal_id: "", permission_code: "", scope_id: "", effect: "deny", reason: "" };
  const [form, setForm] = useState(EMPTY);

  const submit = async () => {
    if (!form.principal_id || !form.permission_code || !form.reason.trim()) {
      setError("Principal, permission code, and a reason are all required for an override");
      return;
    }
    setSaving(true);
    try {
      await createOverride({
        principal_type: form.principal_type,
        principal_id: Number(form.principal_id),
        permission_code: form.permission_code,
        scope_id: form.scope_id ? Number(form.scope_id) : undefined,
        effect: form.effect,
        reason: form.reason,
      });
      setShowAdd(false);
      setForm(EMPTY);
      reload();
    } catch (e) { setError(e.message); } finally { setSaving(false); }
  };

  const remove = async (o) => {
    if (!confirm(`Remove this ${o.effect} override on ${o.permission_code}?`)) return;
    try { await deleteOverride(o.id); reload(); } catch (e) { setError(e.message); }
  };

  return (
    <div>
      <div className="groups-panel-header">
        <div className="subtitle">An explicit allow/deny exception outside the normal role grant. Every override needs a reason.</div>
        <button className="btn-primary" onClick={() => setShowAdd(true)}><PlusIcon size={14} /> New Override</button>
      </div>

      <div className="users-list">
        {overrides.length === 0 && <div className="users-empty">No overrides.</div>}
        {overrides.map((o) => (
          <div className="user-row" key={o.id}>
            <div className="user-info">
              <div className="user-name">
                <span className={`perm-chip ${o.effect === "deny" ? "denied" : "granted"}`}>{o.effect}</span>{" "}
                {o.permission_code} for {o.principal_type === "user" ? userLabel(o.principal_id) : `Group #${o.principal_id}`}
              </div>
              <div className="user-meta">
                {o.scope_label ? `at ${o.scope_label}` : "everywhere"} · by {o.granted_by_username} · {o.reason}
              </div>
            </div>
            <div className="user-actions">
              <button className="btn-ghost" onClick={() => remove(o)}><TrashIcon size={13} /></button>
            </div>
          </div>
        ))}
      </div>

      {showAdd && (
        <Modal title="New override" onClose={() => setShowAdd(false)} footer={
          <>
            <button className="btn-ghost" onClick={() => setShowAdd(false)}>Cancel</button>
            <button className="btn-primary" disabled={saving} onClick={submit}>{saving ? "Creating…" : "Create override"}</button>
          </>
        }>
          <Field label="Principal type">
            <select value={form.principal_type} onChange={(e) => setForm({ ...form, principal_type: e.target.value, principal_id: "" })}>
              <option value="user">User</option>
              <option value="group">Org group</option>
            </select>
          </Field>
          {form.principal_type === "user" ? (
            <Field label="User">
              <select value={form.principal_id} onChange={(e) => setForm({ ...form, principal_id: e.target.value })}>
                <option value="">Select a user…</option>
                {users.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
              </select>
            </Field>
          ) : (
            <Field label="Group ID"><input value={form.principal_id} onChange={(e) => setForm({ ...form, principal_id: e.target.value })} /></Field>
          )}
          <Field label="Permission code">
            <select value={form.permission_code} onChange={(e) => setForm({ ...form, permission_code: e.target.value })}>
              <option value="">Select a permission…</option>
              {permCatalog.map((p) => <option key={p.code} value={p.code}>{p.code}</option>)}
            </select>
          </Field>
          <Field label="Effect">
            <select value={form.effect} onChange={(e) => setForm({ ...form, effect: e.target.value })}>
              <option value="deny">Deny</option>
              <option value="allow">Allow</option>
            </select>
          </Field>
          <Field label="Scope (optional -- blank applies everywhere)">
            <select value={form.scope_id} onChange={(e) => setForm({ ...form, scope_id: e.target.value })}>
              <option value="">Everywhere</option>
              {scopes.map((s) => <option key={s.id} value={s.id}>{s.label || `Scope #${s.id}`}</option>)}
            </select>
          </Field>
          <Field label="Reason (required)">
            <input value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} />
          </Field>
        </Modal>
      )}
    </div>
  );
}

// ── Access Reviews ───────────────────────────────────────────────────
function ReviewsTab({ reviews, users, bindings, principalId, setPrincipalId, reload, setError }) {
  const [showAdd, setShowAdd] = useState(false);
  const [saving, setSaving] = useState(false);
  const EMPTY = { principal_type: "user", principal_id: "", binding_id: "", decision: "retain", notes: "" };
  const [form, setForm] = useState(EMPTY);

  const submit = async () => {
    if (!form.principal_id) { setError("Principal is required"); return; }
    setSaving(true);
    try {
      await createReview({
        principal_type: form.principal_type,
        principal_id: Number(form.principal_id),
        binding_id: form.binding_id ? Number(form.binding_id) : undefined,
        decision: form.decision,
        notes: form.notes || undefined,
      });
      setShowAdd(false);
      setForm(EMPTY);
      reload();
    } catch (e) { setError(e.message); } finally { setSaving(false); }
  };

  return (
    <div>
      <div className="groups-panel-header">
        <div className="subtitle">
          An attestation log -- recording a decision here does NOT itself revoke a binding.
          To actually remove access, use the Revoke button on the Bindings tab.
        </div>
        <button className="btn-primary" onClick={() => setShowAdd(true)}><PlusIcon size={14} /> Record Review</button>
      </div>

      <div className="mfield" style={{ maxWidth: 320, marginBottom: 16 }}>
        <label>Filter by principal</label>
        <select value={principalId} onChange={(e) => setPrincipalId(e.target.value)}>
          <option value="">All reviews (most recent 500)</option>
          {users.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
        </select>
      </div>

      <div className="users-list">
        {reviews.length === 0 && <div className="users-empty">No reviews recorded.</div>}
        {reviews.map((r) => (
          <div className="user-row" key={r.id}>
            <div className="user-info">
              <div className="user-name">
                <span className={`perm-chip ${r.decision === "revoke" ? "denied" : "granted"}`}>{r.decision}</span>{" "}
                {r.principal_type} #{r.principal_id}{r.binding_id ? ` (binding #${r.binding_id})` : ""}
              </div>
              <div className="user-meta">
                by {r.reviewed_by_username} on {new Date(r.reviewed_at).toLocaleDateString()}
                {r.notes ? ` · ${r.notes}` : ""}
              </div>
            </div>
          </div>
        ))}
      </div>

      {showAdd && (
        <Modal title="Record an access review" onClose={() => setShowAdd(false)} footer={
          <>
            <button className="btn-ghost" onClick={() => setShowAdd(false)}>Cancel</button>
            <button className="btn-primary" disabled={saving} onClick={submit}>{saving ? "Recording…" : "Record review"}</button>
          </>
        }>
          <Field label="Principal type">
            <select value={form.principal_type} onChange={(e) => setForm({ ...form, principal_type: e.target.value, principal_id: "" })}>
              <option value="user">User</option>
              <option value="group">Org group</option>
            </select>
          </Field>
          {form.principal_type === "user" ? (
            <Field label="User">
              <select value={form.principal_id} onChange={(e) => setForm({ ...form, principal_id: e.target.value })}>
                <option value="">Select a user…</option>
                {users.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
              </select>
            </Field>
          ) : (
            <Field label="Group ID"><input value={form.principal_id} onChange={(e) => setForm({ ...form, principal_id: e.target.value })} /></Field>
          )}
          <Field label="Specific binding (optional)">
            <select value={form.binding_id} onChange={(e) => setForm({ ...form, binding_id: e.target.value })}>
              <option value="">Not tied to a specific binding</option>
              {bindings.filter((b) => String(b.principal_id) === String(form.principal_id) && b.principal_type === form.principal_type)
                .map((b) => <option key={b.id} value={b.id}>{b.role_key} @ {b.scope_label || b.scope_id}</option>)}
            </select>
          </Field>
          <Field label="Decision">
            <select value={form.decision} onChange={(e) => setForm({ ...form, decision: e.target.value })}>
              <option value="retain">Retain</option>
              <option value="revoke">Revoke (record intent -- then revoke the binding separately)</option>
              <option value="modify">Modify</option>
            </select>
          </Field>
          <Field label="Notes">
            <input value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          </Field>
        </Modal>
      )}
    </div>
  );
}
