// src/pages/access/AdvancedTab.jsx
//
// Scopes, role bindings and deny overrides (the "v2" engine). One tab with
// three sections instead of three separate tabs.
//
// ENFORCEMENT STATUS (shown to the admin, not hidden):
//   • Unscoped DENY overrides  → enforced at the API gate (permissions.py).
//   • Role bindings, scoped overrides, custom-role grants → recorded,
//     audited and previewable, NOT yet enforced on data endpoints.
// The banner below says exactly that so nobody assumes a binding is a
// working security boundary.
import { useState } from "react";
import { useAuth } from "../../auth/AuthContext";
import * as api from "../../api/access";
import { PlusIcon } from "../../components/icons";
import { Badge, Modal, ConfirmDialog, Field, Banner, Empty, SkeletonRows, KebabMenu, useToast, useAsync, fmtDate, expiryLabel } from "./ui";

const toIsoEnd = (d) => (d ? new Date(`${d}T23:59:59`).toISOString() : undefined);
const tomorrow = () => new Date(Date.now() + 86400000).toISOString().slice(0, 10);

/* ── Scopes ── */
function ScopeModal({ accounts, onClose, onSaved }) {
  const toast = useToast();
  const [f, setF] = useState({ label: "", cloud: "", account: "", regions: "" });
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const clouds = [...new Set(accounts.map((a) => a.provider || "aws"))];
  const accts = accounts.filter((a) => !f.cloud || (a.provider || "aws") === f.cloud);

  async function submit() {
    if (!f.label.trim()) return setErr("Label is required");
    setBusy(true);
    try {
      const regions = f.regions.split(",").map((s) => s.trim()).filter(Boolean);
      await api.createScope({
        label: f.label.trim(), cloud: f.cloud || undefined,
        account_ref_id: f.account ? Number(f.account) : undefined,
        regions: regions.length ? regions : undefined,
      });
      toast("Scope created"); onSaved(); onClose();
    } catch (e) { setErr(api.errMsg(e)); setBusy(false); }
  }
  return (
    <Modal title="New scope" onClose={onClose} busy={busy}
      footer={<><button className="ac-btn ghost" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="ac-btn primary" onClick={submit} disabled={busy}>Create scope</button></>}>
      {err && <Banner tone="err">{err}</Banner>}
      <Field label="Label"><input value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} placeholder="e.g. Prod AWS ap-south-1" /></Field>
      <div className="ac-grid2">
        <Field label="Cloud"><select value={f.cloud} onChange={(e) => setF({ ...f, cloud: e.target.value, account: "" })}>
          <option value="">Any cloud</option>{clouds.map((c) => <option key={c} value={c}>{c.toUpperCase()}</option>)}</select></Field>
        <Field label="Account"><select value={f.account} onChange={(e) => setF({ ...f, account: e.target.value })}>
          <option value="">Any account</option>{accts.map((a) => <option key={a.id} value={a.id}>{a.account_name}</option>)}</select></Field>
      </div>
      <Field label="Regions" hint="Comma-separated; blank means all regions"><input value={f.regions} onChange={(e) => setF({ ...f, regions: e.target.value })} placeholder="ap-south-1, us-east-1" /></Field>
    </Modal>
  );
}

function Scopes({ canManage, accounts }) {
  const toast = useToast();
  const scopes = useAsync(api.listScopes, []);
  const [add, setAdd] = useState(false);
  const [confirm, setConfirm] = useState(null);
  return (
    <section className="ac-panel">
      <header><div><h3>Scopes</h3><p className="muted">Reusable cloud / account / region boundaries that bindings and overrides point at.</p></div>
        {canManage && <button className="ac-btn ghost" onClick={() => setAdd(true)}><PlusIcon size={13} /> New scope</button>}</header>
      {scopes.error && <Banner tone="err">{api.errMsg(scopes.error)}</Banner>}
      {scopes.loading && !scopes.data && <SkeletonRows n={2} />}
      {scopes.data && (scopes.data.length === 0 ? <Empty title="No scopes" /> : (
        <ul className="ac-list roomy">
          {scopes.data.map((s) => (
            <li key={s.id}>
              <span><strong>{s.label}</strong>{s.is_system ? <> <Badge tone="muted">Built-in</Badge></> : null}
                <div className="muted small">{[s.cloud ? s.cloud.toUpperCase() : "Any cloud", s.account_name || (s.account_ref_id ? `Account #${s.account_ref_id}` : "any account"),
                  (s.regions || []).length ? s.regions.join(", ") : "all regions"].join(" · ")}
                  {" · "}{s.binding_count} binding{s.binding_count === 1 ? "" : "s"}{s.override_count ? `, ${s.override_count} override${s.override_count === 1 ? "" : "s"}` : ""}</div></span>
              {canManage && !s.is_system && <button className="ac-link danger" onClick={() => setConfirm({
                title: `Delete scope "${s.label}"?`, confirmLabel: "Delete scope", danger: true,
                body: <p>{s.binding_count ? `${s.binding_count} binding(s) use this scope; revoke them first.` : "No binding uses this scope."}</p>,
                run: async () => { await api.deleteScope(s.id); toast("Scope deleted"); scopes.reload(); },
              })}>Delete</button>}
            </li>
          ))}
        </ul>
      ))}
      {add && <ScopeModal accounts={accounts} onClose={() => setAdd(false)} onSaved={scopes.reload} />}
      {confirm && <ConfirmDialog {...confirm} onConfirm={confirm.run} onClose={() => setConfirm(null)} />}
    </section>
  );
}

/* ── Principal picker (user or group) ── */
function PrincipalPicker({ value, onChange, users, groups }) {
  return (
    <div className="ac-grid2">
      <Field label="Applies to">
        <select value={value.type} onChange={(e) => onChange({ type: e.target.value, id: "" })}>
          <option value="user">A user</option><option value="group">A group</option>
        </select>
      </Field>
      <Field label={value.type === "user" ? "User" : "Group"}>
        <select value={value.id} onChange={(e) => onChange({ ...value, id: e.target.value })}>
          <option value="">Select…</option>
          {(value.type === "user" ? users.filter((u) => u.active && u.role !== "admin") : groups)
            .map((p) => <option key={p.id} value={p.id}>{p.username || p.name}</option>)}
        </select>
      </Field>
    </div>
  );
}

/* ── Bindings ── */
function BindingModal({ users, groups, onClose, onSaved }) {
  const toast = useToast();
  const roles = useAsync(api.listRoles, []);
  const scopes = useAsync(api.listScopes, []);
  const [p, setP] = useState({ type: "user", id: "" });
  const [f, setF] = useState({ role: "", scope: "", reason: "", expires: "" });
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const role = (roles.data || []).find((r) => r.id === Number(f.role));
  const adminRank = role && role.role_key === "admin";

  async function submit() {
    if (!p.id || !f.role || !f.scope) return setErr("Choose who, which role and which scope");
    setBusy(true);
    try {
      await api.createBinding({
        principal_type: p.type, principal_id: Number(p.id), role_id: Number(f.role), scope_id: Number(f.scope),
        reason: f.reason.trim() || undefined, expires_at: toIsoEnd(f.expires),
      });
      toast("Binding created"); onSaved(); onClose();
    } catch (e) { setErr(api.errMsg(e)); setBusy(false); }
  }
  return (
    <Modal title="New role binding" onClose={onClose} busy={busy}
      footer={<><button className="ac-btn ghost" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="ac-btn primary" onClick={submit} disabled={busy}>Grant binding</button></>}>
      {err && <Banner tone="err">{err}</Banner>}
      <PrincipalPicker value={p} onChange={setP} users={users} groups={groups} />
      <div className="ac-grid2">
        <Field label="Role"><select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
          <option value="">Select…</option>{(roles.data || []).map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}</select></Field>
        <Field label="Scope"><select value={f.scope} onChange={(e) => setF({ ...f, scope: e.target.value })}>
          <option value="">Select…</option>{(scopes.data || []).map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select></Field>
      </div>
      <Field label={adminRank ? "Reason (required for admin-rank roles)" : "Reason (optional)"}><input value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} /></Field>
      <Field label="Expires (optional)" hint="Time-boxed access is the safest default for elevated roles"><input type="date" min={tomorrow()} value={f.expires} onChange={(e) => setF({ ...f, expires: e.target.value })} /></Field>
    </Modal>
  );
}

function Bindings({ canManage, users, groups }) {
  const toast = useToast();
  const list = useAsync(api.listBindings, []);
  const [add, setAdd] = useState(false);
  const [confirm, setConfirm] = useState(null);
  return (
    <section className="ac-panel">
      <header><div><h3>Role bindings</h3><p className="muted">Grant a role to a user or group at a scope, optionally time-boxed.</p></div>
        {canManage && <button className="ac-btn ghost" onClick={() => setAdd(true)}><PlusIcon size={13} /> New binding</button>}</header>
      {list.error && <Banner tone="err">{api.errMsg(list.error)}</Banner>}
      {list.loading && !list.data && <SkeletonRows n={2} />}
      {list.data && (list.data.length === 0 ? <Empty title="No bindings yet" hint="Users get access from their base role, groups and direct account grants." /> : (
        <div className="ac-table-wrap"><table className="ac-table">
          <thead><tr><th>Principal</th><th>Role</th><th>Scope</th><th>Expiry</th><th>Reviewed</th><th /></tr></thead>
          <tbody>{list.data.map((b) => {
            const ex = expiryLabel(b.expires_at);
            return (
              <tr key={b.id} className={b.expired ? "inactive" : ""}>
                <td><Badge tone={b.principal_type === "user" ? "teal" : "purple"}>{b.principal_type}</Badge> {b.principal_name || `#${b.principal_id}`}</td>
                <td>{b.role_name}</td><td>{b.scope_label}</td>
                <td><Badge tone={ex.tone}>{ex.text}</Badge></td>
                <td className="muted small">{b.last_reviewed_at ? fmtDate(b.last_reviewed_at) : "Never"}</td>
                <td>{canManage && <KebabMenu items={[
                  { key: "ext", label: "Set expiry…", onClick: () => setConfirm({ kind: "expiry", b }) },
                  { key: "rev", label: "Revoke…", danger: true, onClick: () => setConfirm({ kind: "revoke", b }) },
                ]} />}</td>
              </tr>
            );
          })}</tbody></table></div>
      ))}
      {add && <BindingModal users={users} groups={groups} onClose={() => setAdd(false)} onSaved={list.reload} />}
      {confirm?.kind === "revoke" && <ConfirmDialog title="Revoke binding?" confirmLabel="Revoke" danger
        body={<p>Removes <strong>{confirm.b.role_name}</strong> from <strong>{confirm.b.principal_name}</strong> at <strong>{confirm.b.scope_label}</strong>.</p>}
        onConfirm={async () => { await api.deleteBinding(confirm.b.id); toast("Binding revoked"); list.reload(); }} onClose={() => setConfirm(null)} />}
      {confirm?.kind === "expiry" && <ExpiryModal b={confirm.b} onClose={() => setConfirm(null)} onSaved={list.reload} />}
    </section>
  );
}

function ExpiryModal({ b, onClose, onSaved }) {
  const toast = useToast();
  const [d, setD] = useState(b.expires_at ? b.expires_at.slice(0, 10) : "");
  const [err, setErr] = useState(null);
  async function save() {
    try { await api.updateBinding(b.id, { expires_at: d ? toIsoEnd(d) : null }); toast("Expiry updated"); onSaved(); onClose(); }
    catch (e) { setErr(api.errMsg(e)); }
  }
  return (
    <Modal title="Binding expiry" onClose={onClose} width={400}
      footer={<><button className="ac-btn ghost" onClick={onClose}>Cancel</button><button className="ac-btn primary" onClick={save}>Save</button></>}>
      {err && <Banner tone="err">{err}</Banner>}
      <Field label="Expires" hint="Leave blank for no expiry"><input type="date" min={tomorrow()} value={d} onChange={(e) => setD(e.target.value)} /></Field>
    </Modal>
  );
}

/* ── Overrides ── */
function OverrideModal({ users, groups, onClose, onSaved }) {
  const toast = useToast();
  const catalog = useAsync(api.permissionCatalog, []);
  const [p, setP] = useState({ type: "user", id: "" });
  const [f, setF] = useState({ code: "", reason: "", expires: "" });
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  async function submit() {
    if (!p.id || !f.code) return setErr("Choose who and which permission");
    if (!f.reason.trim()) return setErr("A reason is required");
    setBusy(true);
    try {
      await api.createOverride({ principal_type: p.type, principal_id: Number(p.id), permission_code: f.code, effect: "deny", reason: f.reason.trim(), expires_at: toIsoEnd(f.expires) });
      toast("Deny override created"); onSaved(); onClose();
    } catch (e) { setErr(api.errMsg(e)); setBusy(false); }
  }
  return (
    <Modal title="New deny override" onClose={onClose} busy={busy}
      footer={<><button className="ac-btn ghost" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="ac-btn danger" onClick={submit} disabled={busy}>Create deny</button></>}>
      {err && <Banner tone="err">{err}</Banner>}
      <Banner tone="info">Removes one permission from someone even though their role grants it. Administrators can't be denied — change their role instead.</Banner>
      <PrincipalPicker value={p} onChange={setP} users={users} groups={groups} />
      <Field label="Permission"><select value={f.code} onChange={(e) => setF({ ...f, code: e.target.value })}>
        <option value="">Select…</option>
        {(catalog.data || []).map((c) => <optgroup key={c.category} label={c.category}>{c.permissions.map((x) => <option key={x.code} value={x.code}>{x.label} — {x.code}</option>)}</optgroup>)}
      </select></Field>
      <Field label="Reason (required)"><input value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} /></Field>
      <Field label="Expires (optional)"><input type="date" min={tomorrow()} value={f.expires} onChange={(e) => setF({ ...f, expires: e.target.value })} /></Field>
    </Modal>
  );
}

function Overrides({ canManage, users, groups }) {
  const toast = useToast();
  const list = useAsync(api.listOverrides, []);
  const [add, setAdd] = useState(false);
  const [confirm, setConfirm] = useState(null);
  return (
    <section className="ac-panel">
      <header><div><h3>Deny overrides</h3><p className="muted">Take a specific permission away from a user or group. Enforced at the API.</p></div>
        {canManage && <button className="ac-btn ghost" onClick={() => setAdd(true)}><PlusIcon size={13} /> New deny</button>}</header>
      {list.error && <Banner tone="err">{api.errMsg(list.error)}</Banner>}
      {list.loading && !list.data && <SkeletonRows n={2} />}
      {list.data && (list.data.length === 0 ? <Empty title="No overrides" /> : (
        <ul className="ac-list roomy">{list.data.map((o) => (
          <li key={o.id}>
            <span><Badge tone="red">DENY</Badge> <code>{o.permission_code}</code> <span className="muted">for {o.principal_type} {o.principal_name || `#${o.principal_id}`}</span>
              <div className="muted small">{o.reason}{o.expires_at ? ` · ${expiryLabel(o.expires_at).text}` : ""}{o.scope_label ? ` · scoped to ${o.scope_label} (not yet enforced)` : ""}</div></span>
            {canManage && <button className="ac-link danger" onClick={() => setConfirm(o)}>Remove</button>}
          </li>))}</ul>
      ))}
      {add && <OverrideModal users={users} groups={groups} onClose={() => setAdd(false)} onSaved={list.reload} />}
      {confirm && <ConfirmDialog title="Remove override?" confirmLabel="Remove" body={<p>The permission becomes available again to {confirm.principal_name}.</p>}
        onConfirm={async () => { await api.deleteOverride(confirm.id); toast("Override removed"); list.reload(); }} onClose={() => setConfirm(null)} />}
    </section>
  );
}

export default function AdvancedTab() {
  const { hasPermission } = useAuth();
  const users = useAsync(() => api.listUsers().catch(() => []), []);
  const groups = useAsync(() => api.listGroups().catch(() => []), []);
  const accounts = useAsync(() => api.listAccounts().then((d) => (Array.isArray(d) ? d : [])).catch(() => []), []);
  const u = users.data || [], g = groups.data || [];
  return (
    <div className="ac-stack">
      <Banner tone="warn">
        <strong>Enforcement status.</strong> Unscoped deny overrides are enforced on every API call. Role bindings, scoped overrides and
        custom-role grants are recorded, audited and reviewable, but are <strong>not yet applied to data access</strong> — access to accounts
        still comes from a user's base role, groups and direct account grants. Don't rely on a binding as a security boundary yet.
      </Banner>
      {hasPermission("rbac.scope.view") && <Scopes canManage={hasPermission("rbac.scope.manage")} accounts={accounts.data || []} />}
      {hasPermission("rbac.binding.view") && <Bindings canManage={hasPermission("rbac.binding.manage")} users={u} groups={g} />}
      {hasPermission("rbac.override.manage") && <Overrides canManage users={u} groups={g} />}
    </div>
  );
}
