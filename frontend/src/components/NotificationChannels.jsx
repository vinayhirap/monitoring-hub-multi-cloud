// Alert notification channels (audit C9): where alerts are sent besides the browser.
// Secrets: a Slack/Teams/webhook URL is write-only here. The API only returns the host, so an
// edit leaves the stored URL untouched unless a new one is typed.
import { useCallback, useEffect, useState } from "react";
import { Panel, Badge, ConfirmDialog, EmptyState } from "./ui";
import {
  getNotificationChannels, createNotificationChannel, updateNotificationChannel,
  deleteNotificationChannel, testNotificationChannel, getNotificationLog,
} from "../api/api";

const TYPES = [
  { key: "slack",   label: "Slack",   hint: "Incoming-webhook URL (https://hooks.slack.com/…)" },
  { key: "teams",   label: "Teams",   hint: "Incoming-webhook / Workflow URL (https://…)" },
  { key: "webhook", label: "Webhook", hint: "https URL that receives a JSON POST" },
  { key: "email",   label: "Email",   hint: "One or more addresses, comma-separated" },
];
const BLANK = { id: null, name: "", type: "slack", target: "", min_severity: "CRITICAL", opened: true, escalated: true, enabled: true };
const when = s => { try { return new Date(s).toLocaleString(); } catch { return s || ""; } };

export default function NotificationChannels() {
  const [rows, setRows] = useState(null);
  const [log, setLog] = useState([]);
  const [form, setForm] = useState(null);       // null = closed
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState({});         // per-channel test result
  const [del, setDel] = useState(null);

  const load = useCallback(async () => {
    try {
      const [c, l] = await Promise.all([getNotificationChannels(), getNotificationLog()]);
      setRows(c); setLog(Array.isArray(l) ? l : []);
    } catch (e) { setRows([]); setErr(e.message || "Could not load channels"); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const edit = (r) => setForm({
    id: r.id, name: r.name, type: r.type, target: "", min_severity: r.min_severity,
    opened: r.events.includes("opened"), escalated: r.events.includes("escalated"), enabled: r.enabled,
  });

  async function save() {
    setErr("");
    const events = [form.opened && "opened", form.escalated && "escalated"].filter(Boolean);
    if (!form.name.trim()) return setErr("Give the channel a name");
    if (!events.length) return setErr("Pick at least one event");
    if (!form.id && !form.target.trim()) return setErr("Enter the destination");
    const body = { name: form.name.trim(), min_severity: form.min_severity, events, enabled: form.enabled };
    if (form.target.trim()) body.target = form.target.trim();
    setBusy(true);
    try {
      if (form.id) await updateNotificationChannel(form.id, body);
      else await createNotificationChannel({ ...body, type: form.type });
      setForm(null); await load();
    } catch (e) { setErr(e.message || "Save failed"); }
    finally { setBusy(false); }
  }

  async function test(r) {
    setNote(n => ({ ...n, [r.id]: { text: "Sending…" } }));
    try {
      const res = await testNotificationChannel(r.id);
      setNote(n => ({ ...n, [r.id]: { ok: res.ok, text: res.detail } }));
      load();
    } catch (e) { setNote(n => ({ ...n, [r.id]: { ok: false, text: e.message || "Test failed" } })); }
  }

  async function remove() {
    setBusy(true);
    try { await deleteNotificationChannel(del.id); setDel(null); await load(); }
    catch (e) { setErr(e.message || "Delete failed"); setDel(null); }
    finally { setBusy(false); }
  }

  const typeInfo = TYPES.find(t => t.key === form?.type);

  return (
    <div id="notifications">
      <Panel title="Notification channels"
             subtitle="Send alerts to Slack, Teams, a webhook or a team mailbox. Maintenance-window alerts are never sent."
             actions={!form && <button type="button" className="ui-btn ui-btn-primary" onClick={() => { setErr(""); setForm({ ...BLANK }); }}>Add channel</button>}>
        {err && <div role="alert" className="ui-sub" style={{ color: "var(--crit, #f87171)", marginBottom: 8 }}>{err}</div>}

        {form && (
          <div className="ui-panel-body" style={{ display: "grid", gap: 10, maxWidth: 560, marginBottom: 12 }}>
            <label>Name<input className="ui-input" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="e.g. Ops Slack" /></label>
            <label>Type
              <select className="ui-input" value={form.type} disabled={!!form.id} onChange={e => setForm({ ...form, type: e.target.value })}>
                {TYPES.map(t => <option key={t.key} value={t.key}>{t.label}</option>)}
              </select>
            </label>
            <label>{form.type === "email" ? "Recipients" : "Destination URL"}
              <input className="ui-input" type={form.type === "email" ? "text" : "password"} autoComplete="off"
                     value={form.target} onChange={e => setForm({ ...form, target: e.target.value })}
                     placeholder={form.id ? "Leave blank to keep the saved destination" : typeInfo?.hint} />
            </label>
            <label>Send
              <select className="ui-input" value={form.min_severity} onChange={e => setForm({ ...form, min_severity: e.target.value })}>
                <option value="CRITICAL">Critical alerts only</option>
                <option value="WARNING">Warning and critical alerts</option>
              </select>
            </label>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              <label><input type="checkbox" checked={form.opened} onChange={e => setForm({ ...form, opened: e.target.checked })} /> When an alert opens</label>
              <label><input type="checkbox" checked={form.escalated} onChange={e => setForm({ ...form, escalated: e.target.checked })} /> When an alert is escalated</label>
              <label><input type="checkbox" checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} /> Enabled</label>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button type="button" className="ui-btn ui-btn-primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save"}</button>
              <button type="button" className="ui-btn" disabled={busy} onClick={() => { setForm(null); setErr(""); }}>Cancel</button>
            </div>
          </div>
        )}

        {rows === null ? <div className="ui-sub">Loading…</div>
          : rows.length === 0 && !form ? <EmptyState title="No channels yet" body="Alerts currently reach people only through the dashboard and group emails. Add a channel to send them elsewhere." />
          : rows.map(r => (
            <div key={r.id} style={{ display: "flex", alignItems: "center", gap: 12, padding: "8px 0", borderTop: "1px solid var(--line-1, #1f2937)", flexWrap: "wrap" }}>
              <div style={{ minWidth: 180, flex: 1 }}>
                <strong>{r.name}</strong>{" "}
                <Badge tone={r.enabled ? "ok" : "mute"}>{r.enabled ? "on" : "off"}</Badge>
                <div className="ui-sub">{r.type} · {r.target_preview} · {r.min_severity === "CRITICAL" ? "critical only" : "warning+"} · {r.events.join(", ")}</div>
                {note[r.id] && <div className="ui-sub" style={{ color: note[r.id].ok === false ? "var(--crit, #f87171)" : undefined }}>{note[r.id].text}</div>}
              </div>
              <button type="button" className="ui-btn" onClick={() => test(r)}>Test</button>
              <button type="button" className="ui-btn" onClick={() => { setErr(""); edit(r); }}>Edit</button>
              <button type="button" className="ui-btn" onClick={() => setDel(r)}>Delete</button>
            </div>
          ))}

        {log.length > 0 && (
          <details style={{ marginTop: 12 }}>
            <summary className="ui-sub">Recent deliveries ({log.length})</summary>
            {log.slice(0, 20).map(l => (
              <div key={l.id} className="ui-sub" style={{ padding: "2px 0" }}>
                {when(l.created_at)} · {l.channel_name} · {l.event} · {l.status}{l.detail ? ` · ${l.detail}` : ""}
              </div>
            ))}
          </details>
        )}
      </Panel>

      <ConfirmDialog open={!!del} danger title="Delete this channel?" confirmLabel="Delete" busy={busy}
                     body={<>Alerts will no longer be sent to <strong>{del?.name}</strong>.</>}
                     onConfirm={remove} onCancel={() => setDel(null)} />
    </div>
  );
}
