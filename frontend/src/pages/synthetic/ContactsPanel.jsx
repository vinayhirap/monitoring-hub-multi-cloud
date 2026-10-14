// src/pages/synthetic/ContactsPanel.jsx -- who is emailed when this check goes down or its certificate nears expiry.
// Contacts are per check, so the site owner, the network team and the security team can each get only what they need.
// Everyone with synthetic.view sees the list (addresses masked); synthetic.manage can edit. Reports link: Reports page.
import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { getSyntheticContacts, setSyntheticContacts, testSyntheticContacts, updateSyntheticCheck } from "../../api/api";
import { useAuth } from "../../auth/AuthContext";
import { PlusIcon, TrashIcon } from "../../components/icons";

const errText = (err) => err?.detail || err?.message || "Request failed";
const EMAIL = /^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$/;

export default function ContactsPanel({ check, onClose, onChanged }) {
  const { hasPermission } = useAuth();
  const canEdit = hasPermission("synthetic.manage");
  const isHttps = check.check_type === "https";
  const [rows, setRows] = useState(null);
  const [defaults, setDefaults] = useState({ warn: 30, crit: 7 });
  const [custom, setCustom] = useState(false);
  const [warn, setWarn] = useState("");
  const [crit, setCrit] = useState("");
  const [draft, setDraft] = useState("");
  const [msg, setMsg] = useState(null);                       // {kind: "ok"|"err", text}
  const [busy, setBusy] = useState(false);
  const [readOnly, setReadOnly] = useState(false);       // addresses masked by the server for non-managers

  useEffect(() => {
    let dead = false;
    getSyntheticContacts(check.id).then(r => {
      if (dead) return;
      setRows((r.contacts || []).map(c => ({ ...c })));
      setReadOnly(r.editable === false);
      setDefaults({ warn: r.default_warn_days ?? 30, crit: r.default_crit_days ?? 7 });
      setCustom(!!r.cert_thresholds_custom);
      setWarn(String(r.cert_warn_days ?? 30));
      setCrit(String(r.cert_crit_days ?? 7));
    }).catch(e => { if (!dead) { setRows([]); setMsg({ kind: "err", text: errText(e) }); } });
    return () => { dead = true; };
  }, [check.id]);

  const masked = readOnly;
  const edit = (i, patch) => setRows(l => l.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const add = () => {
    const email = draft.trim();
    if (!EMAIL.test(email)) { setMsg({ kind: "err", text: `"${email}" is not a valid email address.` }); return; }
    if (rows.some(r => (r.email || "").toLowerCase() === email.toLowerCase())) { setMsg({ kind: "err", text: `${email} is already listed.` }); return; }
    setRows(l => [...l, { email, label: "", notify_outage: true, notify_cert: true }]);
    setDraft(""); setMsg(null);
  };

  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      if (isHttps) {
        const body = custom ? { cert_warn_days: Number(warn), cert_crit_days: Number(crit) } : { cert_warn_days: null, cert_crit_days: null };
        await updateSyntheticCheck(check.id, body);
      }
      await setSyntheticContacts(check.id, rows.map(r => ({ email: r.email, label: r.label || null, notify_outage: !!r.notify_outage, notify_cert: !!r.notify_cert })));
      setMsg({ kind: "ok", text: "Saved." });
      onChanged();
    } catch (e) { setMsg({ kind: "err", text: errText(e) }); } finally { setBusy(false); }
  };

  const test = async () => {
    setBusy(true); setMsg(null);
    try {
      const r = await testSyntheticContacts(check.id);
      setMsg({ kind: "ok", text: r?.sent != null ? `Test email sent to ${r.sent} contact(s).` : "Test email sent." });
    } catch (e) {
      setMsg({ kind: "err", text: /501/.test(e.message || "") ? "Email delivery isn't set up yet: an admin must configure SMTP on the server." : errText(e) });
    } finally { setBusy(false); }
  };

  return (
    <div className="syn-contacts" role="region" aria-label={`Alert contacts for ${check.name}`}>
      <div className="syn-contacts-head">
        <b>Who is notified: {check.name}</b>
        <span className="syn-note">Email, per person. Outages are sent when the check goes down and when it recovers; certificate notices when it nears expiry, escalate, and when it is renewed. Maintenance windows silence them.</span>
        <button type="button" className="syn-btn-cancel" onClick={onClose}>Close</button>
      </div>

      {rows === null ? <div className="syn-note">Loading...</div> : (
        <>
          {rows.length === 0 && <div className="syn-warn-note">No one is notified by email about this check. {canEdit ? "Add an address below." : "Ask an admin to add one."}</div>}
          {rows.length > 0 && (
            <table className="syn-table syn-contacts-table">
              <thead><tr><th>Email</th><th>Label</th><th>Outages</th><th>Certificate</th><th></th></tr></thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={r.email + i}>
                    <td className="mono">{r.email}</td>
                    <td>{canEdit ? <input aria-label={`Label for ${r.email}`} value={r.label || ""} maxLength={100} placeholder="Network team" onChange={e => edit(i, { label: e.target.value })} /> : (r.label || "")}</td>
                    <td><input type="checkbox" aria-label={`Outages to ${r.email}`} checked={!!r.notify_outage} disabled={!canEdit || masked} onChange={e => edit(i, { notify_outage: e.target.checked })} /></td>
                    <td><input type="checkbox" aria-label={`Certificate notices to ${r.email}`} checked={!!r.notify_cert} disabled={!canEdit || masked} onChange={e => edit(i, { notify_cert: e.target.checked })} /></td>
                    <td>{canEdit && !masked && <button type="button" className="syn-btn-delete" title="Remove" aria-label={`Remove ${r.email}`} onClick={() => setRows(l => l.filter((_, j) => j !== i))}><TrashIcon size={13} /></button>}</td>
                  </tr>))}
              </tbody>
            </table>)}
          {masked && rows.length > 0 && <div className="syn-note">Addresses are partly hidden for your role; editing needs the synthetic manage permission.</div>}

          {canEdit && !masked && (
            <div className="syn-contacts-add">
              <input aria-label="New contact email" type="email" value={draft} placeholder="name@company.com" onChange={e => setDraft(e.target.value)}
                     onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); add(); } }} />
              <button type="button" className="syn-btn-add" onClick={add}><PlusIcon size={13} /> Add contact</button>
            </div>)}

          {isHttps && (
            <div className="syn-contacts-cert">
              <label className="syn-toggle-line">
                <input type="checkbox" checked={custom} disabled={!canEdit} onChange={e => setCustom(e.target.checked)} />
                <span>Use custom certificate thresholds (default: warn {defaults.warn} days, critical {defaults.crit} days before expiry)</span>
              </label>
              {custom && (
                <div className="syn-contacts-days">
                  <label>Warn at (days) <input type="number" min="2" max="365" value={warn} disabled={!canEdit} onChange={e => setWarn(e.target.value)} /></label>
                  <label>Critical at (days) <input type="number" min="1" max="364" value={crit} disabled={!canEdit} onChange={e => setCrit(e.target.value)} /></label>
                </div>)}
              <div className="syn-note">Reminders: while in the warning window, once a week; once critical or expired, daily. Reminders go to the contacts above only.</div>
            </div>)}

          <div className="syn-contacts-actions">
            {canEdit && !masked && <button type="button" className="syn-btn-add" onClick={save} disabled={busy}>{busy ? "Working..." : "Save"}</button>}
            {canEdit && !masked && <button type="button" className="syn-btn-cancel" onClick={test} disabled={busy || rows.length === 0}>Send test email</button>}
            {hasPermission("reports.generate") && <Link className="syn-link" to={`/reports?template=synthetic&account=${check.aws_account_id}&check=${check.id}`}>Generate a report for this check</Link>}
          </div>
          {msg && <div className={msg.kind === "ok" ? "syn-ok-note" : "syn-error"} role={msg.kind === "err" ? "alert" : "status"}>{msg.text}</div>}
        </>)}
    </div>
  );
}
