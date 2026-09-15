// src/pages/MaintenanceWindows.jsx
// Maintenance windows with topology-aware silencing. See
// app/collector/maintenance.py's module docstring: declaring a window
// on one resource automatically silences everything that depends on
// it too (walking the real dependency graph), and it un-silences
// itself the moment the window ends -- nothing to remember to turn
// back off.
import { useState, useEffect, useCallback } from "react";
import { getMaintenanceWindows, createMaintenanceWindow, deleteMaintenanceWindow, getAccounts } from "../api/api";
import { PlusIcon, TrashIcon, AlertOctagonIcon, ToolIcon } from "../components/icons";
import "./MaintenanceWindows.css";

function toLocalInputValue(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const now = new Date();
const inOneHour = new Date(now.getTime() + 60 * 60 * 1000);

const EMPTY_FORM = {
  aws_account_id: "", resource_id: "", reason: "",
  starts_at: toLocalInputValue(now), ends_at: toLocalInputValue(inOneHour), silence_downstream: true,
};

export default function MaintenanceWindows() {
  const [windows, setWindows] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);

  const load = useCallback(() => {
    getMaintenanceWindows().then(setWindows).catch(e => setError(e.message));
    getAccounts().then(setAccounts).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  const handleCreate = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await createMaintenanceWindow({
        ...form,
        aws_account_id: Number(form.aws_account_id),
      });
      setForm(f => ({ ...EMPTY_FORM, aws_account_id: f.aws_account_id }));
      load();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id) => {
    try { await deleteMaintenanceWindow(id); load(); } catch (err) { setError(err.message); }
  };

  return (
    <div className="mw-page">
      <div className="c-header">
        <div>
          <h1>Maintenance <span className="hl">Windows</span></h1>
          <p className="sub">Silence a resource (and everything that depends on it) for planned work -- alerts still record, but no page or email fires while a window is active</p>
        </div>
      </div>

      {error && <div className="mw-error"><AlertOctagonIcon size={13} /> {error}</div>}

      <form className="mw-form" onSubmit={handleCreate}>
        <div className="mw-field">
          <label>Account</label>
          <select value={form.aws_account_id} onChange={e => setForm(f => ({ ...f, aws_account_id: e.target.value }))} required>
            <option value="" disabled>Select account…</option>
            {accounts.map(a => <option key={a.id} value={a.id}>{a.account_name}</option>)}
          </select>
        </div>
        <div className="mw-field mw-field-wide">
          <label>Resource ID</label>
          <input value={form.resource_id} onChange={e => setForm(f => ({ ...f, resource_id: e.target.value }))}
                 placeholder="e.g. i-0abc123" required />
        </div>
        <div className="mw-field mw-field-wide">
          <label>Reason</label>
          <input value={form.reason} onChange={e => setForm(f => ({ ...f, reason: e.target.value }))}
                 placeholder="DB patching" required />
        </div>
        <div className="mw-field">
          <label>Starts</label>
          <input type="datetime-local" value={form.starts_at} onChange={e => setForm(f => ({ ...f, starts_at: e.target.value }))} required />
        </div>
        <div className="mw-field">
          <label>Ends</label>
          <input type="datetime-local" value={form.ends_at} onChange={e => setForm(f => ({ ...f, ends_at: e.target.value }))} required />
        </div>
        <label className="mw-checkbox">
          <input type="checkbox" checked={form.silence_downstream}
                 onChange={e => setForm(f => ({ ...f, silence_downstream: e.target.checked }))} />
          Silence dependents too
        </label>
        <button type="submit" className="mw-btn-add" disabled={saving}>
          <PlusIcon size={13} /> {saving ? "Adding…" : "Add window"}
        </button>
      </form>

      <div className="mw-card">
        <div className="mw-bar">
          <span className="bar-icon">▐</span>
          <span className="bar-title">MAINTENANCE WINDOWS</span>
          <span className="bar-count">{windows.length} configured</span>
        </div>

        {windows.length === 0 ? (
          <div className="mw-empty">No maintenance windows scheduled — add one above before starting planned work.</div>
        ) : (
          <table className="mw-table">
            <thead>
              <tr>
                <th>Resource</th>
                <th>Reason</th>
                <th>Window</th>
                <th>Cascade</th>
                <th>State</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {windows.map(w => (
                <tr key={w.id}>
                  <td className="mono">{w.resource_id}</td>
                  <td>{w.reason}</td>
                  <td className="mono mw-window">
                    {new Date(w.starts_at).toLocaleString()} → {new Date(w.ends_at).toLocaleString()}
                  </td>
                  <td>{w.silence_downstream ? "Resource + dependents" : "Resource only"}</td>
                  <td>
                    {w.is_active ? (
                      <span className="mw-state mw-state-active"><ToolIcon size={11} /> Active</span>
                    ) : new Date(w.starts_at) > new Date() ? (
                      <span className="mw-state mw-state-scheduled">Scheduled</span>
                    ) : (
                      <span className="mw-state mw-state-ended">Ended</span>
                    )}
                  </td>
                  <td className="mw-actions">
                    <button className="mw-btn-delete" onClick={() => handleDelete(w.id)} title="Delete window">
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
