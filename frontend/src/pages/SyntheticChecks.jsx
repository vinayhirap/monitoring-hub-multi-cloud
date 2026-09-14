// src/pages/SyntheticChecks.jsx
// Synthetic / uptime (blackbox) monitoring -- active HTTP/TCP/DNS
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
};

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
          <p className="sub">Active HTTP/TCP/DNS probes run on a schedule -- a failing check becomes a normal alert, correlated and escalated like anything else</p>
        </div>
      </div>

      {error && <div className="syn-error"><AlertOctagonIcon size={13} /> {error}</div>}

      <form className="syn-form" onSubmit={handleCreate}>
        <div className="syn-field">
          <label>Account</label>
          <select value={form.aws_account_id} onChange={e => setForm(f => ({ ...f, aws_account_id: e.target.value }))} required>
            <option value="" disabled>Select account…</option>
            {accounts.map(a => <option key={a.id} value={a.id}>{a.account_name}</option>)}
          </select>
        </div>
        <div className="syn-field">
          <label>Name</label>
          <input value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                 placeholder="Payment API health" required />
        </div>
        <div className="syn-field syn-field-narrow">
          <label>Type</label>
          <select value={form.check_type} onChange={e => setForm(f => ({ ...f, check_type: e.target.value }))}>
            <option value="http">HTTP</option>
            <option value="tcp">TCP</option>
            <option value="dns">DNS</option>
          </select>
        </div>
        <div className="syn-field syn-field-wide">
          <label>Target</label>
          <input value={form.target} onChange={e => setForm(f => ({ ...f, target: e.target.value }))}
                 placeholder={form.check_type === "http" ? "https://api.example.com/health" : form.check_type === "tcp" ? "db.example.com:5432" : "example.com"} required />
        </div>
        <div className="syn-field syn-field-narrow">
          <label>Interval (sec)</label>
          <input type="number" min="60" value={form.interval_seconds}
                 onChange={e => setForm(f => ({ ...f, interval_seconds: e.target.value }))} />
        </div>
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
          <table className="syn-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Target</th>
                <th>Status</th>
                <th>Uptime (24h)</th>
                <th>Interval</th>
                <th>Enabled</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {checks.map(c => (
                <tr key={c.id}>
                  <td>{c.name}</td>
                  <td className="mono">{c.check_type.toUpperCase()}</td>
                  <td className="syn-target mono">{c.target}</td>
                  <td><StatusBadge status={c.current_status} /></td>
                  <td className="mono">{c.uptime_pct_24h != null ? `${c.uptime_pct_24h}%` : "—"}</td>
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
          </table>
        )}
      </div>
    </div>
  );
}
