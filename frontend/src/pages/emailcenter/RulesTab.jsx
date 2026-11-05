// Alert & incident rules tab: "when one of these events happens at or above this severity (for this account or all of
// them), email this recipient list". Mail is sent by the backend (app/email/rule_dispatch.py) from a background worker, so
// a slow mail server never delays monitoring. emailcenter.view sees the rules (without addresses); emailcenter.manage
// creates, pauses, edits and deletes them. Rules only act while EMAIL_CENTER_ENABLED=true.
import { useCallback, useEffect, useState } from "react";
import { Badge, ConfirmDialog, DataTable, EmptyState, Panel, Skeleton } from "../../components/ui";
import { plural } from "../../utils/plural";
import {
  RULE_EVENTS, RULE_SEVERITIES, emptyRuleForm, ruleAccountLabel, ruleEventLabel, ruleFormProblem, ruleFormToPayload,
  ruleToForm, toggleRuleEvent,
} from "../../utils/emailCenterView";
import { createRule, deleteRule, errMsg, listLists, listRules, updateRule } from "../../api/emailCenter";

function RuleForm({ options, lists, initial, editingId, onSaved, onCancel }) {
  const [f, setF] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const set = patch => setF(prev => ({ ...prev, ...patch }));

  const save = async e => {
    e.preventDefault();
    const problem = ruleFormProblem(f);
    if (problem) { setError(problem); return; }
    setBusy(true); setError("");
    try {
      const payload = ruleFormToPayload(f);
      if (editingId) await updateRule(editingId, payload); else await createRule(payload);
      onSaved();
    } catch (err) { setError(errMsg(err)); } finally { setBusy(false); }
  };

  return (
    <form className="ec-form" onSubmit={save}>
      <label>Name<input className="ui-input" value={f.name} maxLength={120} onChange={e => set({ name: e.target.value })} /></label>
      <fieldset className="ec-pick">
        <legend>Send mail when ({f.events.length} chosen)</legend>
        <div className="ec-checks">
          {RULE_EVENTS.map(ev => (
            <label key={ev.key} className="ec-check">
              <input type="checkbox" checked={f.events.includes(ev.key)} onChange={() => set({ events: toggleRuleEvent(f.events, ev.key) })} />
              {ev.label} <span className="ec-dim">{ev.hint}</span>
            </label>))}
        </div>
      </fieldset>
      <div className="ec-grid">
        <label>Severity
          <select className="ui-input" value={f.min_severity} onChange={e => set({ min_severity: e.target.value })}>
            {RULE_SEVERITIES.map(s => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </label>
        <label>Account
          <select className="ui-input" value={f.account_id} onChange={e => set({ account_id: e.target.value })}>
            <option value="">All accounts</option>
            {(options?.accounts || []).map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </label>
        <label>Recipient list
          <select className="ui-input" value={f.recipient_list_id} onChange={e => set({ recipient_list_id: e.target.value })}>
            <option value="">No list (typed addresses only)</option>
            {lists.map(l => <option key={l.id} value={l.id}>{l.name}</option>)}
          </select>
        </label>
      </div>
      <label>Extra addresses (added to the list)
        <textarea className="ui-input" rows={2} placeholder="one@example.com, two@example.com" value={f.extra_emails} onChange={e => set({ extra_emails: e.target.value })} />
      </label>
      <label className="ec-check">
        <input type="checkbox" checked={f.enabled} onChange={e => set({ enabled: e.target.checked })} /> Rule is enabled
      </label>
      <p className="ec-dim">
        People are mailed once per alert or incident event, however many rules match. Users who may not see the account, and
        anyone already mailed by a notification channel or the escalation group for that alert, are skipped.
      </p>
      {error && <div className="ec-error" role="alert">{error}</div>}
      <div className="ec-actions">
        <button type="button" className="ui-btn" onClick={onCancel}>Cancel</button>
        <button className="ui-btn ui-btn-primary" disabled={busy}>{busy ? "Saving…" : editingId ? "Save changes" : "Create rule"}</button>
      </div>
    </form>
  );
}

export default function RulesTab({ options, canManage }) {
  const [rows, setRows] = useState(null);
  const [lists, setLists] = useState([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState(null);          // null | "new" | rule row
  const [toDelete, setToDelete] = useState(null);
  const [busyId, setBusyId] = useState(null);

  const load = useCallback(() => { listRules().then(r => { setRows(r); setError(""); }).catch(e => setError(errMsg(e))); }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { if (canManage) listLists().then(setLists).catch(() => {}); }, [canManage]);

  const act = async (id, fn, okMsg) => {
    setBusyId(id); setError(""); setNotice("");
    try { await fn(); if (okMsg) setNotice(okMsg); load(); }
    catch (e) { setError(errMsg(e)); } finally { setBusyId(null); }
  };
  const toggle = r => act(r.id, () => updateRule(r.id, { enabled: !r.enabled }), r.enabled ? `Paused "${r.name}".` : `Enabled "${r.name}".`);
  const remove = async () => { const r = toDelete; setToDelete(null); await act(r.id, () => deleteRule(r.id), `Deleted "${r.name}".`); };

  if (editing) {
    return (
      <Panel title={editing === "new" ? "New alert & incident rule" : `Edit "${editing.name}"`}>
        <RuleForm options={options} lists={lists} editingId={editing === "new" ? null : editing.id}
          initial={editing === "new" ? emptyRuleForm() : ruleToForm(editing)}
          onCancel={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />
      </Panel>);
  }

  const accounts = options?.accounts || [];
  const columns = [
    { key: "name", header: "Rule", sort: r => r.name, render: r => (
      <div><b>{r.name}</b> {!r.enabled && <Badge tone="mute">Paused</Badge>}
        <div className="ec-dim">{(r.events || []).map(ruleEventLabel).join(", ")}</div></div>) },
    { key: "sev", header: "Severity", sort: r => r.min_severity, render: r => (
      <Badge tone={r.min_severity === "CRITICAL" ? "crit" : "warn"}>{r.min_severity === "CRITICAL" ? "Critical only" : "Warning and up"}</Badge>) },
    { key: "account", header: "Account", sort: r => ruleAccountLabel(r, accounts), render: r => ruleAccountLabel(r, accounts) },
    { key: "to", header: "Recipients", render: r => canManage
      ? <span>{r.recipient_list_name || "No list"}{(r.extra_emails || []).length ? ` + ${plural(r.extra_emails.length, "address", "addresses")}` : ""}</span>
      : <span className="ec-dim">Hidden</span> },
  ];
  if (canManage) {
    columns.push({ key: "actions", header: "", render: r => (
      <div className="ec-rowbtns">
        <button className="ui-btn" disabled={busyId === r.id} onClick={() => toggle(r)}>{r.enabled ? "Pause" : "Enable"}</button>
        <button className="ui-btn" onClick={() => setEditing(r)}>Edit</button>
        <button className="ui-btn ui-btn-danger" onClick={() => setToDelete(r)}>Delete</button>
      </div>) });
  }

  return (
    <div className="ec-stack">
      <Panel title="Alert & incident rules"
        subtitle="Email a recipient list when an alert or incident is opened, escalated or resolved. Each person gets one mail per event, and mail is sent in the background"
        actions={canManage && <button className="ui-btn ui-btn-primary" onClick={() => setEditing("new")}>New rule</button>}>
        {error && <div className="ec-error" role="alert">{error}</div>}
        {notice && <div className="ec-ok" role="status">{notice}</div>}
        {!rows && !error && <Skeleton h={100} />}
        {rows && (
          <DataTable columns={columns} rows={rows} rowKey={r => r.id} initialSort={{ key: "name", dir: "asc" }}
            empty={<EmptyState title="No rules yet" body={canManage ? "Create one to email people when alerts or incidents change." : "No rules cover the accounts you can see."} />} />)}
      </Panel>
      <Panel title="Good to know" subtitle="What the rules do and do not cover">
        <ul className="ec-notes">
          <li>Sent mail appears in the Delivery log with the category <code>alert_rule</code>; a failed send never affects the alert itself.</li>
          <li>Resolved alerts and incidents are picked up by a check that runs about every 5 minutes, so those mails can arrive a few minutes after the event.</li>
          <li>Alerts from synthetic checks, cost-jump and the host-agent watchdog send opened and escalated rule mail like any other alert. People a check or the cost-jump alert already mails are skipped.</li>
          <li>Cross-metric anomaly alerts send opened rule mail like any other alert, except while the resource is in a maintenance window.</li>
          <li>To avoid floods, the same person gets one mail per resource and event in a 30-minute window and at most 20 mails an hour per rule (server settings can change these).</li>
        </ul>
      </Panel>
      <ConfirmDialog open={!!toDelete} danger title="Delete this rule?"
        body={toDelete && `"${toDelete.name}" will stop sending mail. Past deliveries stay in the log.`}
        confirmLabel="Delete rule" onConfirm={remove} onCancel={() => setToDelete(null)} />
    </div>
  );
}
