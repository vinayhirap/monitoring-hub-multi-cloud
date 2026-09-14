// src/pages/Slos.jsx
// SLO / error-budget tracking. See app/api/slo.py's module docstring:
// turns a wall of alerts into one number ("60% of this month's error
// budget used") instead of "CPU hit 95% for 3 minutes, twice".
//
// Two measurement modes map to one form: link to an existing synthetic
// check (uptime-based), or type a resource_id directly (alert-duration
// based) -- there is no resource-picker API yet, so resource mode asks
// for the same resource_id string visible in the Alerts page's
// Resource column. Exactly one of the two is required, matching
// app/api/slo.py's create_slo() validation.
import { useState, useEffect, useCallback } from "react";
import { getSlos, createSlo, deleteSlo, getAccounts, getSyntheticChecks } from "../api/api";
import { PlusIcon, TrashIcon, AlertOctagonIcon } from "../components/icons";
import "./Slos.css";

const EMPTY_FORM = {
  aws_account_id: "", name: "", mode: "synthetic", synthetic_check_id: "",
  resource_id: "", metric_name: "", target_pct: 99.9, window_days: 30,
};

function StatusBadge({ status }) {
  const labels = { ok: "On track", warning: "At risk", breached: "Breached", no_data: "No data yet" };
  return <span className={`slo-status slo-status-${status}`}>● {labels[status] || status}</span>;
}

function BudgetBar({ pct }) {
  if (pct == null) return <span className="slo-nodata">—</span>;
  const clamped = Math.max(0, Math.min(100, pct));
  const tone = pct >= 100 ? "breached" : pct >= 75 ? "warning" : "ok";
  return (
    <div className="slo-budget-bar" title={`${pct}% of error budget used`}>
      <div className={`slo-budget-fill slo-budget-${tone}`} style={{ width: `${clamped}%` }} />
    </div>
  );
}

export default function Slos() {
  const [slos, setSlos] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [checks, setChecks] = useState([]);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);

  const load = useCallback(() => {
    getSlos().then(setSlos).catch(e => setError(e.message));
    getAccounts().then(setAccounts).catch(() => {});
    getSyntheticChecks().then(setChecks).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  const handleCreate = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await createSlo({
        aws_account_id: Number(form.aws_account_id),
        name: form.name,
        synthetic_check_id: form.mode === "synthetic" ? Number(form.synthetic_check_id) : null,
        resource_id: form.mode === "resource" ? form.resource_id : null,
        metric_name: form.mode === "resource" ? (form.metric_name || null) : null,
        target_pct: Number(form.target_pct),
        window_days: Number(form.window_days),
      });
      setForm(EMPTY_FORM);
      load();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id) => {
    try { await deleteSlo(id); load(); } catch (err) { setError(err.message); }
  };

  return (
    <div className="slo-page">
      <div className="c-header">
        <div>
          <h1>SLOs & <span className="hl">Error Budgets</span></h1>
          <p className="sub">Set a target (e.g. 99.9% over 30 days) and track how much of the allowed "bad time" has been used, instead of reading a wall of individual alerts</p>
        </div>
      </div>

      {error && <div className="slo-error"><AlertOctagonIcon size={13} /> {error}</div>}

      <form className="slo-form" onSubmit={handleCreate}>
        <div className="slo-field">
          <label>Account</label>
          <select value={form.aws_account_id} onChange={e => setForm(f => ({ ...f, aws_account_id: e.target.value }))} required>
            <option value="" disabled>Select account…</option>
            {accounts.map(a => <option key={a.id} value={a.id}>{a.account_name}</option>)}
          </select>
        </div>
        <div className="slo-field">
          <label>Name</label>
          <input value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                 placeholder="Payment API uptime" required />
        </div>
        <div className="slo-field slo-field-narrow">
          <label>Measure via</label>
          <select value={form.mode} onChange={e => setForm(f => ({ ...f, mode: e.target.value }))}>
            <option value="synthetic">Synthetic check</option>
            <option value="resource">Resource alerts</option>
          </select>
        </div>
        {form.mode === "synthetic" ? (
          <div className="slo-field slo-field-wide">
            <label>Synthetic check</label>
            <select value={form.synthetic_check_id} onChange={e => setForm(f => ({ ...f, synthetic_check_id: e.target.value }))} required>
              <option value="" disabled>Select check…</option>
              {checks.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
        ) : (
          <>
            <div className="slo-field slo-field-wide">
              <label>Resource ID</label>
              <input value={form.resource_id} onChange={e => setForm(f => ({ ...f, resource_id: e.target.value }))}
                     placeholder="e.g. i-0abc123 (see Resource column on Alerts)" required />
            </div>
            <div className="slo-field">
              <label>Metric (optional)</label>
              <input value={form.metric_name} onChange={e => setForm(f => ({ ...f, metric_name: e.target.value }))}
                     placeholder="any CRITICAL alert" />
            </div>
          </>
        )}
        <div className="slo-field slo-field-narrow">
          <label>Target %</label>
          <input type="number" step="0.001" min="0" max="100" value={form.target_pct}
                 onChange={e => setForm(f => ({ ...f, target_pct: e.target.value }))} />
        </div>
        <div className="slo-field slo-field-narrow">
          <label>Window (days)</label>
          <input type="number" min="1" value={form.window_days}
                 onChange={e => setForm(f => ({ ...f, window_days: e.target.value }))} />
        </div>
        <button type="submit" className="slo-btn-add" disabled={saving}>
          <PlusIcon size={13} /> {saving ? "Adding…" : "Add SLO"}
        </button>
      </form>

      <div className="slo-card">
        <div className="slo-bar">
          <span className="bar-icon">▐</span>
          <span className="bar-title">TRACKED SLOs</span>
          <span className="bar-count">{slos.length} defined</span>
        </div>

        {slos.length === 0 ? (
          <div className="slo-empty">No SLOs defined yet — add one above to start tracking an error budget.</div>
        ) : (
          <table className="slo-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Target</th>
                <th>Window</th>
                <th>Uptime</th>
                <th>Budget used</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {slos.map(s => (
                <tr key={s.id}>
                  <td>
                    {s.name}
                    <div className="slo-source">{s.synthetic_check_name || s.resource_name || s.resource_id}</div>
                  </td>
                  <td className="mono">{s.target_pct}%</td>
                  <td className="mono">{s.window_days}d</td>
                  <td className="mono">{s.uptime_pct != null ? `${s.uptime_pct}%` : "—"}</td>
                  <td><BudgetBar pct={s.budget_consumed_pct} /></td>
                  <td><StatusBadge status={s.status} /></td>
                  <td className="slo-actions">
                    <button className="slo-btn-delete" onClick={() => handleDelete(s.id)} title="Delete SLO">
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
