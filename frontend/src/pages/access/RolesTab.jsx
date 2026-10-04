// src/pages/access/RolesTab.jsx
//
// The ONE place roles and permissions are shown. Replaces both the
// "Roles & Permissions" matrix in User Management (whose columns were
// mislabelled L1/L2/L3 but actually meant Viewer/Editor/Admin) and the
// "Roles" tab in RBAC Administration.
import { useMemo, useState, useEffect } from "react";
import { useAuth } from "../../auth/AuthContext";
import * as api from "../../api/access";
import { PlusIcon, SearchIcon, ShieldIcon, CheckIcon, MinusIcon } from "../../components/icons";
import { Badge, Modal, ConfirmDialog, Field, Banner, SkeletonRows, useToast, useAsync, useDebounced } from "./ui";
import { plural } from "../../utils/plural";

function NewRoleModal({ source, onClose, onCreated }) {
  const toast = useToast();
  const [f, setF] = useState({ name: "", key: "", description: "" });
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const slug = (s) => s.toLowerCase().trim().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40);

  async function submit() {
    if (!f.name.trim()) return setErr("Name is required");
    const key = f.key || slug(f.name);
    if (!/^[a-z][a-z0-9_]{1,39}$/.test(key)) return setErr("Key must start with a letter: lowercase letters, digits, underscores");
    setBusy(true);
    try {
      const body = { name: f.name.trim(), role_key: key, description: f.description.trim() || undefined };
      const created = source ? await api.cloneRole(source.id, body) : await api.createRole({ ...body, permissions: [] });
      toast(`Role "${f.name.trim()}" created`);
      onCreated(created?.id); onClose();
    } catch (e) { setErr(api.errMsg(e)); setBusy(false); }
  }

  return (
    <Modal title={source ? `Clone “${source.name}”` : "New custom role"} onClose={onClose} busy={busy}
      footer={<><button className="ac-btn ghost" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="ac-btn primary" onClick={submit} disabled={busy}>{busy ? "Creating…" : "Create role"}</button></>}>
      {err && <Banner tone="err">{err}</Banner>}
      <Field label="Display name"><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="e.g. Read-only + Reports" /></Field>
      <Field label="Role key" hint="Permanent identifier. Auto-filled from the name.">
        <input value={f.key || slug(f.name)} onChange={(e) => setF({ ...f, key: slug(e.target.value) })} />
      </Field>
      <Field label="Description (optional)"><input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      {source && <p className="muted small">Starts with the same permissions as {source.name}; adjust them after creating.</p>}
    </Modal>
  );
}

function Editor({ role, catalog, canManage, onSaved, onClone, onDelete }) {
  const toast = useToast();
  const original = useMemo(() => new Set(role.permissions || []), [role]);
  const [sel, setSel] = useState(original);
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 150);
  const [saving, setSaving] = useState(false);
  useEffect(() => setSel(new Set(role.permissions || [])), [role]);

  const locked = role.role_key === "admin" || !canManage;
  const dirty = sel.size !== original.size || [...sel].some((c) => !original.has(c));
  const needle = dq.trim().toLowerCase();

  const cats = useMemo(() => catalog.map((c) => ({
    ...c,
    permissions: c.permissions.filter((p) => !needle || p.code.includes(needle) || p.label.toLowerCase().includes(needle)),
  })).filter((c) => c.permissions.length), [catalog, needle]);

  const toggle = (code) => setSel((s) => { const n = new Set(s); n.has(code) ? n.delete(code) : n.add(code); return n; });
  const setCat = (c, on) => setSel((s) => { const n = new Set(s); c.permissions.forEach((p) => (on ? n.add(p.code) : n.delete(p.code))); return n; });

  async function save() {
    setSaving(true);
    try { await api.setRolePerms(role.id, [...sel]); toast(`Saved ${role.name}`); onSaved(); }
    catch (e) { toast(api.errMsg(e), "err"); }
    finally { setSaving(false); }
  }

  const total = catalog.reduce((n, c) => n + c.permissions.length, 0);
  return (
    <div className="ac-roleeditor">
      <div className="ac-roletop">
        <div>
          <h3>{role.name} {role.is_builtin && <Badge tone="muted">Built-in</Badge>}</h3>
          <p className="muted">{role.description || "No description"}</p>
          <p className="muted small">
            {role.role_key === "admin" ? total : sel.size} of {total} permissions
            {role.is_builtin ? ` · ${role.user_count} user${role.user_count === 1 ? "" : "s"}` : ""}
            {role.binding_count ? ` · ${role.binding_count} binding${role.binding_count === 1 ? "" : "s"}` : ""}
          </p>
        </div>
        <div className="ac-row-gap">
          {canManage && <button className="ac-btn ghost" onClick={() => onClone(role)}>Clone</button>}
          {canManage && !role.is_builtin && <button className="ac-btn danger" onClick={() => onDelete(role)}>Delete</button>}
        </div>
      </div>

      {role.role_key === "admin" && <Banner tone="info">Administrators always hold every permission. This role is read-only so no one can lock themselves out.</Banner>}
      {!canManage && role.role_key !== "admin" && <Banner tone="info">You can view this role but not change it.</Banner>}

      <div className="ac-search wide"><SearchIcon size={14} />
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter permissions" aria-label="Filter permissions" /></div>

      <div className="ac-permgrid">
        {cats.map((c) => {
          const have = c.permissions.filter((p) => role.role_key === "admin" || sel.has(p.code)).length;
          return (
            <fieldset key={c.category} className="ac-permcat" disabled={locked}>
              <legend>
                <span>{c.category}</span>
                <span className="muted small">{have}/{c.permissions.length}</span>
                {!locked && <>
                  <button type="button" className="ac-link" onClick={() => setCat(c, true)}>all</button>
                  <button type="button" className="ac-link" onClick={() => setCat(c, false)}>none</button>
                </>}
              </legend>
              {c.permissions.map((p) => (
                <label key={p.code} className="ac-perm" title={p.description || p.code}>
                  <input type="checkbox" checked={role.role_key === "admin" || sel.has(p.code)} onChange={() => toggle(p.code)} />
                  <span>{p.label}</span><code>{p.code}</code>
                </label>
              ))}
            </fieldset>
          );
        })}
        {cats.length === 0 && <p className="muted">No permissions match “{dq}”.</p>}
      </div>

      {!locked && (
        <div className="ac-savebar" data-dirty={dirty}>
          <span>{dirty ? "You have unsaved changes" : "All changes saved"}</span>
          <button className="ac-btn ghost" onClick={() => setSel(new Set(original))} disabled={!dirty || saving}>Discard</button>
          <button className="ac-btn primary" onClick={save} disabled={!dirty || saving}>{saving ? "Saving…" : "Save permissions"}</button>
        </div>
      )}
    </div>
  );
}

function Matrix({ roles, catalog }) {
  const [q, setQ] = useState("");
  const needle = useDebounced(q, 150).trim().toLowerCase();
  return (
    <div>
      <div className="ac-search wide"><SearchIcon size={14} />
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter permissions" aria-label="Filter matrix" /></div>
      <div className="ac-table-wrap">
        <table className="ac-table matrix">
          <thead><tr><th>Permission</th>{roles.map((r) => <th key={r.id} className="c">{r.name}</th>)}</tr></thead>
          <tbody>
            {catalog.map((c) => {
              const rows = c.permissions.filter((p) => !needle || p.code.includes(needle) || p.label.toLowerCase().includes(needle));
              if (!rows.length) return null;
              return [
                <tr key={c.category} className="cat"><td colSpan={roles.length + 1}>{c.category}</td></tr>,
                ...rows.map((p) => (
                  <tr key={p.code}>
                    <td>{p.label} <code className="muted">{p.code}</code></td>
                    {roles.map((r) => {
                      const on = r.role_key === "admin" || (r.permissions || []).includes(p.code);
                      return <td key={r.id} className="c"><span className={`ac-dot ${on ? "on" : "off"}`} aria-label={on ? "granted" : "not granted"}>{on ? <CheckIcon size={14} /> : <MinusIcon size={14} />}</span></td>;
                    })}
                  </tr>
                )),
              ];
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function RolesTab() {
  const { hasPermission } = useAuth();
  const toast = useToast();
  const roles = useAsync(api.listRoles, []);
  const catalog = useAsync(api.permissionCatalog, []);
  const [selId, setSelId] = useState(null);
  const [view, setView] = useState("edit");
  const [modal, setModal] = useState(null);
  const [confirm, setConfirm] = useState(null);

  const canManage = hasPermission("permissions.manage");
  const canCreate = hasPermission("roles.create");
  const list = roles.data || [];
  const sel = list.find((r) => r.id === selId) || list[0];

  return (
    <div>
      <div className="ac-toolbar">
        <div className="ac-seg" role="tablist" aria-label="Roles view">
          <button role="tab" aria-selected={view === "edit"} className={view === "edit" ? "on" : ""} onClick={() => setView("edit")}>Roles</button>
          <button role="tab" aria-selected={view === "matrix"} className={view === "matrix" ? "on" : ""} onClick={() => setView("matrix")}>Compare</button>
        </div>
        <span className="ac-count" />
        {canCreate && <button className="ac-btn primary" onClick={() => setModal({ source: null })}><PlusIcon size={14} /> New role</button>}
      </div>

      {(roles.error || catalog.error) && <Banner tone="err" action={<button className="ac-link" onClick={() => { roles.reload(); catalog.reload(); }}>Retry</button>}>{api.errMsg(roles.error || catalog.error)}</Banner>}
      {(roles.loading || catalog.loading) && !roles.data && <SkeletonRows n={5} />}

      {roles.data && catalog.data && (view === "matrix"
        ? <Matrix roles={list} catalog={catalog.data} />
        : (
          <div className="ac-split">
            <nav className="ac-rolelist" aria-label="Roles">
              {list.map((r) => (
                <button key={r.id} className={sel?.id === r.id ? "on" : ""} onClick={() => setSelId(r.id)}>
                  <ShieldIcon size={14} />
                  <span><strong>{r.name}</strong><em>{r.role_key === "admin" ? "All" : r.permission_count} permissions{r.is_builtin ? "" : " · custom"}</em></span>
                </button>
              ))}
            </nav>
            {sel && <Editor key={sel.id} role={sel} catalog={catalog.data} canManage={canManage}
              onSaved={roles.reload}
              onClone={(r) => setModal({ source: r })}
              onDelete={(r) => setConfirm({
                title: `Delete role "${r.name}"?`, confirmLabel: "Delete role", danger: true,
                body: <p>{r.binding_count ? `${plural(r.binding_count, "binding")} ${r.binding_count === 1 ? "uses" : "use"} this role and will block deletion — revoke ${r.binding_count === 1 ? "it" : "them"} first.` : "This role is not used by any binding."}</p>,
                run: async () => { await api.deleteRole(r.id); toast(`Role "${r.name}" deleted`); setSelId(null); roles.reload(); },
              })} />}
          </div>
        ))}

      {modal && <NewRoleModal source={modal.source} onClose={() => setModal(null)} onCreated={(id) => { roles.reload(); if (id) setSelId(id); }} />}
      {confirm && <ConfirmDialog {...confirm} onConfirm={confirm.run} onClose={() => setConfirm(null)} />}
    </div>
  );
}
