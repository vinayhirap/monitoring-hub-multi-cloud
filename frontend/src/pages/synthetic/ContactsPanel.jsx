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

  const sw = (checked, label, onChange, disabled) => (
    <label className="syn-toggle" title={label}>
      <input type="checkbox" aria-label={label} checked={checked} disabled={disabled} onChange={onChange} />
      <span className="syn-toggle-track"><span className="syn-toggle-thumb" /></span>
    </label>
  );
  const locked = !canEdit || masked;

  return (
    <div className="syn-contacts" role="region" aria-label={`Alert contacts for ${check.name}`}>
      <div className="syn-edit-head">
        <span className="syn-edit-title">Alert contacts</span>
        <span className="syn-edit-type">{check.name}</span>
      </div>
      <p className="syn-contacts-help">
        Email, per person. Outage emails go out when the check goes down and when it recovers. Certificate emails go out when the
        certificate nears expiry, escalate as it gets closer, and again once it is renewed. Maintenance windows silence them.
      </p>

      {rows === null ? <p className="syn-contacts-help">Loading...</p> : (
        <>
          {rows.length === 0 && <div className="syn-contacts-empty">No one is emailed about this check yet. {canEdit ? "Add an address below." : "Ask an admin to add one."}</div>}
          {rows.length > 0 && (
            <div className="syn-contacts-list" role="table" aria-label="Contacts">
              <div className="syn-contacts-row is-head" role="row"><span>Email</span><span>Label</span><span className="syn-contacts-sw">Outages</span><span className="syn-contacts-sw">Certificate</span><span /></div>
              {rows.map((r, i) => (
                <div className="syn-contacts-row" role="row" key={r.email + i}>
                  <span className="mono">{r.email}</span>
                  {locked ? <span>{r.label || ""}</span>
                    : <input type="text" aria-label={`Label for ${r.email}`} value={r.label || ""} maxLength={100} placeholder="Network team" onChange={e => edit(i, { label: e.target.value })} />}
                  <span className="syn-contacts-sw">{sw(!!r.notify_outage, `Outage emails to ${r.email}`, e => edit(i, { notify_outage: e.target.checked }), locked)}</span>
                  <span className="syn-contacts-sw">{sw(!!r.notify_cert, `Certificate emails to ${r.email}`, e => edit(i, { notify_cert: e.target.checked }), locked)}</span>
                  <span>{!locked && <button type="button" className="syn-btn-delete" title="Remove" aria-label={`Remove ${r.email}`} onClick={() => setRows(l => l.filter((_, j) => j !== i))}><TrashIcon size={13} /></button>}</span>
                </div>))}
            </div>)}
          {masked && rows.length > 0 && <p className="syn-contacts-help">Addresses are partly hidden for your role. Changing contacts needs the synthetic manage permission.</p>}

          {!locked && (
            <div className="syn-contacts-add">
              <input aria-label="New contact email" type="email" value={draft} placeholder="name@company.com" onChange={e => setDraft(e.target.value)}
                     onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); add(); } }} />
              <button type="button" className="syn-btn-add" onClick={add}><PlusIcon size={13} /> Add contact</button>
            </div>)}

          {isHttps && (
            <div className="syn-contacts-cert">
              <div className="syn-contacts-cert-line">
                {sw(custom, "Use custom certificate thresholds", e => setCustom(e.target.checked), !canEdit)}
                <span>{custom ? "Custom certificate thresholds" : `Default thresholds: warn ${defaults.warn} days, critical ${defaults.crit} days before expiry`}</span>
              </div>
              {custom && (
                <div className="syn-contacts-days">
                  <label>Warn at (days)<input type="number" min="2" max="365" value={warn} disabled={!canEdit} onChange={e => setWarn(e.target.value)} /></label>
                  <label>Critical at (days)<input type="number" min="1" max="364" value={crit} disabled={!canEdit} onChange={e => setCrit(e.target.value)} /></label>
                </div>)}
              <div className="syn-contacts-foot">Reminders go to the contacts above only: once a week while in the warning window, daily once critical or expired.</div>
            </div>)}

          <div className="syn-contacts-actions">
            {!locked && <button type="button" className="syn-btn-add" onClick={save} disabled={busy}>{busy ? "Saving..." : "Save"}</button>}
            {!locked && <button type="button" className="syn-btn-cancel" onClick={test} disabled={busy || rows.length === 0}
                                title={rows.length === 0 ? "Add and save a contact first" : "Sends a test email to every saved contact"}>Send test email</button>}
            <button type="button" className="syn-btn-cancel" onClick={onClose}>Close</button>
            {hasPermission("reports.generate") && <Link className="syn-link" to={`/reports?template=synthetic&account=${check.aws_account_id}&check=${check.id}`}>Generate a report for this check</Link>}
          </div>
          {msg && <div className={`syn-error syn-contacts-msg${msg.kind === "ok" ? " is-ok" : ""}`} role={msg.kind === "err" ? "alert" : "status"}>{msg.text}</div>}
        </>)}
    </div>
  );
}
