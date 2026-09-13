// src/pages/EscalationPolicies.jsx
// Roadmap phase 9 (2026-09-13). CRUD for escalation_policies.
// See app/collector/escalation.py's docstring: escalating here changes
// what the Alerts page shows, it does not send a notification yet
// (SMTP wiring, item #3, is deferred).
//
// Rebuilt from the original Tailwind-utility-class scaffold for the same
// reason as OpEvents.jsx (raw bg-white/border classes ignoring this
// app's actual theme), and to fix two real functional gaps found while
// rebuilding it:
//   1. It called its own local fetch() wrapper instead of api.js's
//      apiFetch, because the five helpers it needed simply didn't exist
//      in api.js yet -- added there now.
//   2. It never exposed the per-account override the backend already
//      supports (escalate_to_group_id is scoped by aws_account_id,
//      NULL = global fallback -- see the uniq_policy_scope constraint in
//      db/migrations/023_escalation_policies.sql) -- the form only ever
//      created global policies, so an account-specific override was
//      unreachable through the UI even though the API accepted it.
import { useState, useEffect, useCallback } from "react";
import {
  getEscalationPolicies, getEscalationGroups, createEscalationPolicy, deleteEscalationPolicy,
  updateEscalationPolicy, getAccounts,
} from "../api/api";
import { PlusIcon, TrashIcon, AlertOctagonIcon } from "../components/icons";
import "./EscalationPolicies.css";

const EMPTY_FORM = { aws_account_id: "", severity: "CRITICAL", ack_sla_minutes: 15, escalate_to_group_id: "" };

function SevBadge({ sev }) {
  return <span className={`ep-sev ${sev === "CRITICAL" ? "ep-sev-critical" : "ep-sev-warning"}`}>● {sev}</span>;
}

export default function EscalationPolicies() {
  const [policies, setPolicies] = useState([]);
  const [groups, setGroups] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);

  const load = useCallback(() => {
    getEscalationPolicies().then(setPolicies).catch(e => setError(e.message));
    getEscalationGroups().then(setGroups).catch(e => setError(e.message));
    getAccounts().then(setAccounts).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  const handleCreate = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await createEscalationPolicy({
        ...form,
        aws_account_id: form.aws_account_id || null,
        ack_sla_minutes: Number(form.ack_sla_minutes),
      });
      setForm(EMPTY_FORM);
      load();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const handleToggleEnabled = async (p) => {
    try {
      await updateEscalationPolicy(p.id, { enabled: p.enabled ? 0 : 1 });
      load();
    } catch (err) { setError(err.message); }
  };

  const handleDelete = async (id) => {
    try {
      await deleteEscalationPolicy(id);
      load();
    } catch (err) { setError(err.message); }
  };

  return (
    <div className="escpol-page">
      <div className="c-header">
        <div>
          <h1>Escalation <span className="hl">Policies</span></h1>
          <p className="sub">An unacked alert past its SLA is reassigned to the target group and logged — no notification is sent yet (SMTP not wired)</p>
        </div>
      </div>

      {error && <div className="ep-error"><AlertOctagonIcon size={13} /> {error}</div>}

      <form className="ep-form" onSubmit={handleCreate}>
        <div className="ep-field">
          <label>Account</label>
          <select value={form.aws_account_id} onChange={e => setForm(f => ({ ...f, aws_account_id: e.target.value }))}>
            <option value="">All accounts (global)</option>
            {accounts.map(a => <option key={a.id} value={a.id}>{a.account_name}</option>)}
          </select>
        </div>
        <div className="ep-field">
          <label>Severity</label>
          <select value={form.severity} onChange={e => setForm(f => ({ ...f, severity: e.target.value }))}>
            <option value="CRITICAL">Critical</option>
            <option value="WARNING">Warning</option>
          </select>
        </div>
        <div className="ep-field ep-field-narrow">
          <label>Unacked for (min)</label>
          <input type="number" min="1" value={form.ack_sla_minutes}
                 onChange={e => setForm(f => ({ ...f, ack_sla_minutes: e.target.value }))} />
        </div>
        <div className="ep-field">
          <label>Escalate to</label>
          <select value={form.escalate_to_group_id} onChange={e => setForm(f => ({ ...f, escalate_to_group_id: e.target.value }))} required>
            <option value="" disabled>Select group…</option>
            {groups.map(g => <option key={g.id} value={g.id}>{g.level} — {g.name}</option>)}
          </select>
        </div>
        <button type="submit" className="ep-btn-add" disabled={saving || !form.escalate_to_group_id}>
          <PlusIcon size={13} /> {saving ? "Adding…" : "Add policy"}
        </button>
      </form>

      <div className="ep-card">
        <div className="ep-bar">
          <span className="bar-icon">▐</span>
          <span className="bar-title">ACTIVE POLICIES</span>
          <span className="bar-count">{policies.length} configured</span>
        </div>

        {policies.length === 0 ? (
          <div className="ep-empty">No escalation policies configured yet — add one above.</div>
        ) : (
          <table className="ep-table">
            <thead>
              <tr>
                <th>Account</th>
                <th>Severity</th>
                <th>SLA</th>
                <th>Escalates to</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {policies.map(p => (
                <tr key={p.id}>
                  <td className="ep-account">{p.account_name || "All accounts (global)"}</td>
                  <td><SevBadge sev={p.severity} /></td>
                  <td className="mono">{p.ack_sla_minutes} min</td>
                  <td>{p.group_name}</td>
                  <td>
                    <label className="ep-toggle">
                      <input type="checkbox" checked={!!p.enabled} onChange={() => handleToggleEnabled(p)} />
                      <span className="ep-toggle-track"><span className="ep-toggle-thumb" /></span>
                    </label>
                  </td>
                  <td className="ep-actions">
                    <button className="ep-btn-delete" onClick={() => handleDelete(p.id)} title="Delete policy">
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
