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
import { useState, useEffect, useCallback } from "react";
import {
  getSyntheticChecks, createSyntheticCheck, updateSyntheticCheck, deleteSyntheticCheck, getAccounts,
} from "../api/api";
import { PlusIcon, TrashIcon, AlertOctagonIcon } from "../components/icons";
import "./SyntheticChecks.css";

const EMPTY_FORM = {
  aws_account_id: "", name: "", check_type: "http", target: "",
  interval_seconds: 300, consecutive_failure_threshold: 2,
  expect_https_redirect: false,
};

const TYPE_LABEL = { http: "HTTP", https: "HTTPS", tcp: "TCP", dns: "DNS" };
// Same thresholds the backend alerts on (app/collector/synthetic.py: WARNING <= 30d, CRITICAL <= 7d).
// Display tone only; the alert itself is raised server-side.
const CERT_WARN_DAYS = 30, CERT_CRIT_DAYS = 7;

function certTone(days) {
  if (days == null) return "";
  if (days <= CERT_CRIT_DAYS) return "syn-cert-crit";
  if (days <= CERT_WARN_DAYS) return "syn-cert-warn";
  return "syn-cert-ok";
}

function CertCell({ c }) {
  if (c.check_type !== "https") return <>—</>;
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
  return <span className={certTone(d)} title={tip}>{d < 0 ? "expired" : `${d} day${d === 1 ? "" : "s"}`}</span>;
}

function StatusBadge({ status }) {
  const label = status === "up" ? "Up" : status === "down" ? "Down" : "Unknown";
  return <span className={`syn-status syn-status-${status || "unknown"}`}>● {label}</span>;
}

export default function SyntheticChecks() {
  const [checks, setChecks] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);

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
        expect_https_redirect: form.check_type === "https" && !!form.expect_https_redirect,
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
        <div className="syn-field syn-field-narrow">
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
        {form.check_type === "https" && (
          <div className="syn-field syn-field-check">
            <label>Redirect</label>
            <label className="syn-checkline" title="Also request the http:// version once per probe and require a redirect to https:// (default port 443 only)">
              <input aria-label="Expect HTTP to HTTPS redirect" type="checkbox" checked={!!form.expect_https_redirect}
                     onChange={e => setForm(f => ({ ...f, expect_https_redirect: e.target.checked }))} />
              Expect HTTP→HTTPS redirect
            </label>
          </div>
        )}
        <button type="submit" className="syn-btn-add" disabled={saving}>
          <PlusIcon size={13} /> {saving ? "Adding…" : "Add check"}
        </button>
      </form>

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
                <tr key={c.id}>
                  <td>{c.name}</td>
                  <td className="mono">
                    {TYPE_LABEL[c.check_type] || c.check_type.toUpperCase()}
                    {!!c.expect_https_redirect && <span className="syn-tag" title="Expects http:// to redirect to https://">↪ redirect</span>}
                  </td>
                  <td className="syn-target mono">{c.target}</td>
                  <td>
                    <StatusBadge status={c.current_status} />
                    {c.last_error && <div className="syn-err" title={c.last_error}>{c.last_error}</div>}
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
    </div>
  );
}
