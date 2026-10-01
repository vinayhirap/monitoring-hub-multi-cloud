// src/pages/access/GroupsTab.jsx
import { useMemo, useState } from "react";
import { useAuth } from "../../auth/AuthContext";
import * as api from "../../api/access";
import { PlusIcon, LayersIcon, ChevronDownIcon } from "../../components/icons";
import { Badge, Modal, ConfirmDialog, Field, Banner, Empty, SkeletonRows, Avatar, useToast, useAsync } from "./ui";

const LEVEL_TONE = { L1: "blue", L2: "purple", L3: "orange" };
const LEVEL_HELP = {
  L1: "Top of the hierarchy. Has no parent.",
  L2: "Sits under an L1 group and inherits its access.",
  L3: "Sits under an L2 group and inherits everything above it.",
};

function AddGroupModal({ groups, onClose, onCreated }) {
  const toast = useToast();
  const [f, setF] = useState({ name: "", level: "L1", parent: "", description: "" });
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const parentLevel = f.level === "L2" ? "L1" : f.level === "L3" ? "L2" : null;
  const parents = groups.filter((g) => g.level === parentLevel);

  async function submit() {
    if (!f.name.trim()) return setErr("Name is required");
    if (parentLevel && !f.parent) return setErr(`Choose the ${parentLevel} group this sits under`);
    setBusy(true);
    try {
      await api.createGroup({
        name: f.name.trim(), level: f.level, description: f.description.trim() || undefined,
        parent_group_id: parentLevel ? Number(f.parent) : undefined,
      });
      toast(`Group "${f.name.trim()}" created`);
      onCreated(); onClose();
    } catch (e) { setErr(api.errMsg(e)); setBusy(false); }
  }

  return (
    <Modal title="New group" onClose={onClose} busy={busy}
      footer={<><button className="ac-btn ghost" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="ac-btn primary" onClick={submit} disabled={busy}>{busy ? "Creating…" : "Create group"}</button></>}>
      {err && <Banner tone="err">{err}</Banner>}
      <Field label="Name"><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="e.g. India NOC" /></Field>
      <div className="ac-grid2">
        <Field label="Level" hint={LEVEL_HELP[f.level]}>
          <select value={f.level} onChange={(e) => setF({ ...f, level: e.target.value, parent: "" })}>
            <option value="L1">L1 — top level</option><option value="L2">L2 — under an L1</option><option value="L3">L3 — under an L2</option>
          </select>
        </Field>
        {parentLevel && (
          <Field label={`Parent (${parentLevel})`} hint={parents.length ? undefined : `Create an ${parentLevel} group first`}>
            <select value={f.parent} onChange={(e) => setF({ ...f, parent: e.target.value })}>
              <option value="">Select…</option>
              {parents.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
            </select>
          </Field>
        )}
      </div>
      <Field label="Description (optional)"><input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} placeholder="What this group is for" /></Field>
      <Banner tone="info">Groups organise people and carry account access. A group's level does <strong>not</strong> change its members' role — set that on the user.</Banner>
    </Modal>
  );
}

function AddPolicyForm({ accounts, onAdd }) {
  const [target, setTarget] = useState("");
  const [regions, setRegions] = useState("");
  const clouds = [...new Set(accounts.map((a) => a.provider || "aws"))];
  async function go() {
    const [kind, val] = target.split(":");
    const scope = kind === "acct"
      ? { cloud: accounts.find((a) => a.id === Number(val))?.provider || "aws", account_ref_id: Number(val) }
      : { cloud: val };
    const r = regions.split(",").map((s) => s.trim()).filter(Boolean);
    if (r.length) scope.regions = r;
    await onAdd(scope);
    setTarget(""); setRegions("");
  }
  return (
    <div className="ac-inline wrap">
      <select value={target} onChange={(e) => setTarget(e.target.value)}>
        <option value="">Add access to…</option>
        {clouds.map((c) => <option key={c} value={`cloud:${c}`}>All {c.toUpperCase()} accounts</option>)}
        {accounts.map((a) => <option key={a.id} value={`acct:${a.id}`}>{a.account_name} ({a.provider || "aws"})</option>)}
      </select>
      <input value={regions} onChange={(e) => setRegions(e.target.value)} placeholder="Regions (optional, comma-separated)" />
      <button className="ac-btn ghost" disabled={!target} onClick={go}>Add policy</button>
    </div>
  );
}

function GroupCard({ g, all, accounts, users, canEdit, canDelete, onChanged, onDelete }) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const detail = useAsync(() => (open ? api.getGroup(g.id) : Promise.resolve(null)), [open, g.id]);
  const d = detail.data;
  const [addUser, setAddUser] = useState("");
  const parent = all.find((x) => x.id === g.parent_group_id);
  const memberIds = new Set((d?.members || []).map((m) => m.id));

  async function run(fn, ok) {
    try { await fn(); toast(ok); await detail.reload(); onChanged(); }
    catch (e) { toast(api.errMsg(e), "err"); }
  }

  return (
    <div className={`ac-gcard lvl-${g.level}`}>
      <button className="ac-ghead" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <Badge tone={LEVEL_TONE[g.level]}>{g.level}</Badge>
        <div className="ac-gtitle">
          <strong>{g.name}</strong>
          <span className="muted small">{g.description || (parent ? `Under ${parent.name}` : "Top-level group")}</span>
        </div>
        <ChevronDownIcon size={16} className={open ? "flip" : ""} />
      </button>
      {open && (
        <div className="ac-gbody">
          {detail.loading && !d && <SkeletonRows n={2} />}
          {detail.error && <Banner tone="err">{api.errMsg(detail.error)}</Banner>}
          {d && <>
            {d.chain?.length > 1 && (
              <p className="muted small">Inherits from: {d.chain.filter((c) => c.id !== g.id).map((c) => `${c.name} (${c.level})`).join(" → ")}</p>
            )}
            <h5>Members ({d.members.length})</h5>
            {d.members.length === 0 ? <p className="muted">No members yet.</p> : (
              <div className="ac-chips">
                {d.members.map((m) => (
                  <span key={m.id} className="ac-chip"><Avatar name={m.username} />{m.username}
                    {canEdit && <button aria-label={`Remove ${m.username}`} onClick={() => run(() => api.removeGroupMember(g.id, m.id), `Removed ${m.username}`)}>×</button>}
                  </span>
                ))}
              </div>
            )}
            {canEdit && (
              <div className="ac-inline">
                <select value={addUser} onChange={(e) => setAddUser(e.target.value)}>
                  <option value="">Add member…</option>
                  {users.filter((u) => !memberIds.has(u.id) && u.active).map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
                </select>
                <button className="ac-btn ghost" disabled={!addUser} onClick={() => run(async () => { await api.addGroupMembers(g.id, [Number(addUser)]); setAddUser(""); }, "Member added")}>Add</button>
              </div>
            )}

            <h5>Access policies ({d.own_policies.length})</h5>
            {d.own_policies.length === 0
              ? <p className="muted">No policies attached directly — members only get what parent groups grant.</p>
              : <ul className="ac-list">
                  {d.own_policies.map((p) => {
                    const a = accounts.find((x) => x.id === p.account_ref_id);
                    return (
                      <li key={p.id}>
                        <span>{a ? a.account_name : `All ${(p.cloud || "").toUpperCase()} accounts`}
                          {p.regions?.length ? <span className="muted"> · {p.regions.join(", ")}</span> : null}</span>
                        {canEdit && <button className="ac-link danger" onClick={() => run(() => api.removeGroupPolicy(p.id), "Policy removed")}>Remove</button>}
                      </li>
                    );
                  })}
                </ul>}
            {canEdit && <AddPolicyForm accounts={accounts} onAdd={(scope) => run(() => api.addGroupPolicy(g.id, [scope]), "Policy added")} />}
            {canDelete && <div className="ac-gfoot"><button className="ac-btn danger" onClick={() => onDelete(g)}>Delete group</button></div>}
          </>}
        </div>
      )}
    </div>
  );
}

export default function GroupsTab() {
  const { hasPermission } = useAuth();
  const toast = useToast();
  const canCreate = hasPermission("groups.create");
  const canEdit = hasPermission("groups.update");
  const canDelete = hasPermission("groups.delete");

  const groups = useAsync(api.listGroups, []);
  const users = useAsync(() => api.listUsers().catch(() => []), []);
  const accounts = useAsync(() => api.listAccounts().then((d) => (Array.isArray(d) ? d : [])).catch(() => []), []);
  const [showAdd, setShowAdd] = useState(false);
  const [confirm, setConfirm] = useState(null);

  const tree = useMemo(() => {
    const list = groups.data || [];
    const byParent = {};
    list.forEach((g) => { (byParent[g.parent_group_id || 0] ||= []).push(g); });
    const out = [];
    const walk = (pid, depth) => (byParent[pid] || []).forEach((g) => { out.push({ g, depth }); walk(g.id, depth + 1); });
    walk(0, 0);
    // orphans (parent missing) still get shown
    list.filter((g) => !out.some((o) => o.g.id === g.id)).forEach((g) => out.push({ g, depth: 0 }));
    return out;
  }, [groups.data]);

  function askDelete(g) {
    setConfirm({
      title: `Delete group "${g.name}"?`, confirmLabel: "Delete group", danger: true,
      body: <p>Members lose any access this group granted. Their own direct grants and role are unaffected. A group that still has child groups cannot be deleted.</p>,
      run: async () => { await api.deleteGroup(g.id); toast(`Group "${g.name}" deleted`); groups.reload(); },
    });
  }

  return (
    <div>
      <div className="ac-toolbar">
        <p className="ac-lede">Groups bundle people and account access. Each level inherits everything granted to the groups above it.</p>
        {canCreate && <button className="ac-btn primary" onClick={() => setShowAdd(true)}><PlusIcon size={14} /> New group</button>}
      </div>
      {groups.error && <Banner tone="err" action={<button className="ac-link" onClick={groups.reload}>Retry</button>}>{api.errMsg(groups.error)}</Banner>}
      {groups.loading && !groups.data && <SkeletonRows n={3} />}
      {groups.data && (tree.length === 0
        ? <Empty icon={<LayersIcon size={28} />} title="No groups yet" hint="Create an L1 group to start building your hierarchy."
            action={canCreate ? <button className="ac-btn primary" onClick={() => setShowAdd(true)}>New group</button> : null} />
        : <div className="ac-tree">
            {tree.map(({ g, depth }) => (
              <div key={g.id} style={{ marginLeft: depth * 22 }}>
                <GroupCard g={g} all={groups.data} accounts={accounts.data || []} users={users.data || []}
                  canEdit={canEdit} canDelete={canDelete} onChanged={groups.reload} onDelete={askDelete} />
              </div>
            ))}
          </div>)}
      {showAdd && <AddGroupModal groups={groups.data || []} onClose={() => setShowAdd(false)} onCreated={groups.reload} />}
      {confirm && <ConfirmDialog {...confirm} onConfirm={confirm.run} onClose={() => setConfirm(null)} />}
    </div>
  );
}
