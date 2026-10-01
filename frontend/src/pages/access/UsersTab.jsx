// src/pages/access/UsersTab.jsx
import { useMemo, useState, useEffect } from "react";
import { useAuth } from "../../auth/AuthContext";
import * as api from "../../api/access";
import { PlusIcon, SearchIcon, UsersIcon, XIcon, KeyIcon } from "../../components/icons";
import {
  Avatar, Badge, RoleBadge, Modal, ConfirmDialog, Field, Banner, Empty, SkeletonRows,
  KebabMenu, useToast, useAsync, useDebounced, timeAgo, fmtDate, expiryLabel,
} from "./ui";

const ROLES = ["admin", "editor", "viewer"];
const ROLE_HELP = {
  admin: "Full control, including users and access.",
  editor: "Configure alerts and onboarding within their accounts.",
  viewer: "Read-only access within their accounts.",
};

function genPassword() {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789!@#$%";
  const buf = new Uint32Array(16);
  crypto.getRandomValues(buf);
  return Array.from(buf, (n) => chars[n % chars.length]).join("");
}

/* ───────────────────────── Add user ───────────────────────── */
function AddUserModal({ onClose, onCreated, groups, accounts, isAdmin }) {
  const toast = useToast();
  const [f, setF] = useState({ username: "", password: "", email: "", role: "viewer", groupId: "", accountIds: [] });
  const [errs, setErrs] = useState({});
  const [busy, setBusy] = useState(false);
  const [showPw, setShowPw] = useState(false);
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));

  function validate() {
    const e = {};
    if (!f.username.trim()) e.username = "Username is required";
    else if (!/^[A-Za-z0-9][A-Za-z0-9._@+-]*$/.test(f.username.trim())) e.username = "Letters, digits and . _ @ + - only";
    if (f.password.length < 8) e.password = "At least 8 characters";
    if (f.email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(f.email.trim())) e.email = "Not a valid email address";
    if (!isAdmin && f.accountIds.length === 0) e.accountIds = "Select at least one account";
    return e;
  }

  async function submit() {
    const e = validate();
    setErrs(e);
    if (Object.keys(e).length) return;
    setBusy(true);
    let created;
    try {
      created = await api.createUser({
        username: f.username.trim(), password: f.password, role: f.role,
        email: f.email.trim() || undefined,
        // Sent WITH the create call so an editor's scoped create is one atomic request
        scopes: f.accountIds.map((id) => ({
          cloud: accounts.find((a) => a.id === Number(id))?.provider || "aws",
          account_ref_id: Number(id),
        })),
      });
    } catch (err) {
      setErrs({ submit: api.errMsg(err) });
      setBusy(false);
      return;
    }
    // The user exists now — anything below is a follow-up that must not
    // masquerade as "creation failed".
    const problems = [];
    if (f.groupId) {
      try { await api.addGroupMembers(Number(f.groupId), [created.id]); }
      catch (err) { problems.push(`group assignment: ${api.errMsg(err)}`); }
    }
    setBusy(false);
    if (problems.length) toast(`User created, but ${problems.join("; ")}`, "err");
    else toast(`User "${f.username.trim()}" created`);
    onCreated();
    onClose();
  }

  return (
    <Modal title="Add user" onClose={onClose} busy={busy} width={520}
      footer={<>
        <button className="ac-btn ghost" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="ac-btn primary" onClick={submit} disabled={busy}>{busy ? "Creating…" : "Create user"}</button>
      </>}>
      {errs.submit && <Banner tone="err">{errs.submit}</Banner>}
      <div className="ac-grid2">
        <Field label="Username" error={errs.username}>
          <input value={f.username} onChange={(e) => set("username", e.target.value)} placeholder="e.g. john.doe" autoComplete="off" />
        </Field>
        <Field label="Email (optional)" error={errs.email} hint="Used for the welcome and reset emails">
          <input value={f.email} onChange={(e) => set("email", e.target.value)} placeholder="john.doe@aurionpro.com" autoComplete="off" />
        </Field>
      </div>
      <Field label="Temporary password" error={errs.password} hint="Minimum 8 characters. Share it out-of-band, or use Reset password later to send a set-your-own link.">
        <div className="ac-inline">
          <input type={showPw ? "text" : "password"} value={f.password} onChange={(e) => set("password", e.target.value)} autoComplete="new-password" />
          <button type="button" className="ac-btn ghost" onClick={() => setShowPw((s) => !s)}>{showPw ? "Hide" : "Show"}</button>
          <button type="button" className="ac-btn ghost" onClick={() => { set("password", genPassword()); setShowPw(true); }}>Generate</button>
        </div>
      </Field>
      <div className="ac-grid2">
        <Field label="Role" hint={ROLE_HELP[f.role]}>
          <select value={f.role} onChange={(e) => set("role", e.target.value)} disabled={!isAdmin}>
            {(isAdmin ? ROLES : ["viewer"]).map((r) => <option key={r} value={r}>{r[0].toUpperCase() + r.slice(1)}</option>)}
          </select>
        </Field>
        <Field label="Group (optional)" hint="Adds the group's account access on top of any direct grants">
          <select value={f.groupId} onChange={(e) => set("groupId", e.target.value)}>
            <option value="">No group</option>
            {groups.map((g) => <option key={g.id} value={g.id}>{g.name} ({g.level})</option>)}
          </select>
        </Field>
      </div>
      {f.role !== "admin" && (
        <Field label="Account access" error={errs.accountIds}
          hint={isAdmin ? "Leave empty for no direct account grants. Ctrl/Cmd-click to select several." : "Required — you can only grant accounts within your own access."}>
          <select multiple size={Math.min(6, Math.max(3, accounts.length))} value={f.accountIds}
            onChange={(e) => set("accountIds", Array.from(e.target.selectedOptions, (o) => o.value))}>
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.account_name} ({a.provider || "aws"})</option>)}
          </select>
        </Field>
      )}
      {f.role === "admin" && <Banner tone="warn">Administrators have unrestricted access to every account and every setting.</Banner>}
    </Modal>
  );
}

/* ───────────────────────── Reset-link result ───────────────────────── */
function ResetResult({ result, username, onClose }) {
  const [copied, setCopied] = useState(false);
  return (
    <Modal title="Password reset issued" onClose={onClose} width={480}
      footer={<button className="ac-btn primary" onClick={onClose}>Done</button>}>
      {result.email_sent
        ? <p>A one-time set-password link was emailed to <strong>{username}</strong>. It is valid for {result.expires_in_hours} hours.</p>
        : <>
            <Banner tone="warn">No email was sent (the user has no email, or SMTP isn't configured). This link is shown once — copy it now and share it securely.</Banner>
            <div className="ac-inline">
              <input readOnly value={result.reset_link || ""} onFocus={(e) => e.target.select()} />
              <button className="ac-btn ghost" onClick={async () => { await navigator.clipboard?.writeText(result.reset_link); setCopied(true); }}>{copied ? "Copied" : "Copy"}</button>
            </div>
          </>}
    </Modal>
  );
}

/* ───────────────────────── Drawer ───────────────────────── */
function Section({ title, children, aside }) {
  return (
    <section className="ac-sec">
      <header><h4>{title}</h4>{aside}</header>
      {children}
    </section>
  );
}

function UserDrawer({ userId, onClose, onChanged, groups, accounts, perms, isAdmin, actions }) {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync(() => api.getUserDetail(userId), [userId]);
  const [email, setEmail] = useState(null);
  const [addGroup, setAddGroup] = useState("");
  const [addAcct, setAddAcct] = useState("");
  const p = data?.profile;

  useEffect(() => {
    const esc = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onClose]);

  async function run(fn, ok) {
    try { await fn(); if (ok) toast(ok); await reload(); onChanged(); }
    catch (e) { toast(api.errMsg(e), "err"); }
  }

  const memberOf = new Set((data?.groups || []).map((g) => g.id));
  const grantedAccts = new Set((data?.scope_grants || []).map((s) => s.account_ref_id));
  const eff = data?.effective_scope;

  return (
    <aside className="ac-drawer" role="dialog" aria-label="User details">
      <div className="ac-drawer-head">
        {p ? <><Avatar name={p.username} dim={!p.active} />
          <div className="ac-drawer-title">
            <h3>{p.username}</h3>
            <div className="ac-row-gap"><RoleBadge role={p.role} />{!p.active && <Badge tone="red">DEACTIVATED</Badge>}</div>
          </div></> : <div className="ac-drawer-title"><h3>User</h3></div>}
        <button className="ac-x" onClick={onClose} aria-label="Close"><XIcon size={16} /></button>
      </div>

      <div className="ac-drawer-body">
        {loading && <SkeletonRows n={5} />}
        {error && <Banner tone="err">{api.errMsg(error)}</Banner>}
        {p && <>
          <Section title="Profile">
            <dl className="ac-dl">
              <dt>Email</dt>
              <dd>
                {email === null
                  ? <>{p.email || <span className="muted">Not set</span>} {perms.update && <button className="ac-link" onClick={() => setEmail(p.email || "")}>Edit</button>}</>
                  : <span className="ac-inline">
                      <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@company.com" />
                      <button className="ac-btn primary" onClick={() => run(async () => { await api.updateUser(p.id, { email }); setEmail(null); }, "Email updated")}>Save</button>
                      <button className="ac-btn ghost" onClick={() => setEmail(null)}>Cancel</button>
                    </span>}
              </dd>
              <dt>Created</dt><dd>{fmtDate(p.created_at)}</dd>
              <dt>Last sign-in</dt><dd>{p.last_login_at ? `${timeAgo(p.last_login_at)}` : <span className="muted">Never</span>}</dd>
              {!p.active && <><dt>Deactivated</dt><dd>{fmtDate(p.deactivated_at)}</dd></>}
            </dl>
          </Section>

          {isAdmin && !actions.isSelf(p.id) && (
            <Section title="Base role" aside={<span className="muted small">{ROLE_HELP[p.role]}</span>}>
              <select value={p.role} onChange={(e) => actions.changeRole(p, e.target.value, reload)}>
                {ROLES.map((r) => <option key={r} value={r}>{r[0].toUpperCase() + r.slice(1)}</option>)}
              </select>
            </Section>
          )}

          <Section title="Effective access" aside={<span className="muted small">what this user can actually see</span>}>
            {eff === "FULL_ACCESS"
              ? <Banner tone="info">Unrestricted — administrators see every account.</Banner>
              : (eff || []).length === 0
                ? <p className="muted">No account access. This user can sign in but sees no cloud data.</p>
                : <ul className="ac-list">
                    {eff.map((s, i) => {
                      const acct = accounts.find((a) => a.id === s.account_ref_id);
                      return (
                        <li key={i}>
                          <span>{acct ? acct.account_name : s.account_ref_id ? `Account #${s.account_ref_id}` : `All ${s.cloud || ""} accounts`}
                            {s.regions?.length ? <span className="muted"> · {s.regions.join(", ")}</span> : null}</span>
                          <Badge tone={s.source === "group" ? "purple" : s.source === "binding" ? "yellow" : "teal"}>
                            {s.source === "group" ? `via ${s.group_name} (${s.group_level})` : s.source === "binding" ? `binding: ${s.binding_role}` : "direct"}</Badge>
                        </li>
                      );
                    })}
                  </ul>}
          </Section>

          {perms.update && p.role !== "admin" && (
            <Section title="Direct account grants">
              {(data.scope_grants || []).length === 0 && <p className="muted">None.</p>}
              <ul className="ac-list">
                {(data.scope_grants || []).map((s) => (
                  <li key={s.id}>
                    <span>{s.account_name || (s.account_ref_id ? `Account #${s.account_ref_id}` : `All ${s.cloud} accounts`)}
                      {s.regions?.length ? <span className="muted"> · {s.regions.join(", ")}</span> : null}</span>
                    <button className="ac-link danger" onClick={() => run(() => api.revokeUserAccess(s.id), "Access revoked")}>Revoke</button>
                  </li>
                ))}
              </ul>
              <div className="ac-inline">
                <select value={addAcct} onChange={(e) => setAddAcct(e.target.value)}>
                  <option value="">Add an account…</option>
                  {accounts.filter((a) => !grantedAccts.has(a.id)).map((a) => <option key={a.id} value={a.id}>{a.account_name} ({a.provider || "aws"})</option>)}
                </select>
                <button className="ac-btn ghost" disabled={!addAcct}
                  onClick={() => run(async () => {
                    const a = accounts.find((x) => x.id === Number(addAcct));
                    await api.grantUserAccess(p.id, [{ cloud: a?.provider || "aws", account_ref_id: Number(addAcct) }]);
                    setAddAcct("");
                  }, "Access granted")}>Grant</button>
              </div>
            </Section>
          )}

          <Section title="Groups">
            {(data.groups || []).length === 0 && <p className="muted">Not in any group.</p>}
            <div className="ac-chips">
              {(data.groups || []).map((g) => (
                <span key={g.id} className="ac-chip">{g.name}<em>{g.level}</em>
                  {perms.groupsUpdate && <button aria-label={`Remove from ${g.name}`} onClick={() => run(() => api.removeGroupMember(g.id, p.id), "Removed from group")}>×</button>}
                </span>
              ))}
            </div>
            {perms.groupsUpdate && (
              <div className="ac-inline">
                <select value={addGroup} onChange={(e) => setAddGroup(e.target.value)}>
                  <option value="">Add to group…</option>
                  {groups.filter((g) => !memberOf.has(g.id)).map((g) => <option key={g.id} value={g.id}>{g.name} ({g.level})</option>)}
                </select>
                <button className="ac-btn ghost" disabled={!addGroup}
                  onClick={() => run(async () => { await api.addGroupMembers(Number(addGroup), [p.id]); setAddGroup(""); }, "Added to group")}>Add</button>
              </div>
            )}
          </Section>

          {(data.role_bindings?.length > 0 || data.overrides?.length > 0) && (
            <Section title="Role bindings & overrides" aside={<span className="muted small">bindings control which accounts they see</span>}>
              <ul className="ac-list">
                {data.role_bindings.map((b) => {
                  const ex = expiryLabel(b.expires_at);
                  return <li key={`b${b.id}`}><span>{b.role_name} <span className="muted">@ {b.scope_label}</span></span><Badge tone={ex.tone}>{ex.text}</Badge></li>;
                })}
                {data.overrides.map((o) => (
                  <li key={`o${o.id}`}><span><Badge tone="red">DENY</Badge> <code>{o.permission_code}</code></span><span className="muted small">{o.reason}</span></li>
                ))}
              </ul>
            </Section>
          )}

          {data.last_review && (
            <Section title="Last access review">
              <p><Badge tone={data.last_review.decision === "revoke" ? "red" : "green"}>{data.last_review.decision.toUpperCase()}</Badge>{" "}
                <span className="muted">by {data.last_review.reviewed_by_username || "unknown"} · {timeAgo(data.last_review.reviewed_at)}</span></p>
              {data.last_review.notes && <p className="muted small">{data.last_review.notes}</p>}
            </Section>
          )}
        </>}
      </div>

      {p && !actions.isSelf(p.id) && (
        <div className="ac-drawer-foot">
          {perms.reset && <button className="ac-btn ghost" onClick={() => actions.reset(p)}><KeyIcon size={13} /> Reset password</button>}
          {perms.update && (p.active
            ? <button className="ac-btn ghost" onClick={() => actions.deactivate(p, reload)}>Deactivate</button>
            : <button className="ac-btn ghost" onClick={() => actions.activate(p, reload)}>Reactivate</button>)}
          {perms.delete && <button className="ac-btn danger" onClick={() => actions.remove(p, onClose)}>Delete</button>}
        </div>
      )}
    </aside>
  );
}

/* ───────────────────────── Tab ───────────────────────── */
export default function UsersTab() {
  const { user, hasPermission } = useAuth();
  const toast = useToast();
  const isAdmin = user?.role === "admin";
  const perms = {
    create: hasPermission("users.create"), update: hasPermission("users.update"),
    delete: hasPermission("users.delete"), reset: hasPermission("users.password.reset"),
    groupsUpdate: hasPermission("groups.update"),
  };

  const users = useAsync(api.listUsers, []);
  const groups = useAsync(() => (hasPermission("groups.view") ? api.listGroups() : Promise.resolve([])), []);
  const accounts = useAsync(() => api.listAccounts().then((d) => (Array.isArray(d) ? d : [])).catch(() => []), []);

  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [roleF, setRoleF] = useState("all");
  const [statusF, setStatusF] = useState("all");
  const [showAdd, setShowAdd] = useState(false);
  const [openId, setOpenId] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [resetRes, setResetRes] = useState(null);

  const rows = useMemo(() => {
    const needle = dq.trim().toLowerCase();
    return (users.data || []).filter((u) => {
      if (roleF !== "all" && u.role !== roleF) return false;
      if (statusF === "active" && !u.active) return false;
      if (statusF === "inactive" && u.active) return false;
      if (!needle) return true;
      return u.username.toLowerCase().includes(needle) || (u.email || "").toLowerCase().includes(needle)
        || (u.groups || []).some((g) => g.toLowerCase().includes(needle));
    });
  }, [users.data, dq, roleF, statusF]);

  const counts = useMemo(() => {
    const all = users.data || [];
    return { total: all.length, active: all.filter((u) => u.active).length, admins: all.filter((u) => u.role === "admin" && u.active).length };
  }, [users.data]);

  const actions = {
    isSelf: (id) => id === user?.id,
    changeRole: (u, role, reload) => {
      if (role === u.role) return;
      setConfirm({
        title: "Change base role", confirmLabel: "Change role", danger: role === "admin",
        body: <p>Change <strong>{u.username}</strong> from <strong>{u.role}</strong> to <strong>{role}</strong>? {role === "admin" ? "Administrators can see and change everything." : "Their permissions take effect on their next request."}</p>,
        run: async () => { await api.setUserRole(u.id, role); toast(`${u.username} is now ${role}`); await reload?.(); users.reload(); },
      });
    },
    deactivate: (u, reload) => setConfirm({
      title: `Deactivate ${u.username}?`, confirmLabel: "Deactivate", danger: true,
      body: <p>They are signed out immediately and cannot sign in until reactivated. Their access and history are kept.</p>,
      run: async () => { await api.deactivateUser(u.id); toast(`${u.username} deactivated`); await reload?.(); users.reload(); },
    }),
    activate: async (u, reload) => {
      try { await api.activateUser(u.id); toast(`${u.username} reactivated`); await reload?.(); users.reload(); }
      catch (e) { toast(api.errMsg(e), "err"); }
    },
    reset: (u) => setConfirm({
      title: `Reset password for ${u.username}?`, confirmLabel: "Issue reset link",
      body: <p>This issues a one-time set-password link (valid 24 hours). It is emailed if the user has an address and mail is configured; otherwise you will be shown the link once.</p>,
      run: async () => { const r = await api.resetUserPassword(u.id); setResetRes({ r, username: u.username }); },
    }),
    remove: (u, after) => setConfirm({
      title: `Delete ${u.username}?`, confirmLabel: "Delete user", danger: true, requireText: u.username,
      body: <p>Permanently removes the account, its access grants, group memberships, bindings and overrides. Anything they created is reassigned to you. Prefer <strong>Deactivate</strong> if you may need them back.</p>,
      run: async () => { await api.deleteUser(u.id); toast(`${u.username} deleted`); after?.(); users.reload(); },
    }),
  };

  const canManage = (u) => !u.is_self && (isAdmin || u.role === "viewer");

  return (
    <div>
      <div className="ac-toolbar">
        <div className="ac-search">
          <SearchIcon size={14} />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search by name, email or group" aria-label="Search users" />
        </div>
        <select value={roleF} onChange={(e) => setRoleF(e.target.value)} aria-label="Filter by role">
          <option value="all">All roles</option>
          {ROLES.map((r) => <option key={r} value={r}>{r[0].toUpperCase() + r.slice(1)}</option>)}
        </select>
        <select value={statusF} onChange={(e) => setStatusF(e.target.value)} aria-label="Filter by status">
          <option value="all">Any status</option><option value="active">Active</option><option value="inactive">Deactivated</option>
        </select>
        <span className="ac-count">{users.data ? `${counts.total} users · ${counts.active} active · ${counts.admins} admin${counts.admins === 1 ? "" : "s"}` : ""}</span>
        {perms.create && <button className="ac-btn primary" onClick={() => setShowAdd(true)}><PlusIcon size={14} /> Add user</button>}
      </div>

      {users.error && <Banner tone="err" action={<button className="ac-link" onClick={users.reload}>Retry</button>}>{api.errMsg(users.error)}</Banner>}
      {users.loading && !users.data && <SkeletonRows n={6} />}

      {users.data && (rows.length === 0
        ? <Empty icon={<UsersIcon size={28} />} title={users.data.length ? "No users match your filters" : "No users yet"}
            hint={users.data.length ? "Try clearing the search or filters." : "Add the first user to get started."}
            action={users.data.length ? <button className="ac-btn ghost" onClick={() => { setQ(""); setRoleF("all"); setStatusF("all"); }}>Clear filters</button> : null} />
        : (
          <div className="ac-table-wrap">
            <table className="ac-table">
              <thead><tr><th>User</th><th>Role</th><th>Groups</th><th>Access</th><th>Last sign-in</th><th>Status</th><th aria-label="Actions" /></tr></thead>
              <tbody>
                {rows.map((u) => (
                  <tr key={u.id} className={`${u.active ? "" : "inactive"}${openId === u.id ? " sel" : ""}`} onClick={() => setOpenId(u.id)} tabIndex={0}
                      onKeyDown={(e) => { if (e.key === "Enter") setOpenId(u.id); }}>
                    <td>
                      <div className="ac-usercell">
                        <Avatar name={u.username} dim={!u.active} />
                        <div><strong>{u.username}</strong>{u.is_self && <span className="muted small"> (you)</span>}
                          <div className="muted small mono">{u.email || "no email"}</div></div>
                      </div>
                    </td>
                    <td><RoleBadge role={u.role} /></td>
                    <td>{u.groups.length ? <div className="ac-chips tight">{u.groups.slice(0, 2).map((g) => <span key={g} className="ac-chip sm">{g}</span>)}{u.groups.length > 2 && <span className="muted small">+{u.groups.length - 2}</span>}</div> : <span className="muted">—</span>}</td>
                    <td>{u.role === "admin" ? <Badge tone="orange">All accounts</Badge>
                      : (u.scope_grants || u.groups.length) ? <span className="small">{u.scope_grants} direct{u.groups.length ? ` · ${u.groups.length} via group` : ""}</span> : <span className="muted">None</span>}</td>
                    <td className="muted">{timeAgo(u.last_login_at)}</td>
                    <td>{u.active ? <Badge tone="green">Active</Badge> : <Badge tone="red">Deactivated</Badge>}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <KebabMenu label={`Actions for ${u.username}`} items={[
                        { key: "view", label: "View details", onClick: () => setOpenId(u.id) },
                        canManage(u) && perms.reset && { key: "reset", label: "Reset password", onClick: () => actions.reset(u) },
                        canManage(u) && perms.update && (u.active
                          ? { key: "deact", label: "Deactivate", onClick: () => actions.deactivate(u) }
                          : { key: "act", label: "Reactivate", onClick: () => actions.activate(u) }),
                        canManage(u) && perms.delete && { key: "d1", divider: true },
                        canManage(u) && perms.delete && { key: "del", label: "Delete…", danger: true, onClick: () => actions.remove(u) },
                      ]} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}

      {showAdd && <AddUserModal onClose={() => setShowAdd(false)} onCreated={users.reload}
        groups={groups.data || []} accounts={accounts.data || []} isAdmin={isAdmin} />}
      {openId && <UserDrawer userId={openId} onClose={() => setOpenId(null)} onChanged={users.reload}
        groups={groups.data || []} accounts={accounts.data || []} perms={perms} isAdmin={isAdmin} actions={actions} />}
      {confirm && <ConfirmDialog {...confirm} onConfirm={confirm.run} onClose={() => setConfirm(null)} />}
      {resetRes && <ResetResult result={resetRes.r} username={resetRes.username} onClose={() => setResetRes(null)} />}
    </div>
  );
}
