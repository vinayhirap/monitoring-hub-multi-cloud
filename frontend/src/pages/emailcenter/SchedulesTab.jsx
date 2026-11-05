// Report schedules tab: create / edit / delete / enable-disable, plus "Dry run" and "Run now". A run covers the last full
// calendar period (the backend decides the dates). Without emailcenter.manage the list is read-only and addresses are
// hidden by the API. Polls every 5 s while a schedule is RUNNING.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, ConfirmDialog, DataTable, EmptyState, Panel, Skeleton } from "../../components/ui";
import { useTimezone } from "../../contexts/TimezoneContext";
import { formatStamp } from "../../utils/timeFormat";
import { visibleInterval } from "../../utils/poll";
import { plural } from "../../utils/plural";
import {
  MONTHS, REPORT_TYPE_WORDS, WEEKDAYS, emptyScheduleForm, scheduleFormProblem, scheduleFormToPayload, scheduleToForm,
  scopeLabel, statusTone,
} from "../../utils/emailCenterView";
import { createSchedule, deleteSchedule, errMsg, listSchedules, runSchedule, updateSchedule } from "../../api/emailCenter";
import { getSlos, getSyntheticChecks, listReportScopeResources } from "../../api/api";
import { listHostAgents } from "../../api/hostAgents";

const own = (list, accountId) => (Array.isArray(list) ? list : []).filter(x => String(x.aws_account_id) === String(accountId));

function ScheduleForm({ options, lists, initial, editingId, onSaved, onCancel }) {
  const [f, setF] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [items, setItems] = useState([]);              // hosts / SLOs / synthetic checks / resources of the chosen account
  const set = patch => setF(prev => ({ ...prev, ...patch }));
  const scope = (options?.scopes || []).find(s => s.scope_type === f.scope_type);

  useEffect(() => {
    let live = true;
    setItems([]);
    if (!f.account_id) return undefined;
    const done = fn => r => { if (live) setItems(fn(r)); };
    const none = () => {};
    if (f.scope_type === "HOST") listHostAgents().then(done(r => own(r, f.account_id).map(h => ({ id: String(h.id), label: h.hostname })))).catch(none);
    if (f.scope_type === "SLO") getSlos().then(done(r => own(r, f.account_id).map(x => ({ id: String(x.id), label: x.name })))).catch(none);
    if (f.scope_type === "SYNTHETIC") getSyntheticChecks().then(done(r => own(r, f.account_id).map(c => ({ id: String(c.id), label: c.name })))).catch(none);
    if (f.scope_type === "RESOURCE") listReportScopeResources(f.account_id).then(done(r => (Array.isArray(r) ? r : []).map(x => ({ id: x.resource_id, label: x.name || x.resource_id })))).catch(none);
    return () => { live = false; };
  }, [f.account_id, f.scope_type]);

  const save = async e => {
    e.preventDefault();
    const problem = scheduleFormProblem(f);
    if (problem) { setError(problem); return; }
    setBusy(true); setError("");
    try {
      const payload = scheduleFormToPayload(f);
      if (editingId) await updateSchedule(editingId, payload); else await createSchedule(payload);
      onSaved();
    } catch (err) { setError(errMsg(err)); } finally { setBusy(false); }
  };

  const itemPicker = () => {
    if (f.scope_type === "RESOURCE") {
      return (
        <label>Resource
          {items.length
            ? <select className="ui-input" value={f.resource_id} onChange={e => set({ resource_id: e.target.value })}>
              <option value="">Choose a resource</option>{items.map(i => <option key={i.id} value={i.id}>{i.label}</option>)}</select>
            : <input className="ui-input" value={f.resource_id} placeholder="resource id" onChange={e => set({ resource_id: e.target.value })} />}
        </label>);
    }
    if (["HOST", "SLO", "SYNTHETIC"].includes(f.scope_type)) {
      const word = { HOST: "hosts", SLO: "SLOs", SYNTHETIC: "checks" }[f.scope_type];
      return (
        <label>Which one
          <select className="ui-input" value={f.item_id} disabled={!f.account_id} onChange={e => set({ item_id: e.target.value })}>
            <option value="">{f.account_id ? `All ${word} in this account (${items.length})` : "Choose an account first"}</option>
            {items.map(i => <option key={i.id} value={i.id}>{i.label}</option>)}
          </select>
        </label>);
    }
    return null;
  };

  return (
    <form className="ec-form" onSubmit={save}>
      <label>Name<input className="ui-input" value={f.name} maxLength={120} onChange={e => set({ name: e.target.value })} /></label>
      <div className="ec-grid">
        <label>Report type (also how often it runs)
          <select className="ui-input" value={f.report_type} onChange={e => set({ report_type: e.target.value })}>
            {(options?.report_types || []).map(t => <option key={t} value={t}>{REPORT_TYPE_WORDS[t] || t}</option>)}
          </select>
        </label>
        <label>Scope
          <select className="ui-input" value={f.scope_type} onChange={e => set({ scope_type: e.target.value, item_id: "", resource_id: "" })}>
            {(options?.scopes || []).map(s => <option key={s.scope_type} value={s.scope_type} disabled={!s.allowed && s.scope_type !== f.scope_type}>{s.label}{s.allowed ? "" : " (not allowed for you)"}</option>)}
          </select>
        </label>
        {scope?.needs_account !== false && !["COST", "CLIENT"].includes(f.scope_type) && (
          <label>Account
            <select className="ui-input" value={f.account_id} onChange={e => set({ account_id: e.target.value, item_id: "", resource_id: "" })}>
              <option value="">Choose an account</option>
              {(options?.accounts || []).map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </label>)}
        {f.scope_type === "COST" && (
          <label>Cloud
            <select className="ui-input" value={f.cost_provider} onChange={e => set({ cost_provider: e.target.value })}>
              {(options?.cost_providers || ["all"]).map(c => <option key={c} value={c}>{c === "all" ? "All clouds" : c.toUpperCase()}</option>)}
            </select>
          </label>)}
        {f.scope_type === "CLIENT" && (
          <label>Client name (shown on the cover)
            <input className="ui-input" value={f.client_name} maxLength={80} onChange={e => set({ client_name: e.target.value })} />
          </label>)}
        {itemPicker()}
      </div>
      <div className="ec-grid">
        {f.report_type === "WEEKLY" && (
          <label>Day of week
            <select className="ui-input" value={f.run_weekday} onChange={e => set({ run_weekday: e.target.value })}>
              {(options?.weekdays || WEEKDAYS).map((d, i) => <option key={d} value={i}>{d}</option>)}
            </select>
          </label>)}
        {f.report_type === "YEARLY" && (
          <label>Month (the 12 months before it are reported)
            <select className="ui-input" value={f.run_month} onChange={e => set({ run_month: e.target.value })}>
              {MONTHS.map((m, i) => <option key={m} value={i + 1}>{m}{i === 0 ? " (calendar year)" : i === 3 ? " (April to March)" : ""}</option>)}
            </select>
          </label>)}
        {f.report_type !== "WEEKLY" && (
          <label>Day of month (1 to 28)
            <input className="ui-input" type="number" min={1} max={28} value={f.run_day} onChange={e => set({ run_day: e.target.value })} />
          </label>)}
        <label>Hour
          <input className="ui-input" type="number" min={0} max={23} value={f.run_hour} onChange={e => set({ run_hour: e.target.value })} />
        </label>
        <label>Minute
          <input className="ui-input" type="number" min={0} max={59} value={f.run_minute} onChange={e => set({ run_minute: e.target.value })} />
        </label>
        <label>Timezone
          <select className="ui-input" value={f.timezone} onChange={e => set({ timezone: e.target.value })}>
            {[...new Set([f.timezone, ...(options?.timezones || [])])].map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
      </div>
      <div className="ec-grid">
        <label>Recipient list
          <select className="ui-input" value={f.recipient_list_id} onChange={e => set({ recipient_list_id: e.target.value })}>
            <option value="">None (typed addresses only)</option>
            {(lists || []).map(l => <option key={l.id} value={l.id}>{l.name}</option>)}
          </select>
        </label>
        <label>Extra addresses
          <input className="ui-input" value={f.extra_emails} placeholder="one@example.com, two@example.com" onChange={e => set({ extra_emails: e.target.value })} />
        </label>
      </div>
      <label className="ec-check"><input type="checkbox" checked={f.enabled} onChange={e => set({ enabled: e.target.checked })} /> Enabled</label>
      {error && <div className="ec-error" role="alert">{error}</div>}
      <div className="ec-actions">
        <button type="button" className="ui-btn" onClick={onCancel}>Cancel</button>
        <button className="ui-btn ui-btn-primary" disabled={busy}>{busy ? "Saving…" : editingId ? "Save changes" : "Create schedule"}</button>
      </div>
    </form>
  );
}

function DryRunResult({ r, stamp, onClose }) {
  return (
    <Panel title="Dry run" subtitle="Nothing was generated or sent" actions={<button className="ui-btn" onClick={onClose}>Close</button>}>
      <dl className="ec-facts">
        <dt>When</dt><dd>{r.when}</dd>
        <dt>Period covered</dt><dd>{stamp(r.period_start)} to {stamp(r.period_end)}</dd>
        <dt>Reports enabled</dt><dd>{r.reports_enabled ? "Yes" : "No (REPORTS_ENABLED=false)"}</dd>
        <dt>SMTP configured</dt><dd>{r.smtp_configured ? "Yes" : "No"}</dd>
        <dt>Outcome</dt><dd><b>{r.would}</b></dd>
        <dt>Recipients</dt><dd>{r.recipients.length ? r.recipients.join(", ") : "No one"}</dd>
      </dl>
      {r.skipped.length > 0 && <p className="ec-dim">Skipped: {r.skipped.map(s => `${s.who} (${s.reason})`).join("; ")}</p>}
    </Panel>
  );
}

export default function SchedulesTab({ options, canManage }) {
  const { ianaName, timezone } = useTimezone();
  const stamp = v => formatStamp(v, ianaName, timezone);
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState(null);          // null | "new" | schedule row
  const [dry, setDry] = useState(null);
  const [toDelete, setToDelete] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [confirmRun, setConfirmRun] = useState(null);

  const load = useCallback(() => {
    listSchedules().then(r => { setRows(r); setError(""); }).catch(e => setError(errMsg(e)));
  }, []);
  useEffect(() => { load(); }, [load]);
  const lists = options?.recipient_lists || [];

  const anyRunning = useMemo(() => (rows || []).some(r => r.last_status === "RUNNING"), [rows]);
  useEffect(() => (anyRunning ? visibleInterval(load, 5000) : undefined), [anyRunning, load]);

  const act = async (id, fn, okMsg) => {
    setBusyId(id); setError(""); setNotice("");
    try { const r = await fn(); if (okMsg) setNotice(okMsg); load(); return r; }
    catch (e) { setError(errMsg(e)); return null; } finally { setBusyId(null); }
  };
  const toggle = r => act(r.id, () => updateSchedule(r.id, { enabled: !r.enabled }), r.enabled ? `Paused "${r.name}".` : `Enabled "${r.name}".`);
  const doDry = async r => { const res = await act(r.id, () => runSchedule(r.id, true)); if (res) setDry({ name: r.name, ...res }); };
  const doRun = async r => {
    setConfirmRun(null);
    const res = await act(r.id, () => runSchedule(r.id, false));
    if (res) setNotice(`Started "${r.name}" for ${stamp(res.period_start)} to ${stamp(res.period_end)}. The result appears in the Last result column and the Delivery log.`);
  };
  const remove = async () => { const r = toDelete; setToDelete(null); await act(r.id, () => deleteSchedule(r.id), `Deleted "${r.name}".`); };

  if (editing) {
    return (
      <Panel title={editing === "new" ? "New report schedule" : `Edit "${editing.name}"`}>
        <ScheduleForm options={options} lists={lists} editingId={editing === "new" ? null : editing.id}
          initial={editing === "new" ? emptyScheduleForm(options?.default_timezone) : scheduleToForm(editing, options?.default_timezone)}
          onCancel={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />
      </Panel>);
  }

  const accounts = options?.accounts || [];
  const columns = [
    { key: "name", header: "Schedule", sort: r => r.name, render: r => (
      <div><b>{r.name}</b> {!r.enabled && <Badge tone="mute">Paused</Badge>}
        <div className="ec-dim">{REPORT_TYPE_WORDS[r.report_type]} · {scopeLabel(r, accounts)}</div></div>) },
    { key: "when", header: "Runs", render: r => <span>{r.when}</span> },
    { key: "to", header: "Recipients", render: r => canManage
      ? <span>{r.recipient_list_name || "No list"}{(r.extra_emails || []).length ? ` + ${plural(r.extra_emails.length, "address", "addresses")}` : ""}</span>
      : <span className="ec-dim">Hidden</span> },
    { key: "next", header: "Next run", sort: r => r.next_run_at || "", render: r => r.next_run_at ? stamp(r.next_run_at) : <span className="ec-dim">—</span> },
    { key: "last", header: "Last result", render: r => (
      <div>{r.last_status ? <Badge tone={statusTone(r.last_status)}>{r.last_status}</Badge> : <span className="ec-dim">Never run</span>}
        {r.last_run_at && <div className="ec-dim">{stamp(r.last_run_at)}</div>}
        {r.last_error && <div className="ec-reason" title={r.last_error}>{r.last_error}</div>}</div>) },
  ];
  if (canManage) {
    columns.push({ key: "actions", header: "", render: r => (
      <div className="ec-rowbtns">
        <button className="ui-btn" disabled={busyId === r.id} onClick={() => doDry(r)}>Dry run</button>
        <button className="ui-btn" disabled={busyId === r.id || r.last_status === "RUNNING"} onClick={() => setConfirmRun(r)}>Run now</button>
        <button className="ui-btn" disabled={busyId === r.id} onClick={() => toggle(r)}>{r.enabled ? "Pause" : "Enable"}</button>
        <button className="ui-btn" onClick={() => setEditing(r)}>Edit</button>
        <button className="ui-btn ui-btn-danger" onClick={() => setToDelete(r)}>Delete</button>
      </div>) });
  }

  return (
    <div className="ec-stack">
      <Panel title="Report schedules"
        subtitle="Each run covers the last full calendar period in the schedule's timezone: the previous week, month, quarter, or the 12 months before the chosen month"
        actions={canManage && <button className="ui-btn ui-btn-primary" onClick={() => setEditing("new")}>New schedule</button>}>
        {error && <div className="ec-error" role="alert">{error}</div>}
        {notice && <div className="ec-ok" role="status">{notice}</div>}
        {!rows && !error && <Skeleton h={100} />}
        {rows && (
          <DataTable columns={columns} rows={rows} rowKey={r => r.id} initialSort={{ key: "name", dir: "asc" }}
            empty={<EmptyState title="No schedules yet" body={canManage ? "Create one to email a report on a calendar schedule." : "No schedules are set up for the accounts you can see."} />} />)}
      </Panel>
      {dry && <DryRunResult r={dry} stamp={stamp} onClose={() => setDry(null)} />}
      <ConfirmDialog open={!!confirmRun} title="Run this schedule now?"
        body={confirmRun && `"${confirmRun.name}" will generate its report for the latest full period and email it to everyone on it. This does not use up the scheduled run.`}
        confirmLabel="Run now" onConfirm={() => doRun(confirmRun)} onCancel={() => setConfirmRun(null)} />
      <ConfirmDialog open={!!toDelete} danger title="Delete this schedule?"
        body={toDelete && `"${toDelete.name}" will stop running. Past deliveries stay in the log.`}
        confirmLabel="Delete schedule" onConfirm={remove} onCancel={() => setToDelete(null)} />
    </div>
  );
}
