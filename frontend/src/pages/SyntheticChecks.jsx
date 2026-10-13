// src/pages/SyntheticChecks.jsx
// Synthetic / uptime (blackbox) monitoring -- active HTTP/HTTPS/TCP/DNS
// probes run from the backend against a configured target, on a
// schedule. See app/collector/synthetic.py's module docstring: a
// failing check becomes a normal alert against an auto-created
// resource, so it flows through incidents/health score/escalation/RCA
// with zero special-casing anywhere else in this app.
//
// Built following the exact same shape as EscalationPolicies.jsx
// (create-form-above-table CRUD) so this reads as "another config
// screen in this app" rather than a bolted-on feature.
import { useState, useEffect, useCallback, useRef } from "react";
import {
  getSyntheticChecks, createSyntheticCheck, updateSyntheticCheck, deleteSyntheticCheck, getAccounts,
} from "../api/api";
import { PlusIcon, TrashIcon, EditIcon, AlertOctagonIcon } from "../components/icons";
import "./SyntheticChecks.css";
import CheckDetail from "./synthetic/CheckDetail";
import { certThresholds, certTone } from "./synthetic/certThresholds";

const EMPTY_FORM = {
  aws_account_id: "", name: "", check_type: "http", target: "",
  interval_seconds: 300, consecutive_failure_threshold: 2,
};

const TYPE_LABEL = { http: "HTTP", https: "HTTPS", tcp: "TCP", dns: "DNS" };

function CertCell({ c }) {
  if (c.check_type !== "https") return <>—</>;
  if (c.last_cert_valid === 0) {
    return <span className="syn-cert-crit" title={c.last_error || "Certificate failed verification"}>invalid</span>;
  }
  if (c.cert_days_left == null) return <span title="No successful TLS handshake recorded yet">—</span>;
  const d = c.cert_days_left;
  const tip = [
    c.cert_subject && `Subject: ${c.cert_subject}`,
    c.cert_issuer && `Issuer: ${c.cert_issuer}`,
    c.cert_not_after && `Expires: ${String(c.cert_not_after).replace("T", " ")} UTC`,
    c.tls_cipher && `Cipher: ${c.tls_cipher}`,
    c.handshake_ms != null && `TLS handshake: ${c.handshake_ms} ms`,
    c.cert_valid != null && `Chain + hostname verified: ${c.cert_valid ? "yes" : "no"}`,
  ].filter(Boolean).join("\n");
  return <span className={certTone(d, certThresholds(c))} title={tip}>{d < 0 ? "expired" : `${d} day${d === 1 ? "" : "s"}`}</span>;
}

// A failed probe does not flip the status until consecutive_failure_threshold probes in a row have failed
// (app/collector/synthetic.py), so in between the stored status is still Up/Unknown. Show that as "Failing n/N".
function StatusBadge({ status, failures, threshold, enabled = true }) {
  const n = Number(failures) || 0, t = Number(threshold) || 0;
  if (enabled && status !== "down" && n > 0 && t > 0) {
    const was = status === "up" ? "Up" : "Unknown";
    return (
      <span className="syn-status syn-status-failing"
            title={`${n} failed probe${n === 1 ? "" : "s"} in a row. Marked Down after ${t} in a row; a successful probe resets the count. Previous state: ${was}.`}>
        ● Failing {Math.min(n, t)}/{t}
      </span>
    );
  }
  const label = status === "up" ? "Up" : status === "down" ? "Down" : "Unknown";
  return <span className={`syn-status syn-status-${status || "unknown"}`}>● {label}</span>;
}

// apiFetch puts the backend's own `detail` on err.detail; err.message is only "API /path -> 400".
const errText = (err) => err?.detail || err?.message || "Request failed";

const intOrNull = (v) => (/^\s*-?\d+\s*$/.test(String(v ?? "")) ? Number(v) : null);

// Editable fields = what PATCH /api/synthetic-checks/{id} accepts (type and account are not editable).
// expected_status_code / expected_keyword only apply to http(s) probes.
function editInitial(c) {
  return {
    name: c.name ?? "", target: c.target ?? "",
    interval_seconds: String(c.interval_seconds ?? ""), timeout_seconds: String(c.timeout_seconds ?? ""),
    consecutive_failure_threshold: String(c.consecutive_failure_threshold ?? ""),
    expected_status_code: c.expected_status_code == null ? "" : String(c.expected_status_code),
    expected_keyword: c.expected_keyword ?? "",
  };
}

function EditCheckForm({ check, onCancel, onSaved }) {
  const [f, setF] = useState(() => editInitial(check));
  const [err, setErr] = useState(null);
  const [saving, setSaving] = useState(false);
  const root = useRef(null);
  useEffect(() => { root.current?.scrollIntoView({ block: "nearest" }); }, []);
  const isWeb = check.check_type === "http" || check.check_type === "https";
  const set = (k) => (e) => setF(x => ({ ...x, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    const start = editInitial(check);
    const body = {};
    // Send only what changed: untouched fields are not re-validated server-side (the target check does DNS).
    for (const k of ["name", "target", "expected_keyword"]) {
      if (f[k] !== start[k]) body[k] = k === "expected_keyword" && f[k] === "" ? null : f[k].trim();
    }
    for (const [k, label] of [["interval_seconds", "Interval"], ["timeout_seconds", "Timeout"], ["consecutive_failure_threshold", "Failure threshold"]]) {
      if (f[k] === start[k]) continue;
      const n = intOrNull(f[k]);
      // the API treats a null here as "use the default", so a blank/invalid number must not be sent
      if (n === null) { setErr(`${label} must be a whole number`); return; }
      body[k] = n;
    }
    if (isWeb && f.expected_status_code !== start.expected_status_code) {
      if (f.expected_status_code.trim() === "") body.expected_status_code = null;
      else {
        const n = intOrNull(f.expected_status_code);
        if (n === null) { setErr("Expected status must be a whole number (or blank for the default, 200)"); return; }
        body.expected_status_code = n;
      }
    }
    if (!Object.keys(body).length) { setErr("No changes to save"); return; }
    setSaving(true); setErr(null);
    try {
      await updateSyntheticCheck(check.id, body);
      onSaved();
    } catch (ex) {
      setErr(errText(ex));
      setSaving(false);
    }
  };

  return (
    <form className="syn-edit" ref={root} onSubmit={submit} aria-label={`Edit check ${check.name}`}>
      <div className="syn-edit-head">
        <span className="syn-edit-title">Edit check</span>
        <span className="syn-edit-type mono" title="The check type cannot be changed; delete and re-create the check to change it">{TYPE_LABEL[check.check_type] || check.check_type}</span>
      </div>
      {err && <div className="syn-error syn-edit-error" role="alert"><AlertOctagonIcon size={13} /> <span>{err}</span></div>}
      <div className="syn-edit-grid">
        <div className="syn-field syn-edit-span2">
          <label>Name</label>
          <input aria-label="Name" value={f.name} onChange={set("name")} maxLength={255} required />
        </div>
        <div className="syn-field syn-edit-full">
          <label>Target</label>
          <input aria-label="Target" value={f.target} onChange={set("target")} maxLength={500} required
                 placeholder={check.check_type === "https" ? "https://api.example.com/health" : check.check_type === "http" ? "http://example.com/health" : check.check_type === "tcp" ? "db.example.com:5432" : "example.com"} />
        </div>
        <div className="syn-field">
          <label>Interval (sec)</label>
          <input aria-label="Interval (sec)" type="number" min="60" max="86400" value={f.interval_seconds} onChange={set("interval_seconds")} required />
        </div>
        <div className="syn-field">
          <label>Timeout (sec)</label>
          <input aria-label="Timeout (sec)" type="number" min="1" max="60" value={f.timeout_seconds} onChange={set("timeout_seconds")} required />
        </div>
        <div className="syn-field">
          <label>Failure threshold</label>
          <input aria-label="Failure threshold" type="number" min="1" max="100" value={f.consecutive_failure_threshold} onChange={set("consecutive_failure_threshold")} required
                 title="Consecutive failed probes before the check is marked Down" />
        </div>
        {isWeb && (
          <>
            <div className="syn-field">
              <label>Expected status</label>
              <input aria-label="Expected status" type="number" min="100" max="599" value={f.expected_status_code} onChange={set("expected_status_code")} placeholder="200 (default)" />
            </div>
            <div className="syn-field syn-edit-span2">
              <label>Expected keyword</label>
              <input aria-label="Expected keyword" value={f.expected_keyword} onChange={set("expected_keyword")} maxLength={255} placeholder="none" />
            </div>
          </>
        )}
      </div>
      <div className="syn-edit-actions">
        <button type="submit" className="syn-btn-add" disabled={saving}>{saving ? "Saving…" : "Save changes"}</button>
        <button type="button" className="syn-btn-cancel" onClick={onCancel} disabled={saving}>Cancel</button>
      </div>
    </form>
  );
}

export default function SyntheticChecks() {
  const [checks, setChecks] = useState([]);
  const [detailId, setDetailId] = useState(null);   // row clicked -> detail drawer
  const [accounts, setAccounts] = useState([]);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [editingId, setEditingId] = useState(null);

  const load = useCallback(() => {
    getSyntheticChecks().then(setChecks).catch(e => setError(e.message));
    getAccounts().then(setAccounts).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  const handleCreate = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await createSyntheticCheck({
        ...form,
        aws_account_id: Number(form.aws_account_id),
        interval_seconds: Number(form.interval_seconds),
        consecutive_failure_threshold: Number(form.consecutive_failure_threshold),
      });
      setForm(EMPTY_FORM);
      load();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const handleToggleEnabled = async (c) => {
    try {
      await updateSyntheticCheck(c.id, { enabled: c.enabled ? 0 : 1 });
      load();
    } catch (err) { setError(err.message); }
  };

  const handleDelete = async (id) => {
    try {
      await deleteSyntheticCheck(id);
      if (id === editingId) setEditingId(null);
      load();
    } catch (err) { setError(err.message); }
  };

  return (
    <div className="syn-page">
      <div className="c-header">
        <div>
          <h1>Synthetic <span className="hl">Checks</span></h1>
          <p className="sub">Active HTTP/HTTPS/TCP/DNS probes run on a schedule -- a failing check becomes a normal alert, correlated and escalated like anything else</p>
        </div>
      </div>

      {error && <div className="syn-error"><AlertOctagonIcon size={13} /> {error}</div>}

      <form className="syn-form" onSubmit={handleCreate}>
        <div className="syn-field">
          <label>Account</label>
          <select aria-label="Account" value={form.aws_account_id} onChange={e => setForm(f => ({ ...f, aws_account_id: e.target.value }))} required>
            <option value="" disabled>Select account…</option>
            {accounts.map(a => <option key={a.id} value={a.id}>{a.account_name}</option>)}
          </select>
        </div>
        <div className="syn-field">
          <label>Name</label>
          <input aria-label="Name" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                 placeholder="Payment API health" required />
        </div>
        <div className="syn-field syn-field-type">
          <label>Type</label>
          <select aria-label="Type" value={form.check_type} onChange={e => setForm(f => ({ ...f, check_type: e.target.value }))}>
            <option value="http">HTTP</option>
            <option value="https">HTTPS (with certificate check)</option>
            <option value="tcp">TCP</option>
            <option value="dns">DNS</option>
          </select>
        </div>
        <div className="syn-field syn-field-wide">
          <label>Target</label>
          <input aria-label="Target" value={form.target} onChange={e => setForm(f => ({ ...f, target: e.target.value }))}
                 placeholder={form.check_type === "https" ? "https://api.example.com/health" : form.check_type === "http" ? "http://example.com/health" : form.check_type === "tcp" ? "db.example.com:5432" : "example.com"} required />
        </div>
        <div className="syn-field syn-field-narrow">
          <label>Interval (sec)</label>
          <input aria-label="Interval (sec)" type="number" min="60" value={form.interval_seconds}
                 onChange={e => setForm(f => ({ ...f, interval_seconds: e.target.value }))} />
        </div>
        <button type="submit" className="syn-btn-add" disabled={saving}>
          <PlusIcon size={13} /> {saving ? "Adding…" : "Add check"}
        </button>
      </form>

      {checks.some(c => c.id === editingId) && (
        <EditCheckForm key={editingId} check={checks.find(c => c.id === editingId)}
                       onCancel={() => setEditingId(null)} onSaved={() => { setEditingId(null); load(); }} />
      )}

      <div className="syn-card">
        <div className="syn-bar">
          <span className="bar-icon">▐</span>
          <span className="bar-title">CONFIGURED CHECKS</span>
          <span className="bar-count">{checks.length} check{checks.length === 1 ? "" : "s"}</span>
        </div>

        {checks.length === 0 ? (
          <div className="syn-empty">No synthetic checks configured yet — add one above to start probing an endpoint.</div>
        ) : (
          <div className="tbl-scroll"><table className="syn-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Target</th>
                <th>Status</th>
                <th>Uptime (24h)</th>
                <th>Cert expires in</th>
                <th>TLS</th>
                <th>Interval</th>
                <th>Enabled</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {checks.map(c => (
                <tr key={c.id} {...CheckDetail.rowProps(c.id, setDetailId)}>
                  <td>{c.name}</td>
                  <td className="mono">
                    {TYPE_LABEL[c.check_type] || c.check_type.toUpperCase()}
                  </td>
                  <td className="syn-target mono">{c.target}</td>
                  <td>
                    <StatusBadge status={c.current_status} failures={c.consecutive_failures} threshold={c.consecutive_failure_threshold} enabled={!!c.enabled} />
                    {c.last_error && (
                      <div className={c.last_ok ? "syn-note" : "syn-err"} title={c.last_error}>{c.last_error}</div>
                    )}
                  </td>
                  <td className="mono">{c.uptime_pct_24h != null ? `${c.uptime_pct_24h}%` : "—"}</td>
                  <td className="mono"><CertCell c={c} /></td>
                  <td className="mono">{c.check_type === "https" ? (c.tls_version || "—") : "—"}</td>
                  <td className="mono">{c.interval_seconds}s</td>
                  <td>
                    <label className="syn-toggle">
                      <input type="checkbox" checked={!!c.enabled} onChange={() => handleToggleEnabled(c)} />
                      <span className="syn-toggle-track"><span className="syn-toggle-thumb" /></span>
                    </label>
                  </td>
                  <td className="syn-actions">
                    <button className="syn-btn-edit" onClick={() => setEditingId(c.id)} title="Edit check" aria-label={`Edit ${c.name}`}>
                      <EditIcon size={13} />
                    </button>
                    <button className="syn-btn-delete" onClick={() => handleDelete(c.id)} title="Delete check">
                      <TrashIcon size={13} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </div>
      <CheckDetail checks={checks} id={detailId} onClose={() => setDetailId(null)} />
    </div>
  );
}
