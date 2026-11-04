// Recipients tab: named lists of CloudOps users (with an email), org groups and typed addresses, plus a preview of who
// would really receive mail (inactive users, users without an email and, for one account, users who may not see it are
// skipped by the backend). Needs emailcenter.manage.
import { useCallback, useEffect, useState } from "react";
import { Badge, ConfirmDialog, DataTable, EmptyState, Panel, Skeleton } from "../../components/ui";
import { plural } from "../../utils/plural";
import { emptyListForm, listFormProblem, listFormToPayload, listToForm, toggleId } from "../../utils/emailCenterView";
import { createList, deleteList, errMsg, listLists, previewList, updateList } from "../../api/emailCenter";

function ListForm({ options, initial, onSaved, onCancel, editingId }) {
  const [f, setF] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const users = (options?.users || []).filter(u => !filter || `${u.username} ${u.email}`.toLowerCase().includes(filter.toLowerCase()));

  const save = async e => {
    e.preventDefault();
    const problem = listFormProblem(f);
    if (problem) { setError(problem); return; }
    setBusy(true); setError("");
    try {
      const payload = listFormToPayload(f);
      if (editingId) await updateList(editingId, payload); else await createList(payload);
      onSaved();
    } catch (err) { setError(errMsg(err)); } finally { setBusy(false); }
  };

  return (
    <form className="ec-form" onSubmit={save}>
      <label>Name<input className="ui-input" value={f.name} maxLength={120} onChange={e => setF({ ...f, name: e.target.value })} /></label>
      <label>Description<input className="ui-input" value={f.description} maxLength={255} onChange={e => setF({ ...f, description: e.target.value })} /></label>
      <fieldset className="ec-pick">
        <legend>CloudOps users with an email ({f.user_ids.length} chosen)</legend>
        <input className="ui-input" placeholder="Filter users" value={filter} onChange={e => setFilter(e.target.value)} aria-label="Filter users" />
        <div className="ec-checks">
          {users.map(u => (
            <label key={u.id} className="ec-check">
              <input type="checkbox" checked={f.user_ids.includes(u.id)} onChange={() => setF({ ...f, user_ids: toggleId(f.user_ids, u.id) })} />
              {u.username} <span className="ec-dim">{u.email}</span>
            </label>))}
          {!users.length && <span className="ec-dim">No users match.</span>}
        </div>
      </fieldset>
      <fieldset className="ec-pick">
        <legend>Org groups ({f.group_ids.length} chosen): every active member with an email receives it</legend>
        <div className="ec-checks">
          {(options?.groups || []).map(g => (
            <label key={g.id} className="ec-check">
              <input type="checkbox" checked={f.group_ids.includes(g.id)} onChange={() => setF({ ...f, group_ids: toggleId(f.group_ids, g.id) })} />
              {g.name} <span className="ec-dim">{g.level}</span>
            </label>))}
          {!(options?.groups || []).length && <span className="ec-dim">No org groups yet.</span>}
        </div>
      </fieldset>
      <label>Typed addresses (people without a CloudOps user, shared mailboxes)
        <textarea className="ui-input" rows={2} placeholder="one@example.com, two@example.com" value={f.emails} onChange={e => setF({ ...f, emails: e.target.value })} />
      </label>
      {error && <div className="ec-error" role="alert">{error}</div>}
      <div className="ec-actions">
        <button type="button" className="ui-btn" onClick={onCancel}>Cancel</button>
        <button className="ui-btn ui-btn-primary" disabled={busy}>{busy ? "Saving…" : editingId ? "Save changes" : "Create list"}</button>
      </div>
    </form>
  );
}

function Preview({ list, accounts, onClose }) {
  const [accountId, setAccountId] = useState("");
  const key = `${list.id}:${accountId}`;
  const [res, setRes] = useState({ key: "", data: null, error: "" });
  useEffect(() => {
    let live = true;
    previewList(list.id, accountId)
      .then(d => { if (live) setRes({ key, data: d, error: "" }); })
      .catch(e => { if (live) setRes({ key, data: null, error: errMsg(e) }); });
    return () => { live = false; };
  }, [list.id, accountId, key]);
  const current = res.key === key;                 // an older answer is never shown for a newer choice
  const data = current ? res.data : null;
  const error = current ? res.error : "";
  return (
    <Panel title={`Who receives "${list.name}"`} subtitle="Current addresses, read now" actions={<button className="ui-btn" onClick={onClose}>Close</button>}>
      <label className="ec-inline-label">For account
        <select className="ui-input" value={accountId} onChange={e => setAccountId(e.target.value)}>
          <option value="">Any (not account-specific)</option>
          {(accounts || []).map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
      </label>
      {error && <div className="ec-error" role="alert">{error}</div>}
      {!data && !error && <Skeleton h={60} />}
      {data && (
        <>
          <p><b>{plural(data.addresses.length, "address", "addresses")}</b> would get mail:</p>
          {data.addresses.length ? <ul className="ec-addr">{data.addresses.map(a => <li key={a}>{a}</li>)}</ul> : <p className="ec-dim">No one.</p>}
          {data.skipped.length > 0 && (
            <>
              <p><b>{data.skipped.length} skipped:</b></p>
              <ul className="ec-addr">{data.skipped.map((s, i) => <li key={i}>{s.who} <span className="ec-dim">{s.reason}</span></li>)}</ul>
            </>)}
        </>)}
    </Panel>
  );
}

export default function RecipientsTab({ options, onChanged }) {
  const [lists, setLists] = useState(null);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(null);       // null | "new" | list
  const [preview, setPreview] = useState(null);
  const [toDelete, setToDelete] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => { listLists().then(l => { setLists(l); setError(""); }).catch(e => setError(errMsg(e))); }, []);
  useEffect(() => { load(); }, [load]);

  const remove = async () => {
    setBusy(true);
    try { await deleteList(toDelete.id); setToDelete(null); load(); onChanged?.(); }
    catch (e) { setError(errMsg(e)); setToDelete(null); }
    finally { setBusy(false); }
  };

  if (editing) {
    return (
      <Panel title={editing === "new" ? "New recipient list" : `Edit "${editing.name}"`}>
        <ListForm options={options} editingId={editing === "new" ? null : editing.id}
          initial={editing === "new" ? emptyListForm() : listToForm(editing)}
          onCancel={() => setEditing(null)} onSaved={() => { setEditing(null); load(); onChanged?.(); }} />
      </Panel>);
  }

  const columns = [
    { key: "name", header: "List", sort: l => l.name, render: l => <div><b>{l.name}</b>{l.description && <div className="ec-dim">{l.description}</div>}</div> },
    { key: "members", header: "Members", render: l => `${plural(l.user_ids.length, "user")}, ${plural(l.group_ids.length, "group")}, ${plural(l.emails.length, "typed address", "typed addresses")}` },
    { key: "used", header: "Used by", render: l => l.used_by_schedules ? <Badge tone="info">{plural(l.used_by_schedules, "schedule")}</Badge> : <span className="ec-dim">Nothing yet</span> },
    { key: "actions", header: "", render: l => (
      <div className="ec-rowbtns">
        <button className="ui-btn" onClick={() => setPreview(l)}>Preview</button>
        <button className="ui-btn" onClick={() => setEditing(l)}>Edit</button>
        <button className="ui-btn ui-btn-danger" onClick={() => setToDelete(l)}>Delete</button>
      </div>) },
  ];

  return (
    <div className="ec-stack">
      <Panel title="Recipient lists" subtitle="A list is reused by report schedules; a person's current email is read when mail is sent"
        actions={<button className="ui-btn ui-btn-primary" onClick={() => setEditing("new")}>New list</button>}>
        {error && <div className="ec-error" role="alert">{error}</div>}
        {!lists && !error && <Skeleton h={80} />}
        {lists && (
          <DataTable columns={columns} rows={lists} rowKey={l => l.id} initialSort={{ key: "name", dir: "asc" }}
            empty={<EmptyState title="No recipient lists yet" body="Create one, then pick it on a report schedule." />} />)}
      </Panel>
      {preview && <Preview list={preview} accounts={options?.accounts} onClose={() => setPreview(null)} />}
      <ConfirmDialog open={!!toDelete} danger busy={busy} title="Delete this list?"
        body={toDelete && `"${toDelete.name}" will be removed. A list that a schedule still uses cannot be deleted.`}
        confirmLabel="Delete list" onConfirm={remove} onCancel={() => setToDelete(null)} />
    </div>
  );
}
