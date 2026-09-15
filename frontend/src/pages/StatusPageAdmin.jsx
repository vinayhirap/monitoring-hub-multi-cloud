// src/pages/StatusPageAdmin.jsx
// Curates which resources map to which public-facing component name
// on the public status page (see pages/StatusPagePublic.jsx and
// app/api/status_page.py's module docstring for the sanitization
// boundary -- only the NAME entered here is ever shown publicly, real
// resource_ids never leave this authenticated screen).
import { useState, useEffect, useCallback } from "react";
import {
  getStatusPageComponents, createStatusPageComponent, updateStatusPageComponent,
  deleteStatusPageComponent, getAccounts,
} from "../api/api";
import { PlusIcon, TrashIcon, AlertOctagonIcon, GlobeIcon } from "../components/icons";
import "./StatusPageAdmin.css";

const EMPTY_FORM = { aws_account_id: "", name: "", resource_ids: "" };

export default function StatusPageAdmin() {
  const [components, setComponents] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);

  const load = useCallback(() => {
    getStatusPageComponents().then(setComponents).catch(e => setError(e.message));
    getAccounts().then(setAccounts).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  const handleCreate = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const resource_ids = form.resource_ids.split(",").map(s => s.trim()).filter(Boolean);
      if (resource_ids.length === 0) throw new Error("Enter at least one resource ID");
      await createStatusPageComponent({
        aws_account_id: Number(form.aws_account_id), name: form.name, resource_ids,
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
    try { await updateStatusPageComponent(c.id, { enabled: c.enabled ? 0 : 1 }); load(); }
    catch (err) { setError(err.message); }
  };

  const handleDelete = async (id) => {
    try { await deleteStatusPageComponent(id); load(); } catch (err) { setError(err.message); }
  };

  return (
    <div className="spa-page">
      <div className="c-header">
        <div>
          <h1>Status Page <span className="hl">Components</span></h1>
          <p className="sub">Pick a public-facing name for each service and map it to the real resource(s) behind it -- only the name and a live status ever appear on the public page at /status</p>
        </div>
        <a href="/status" target="_blank" rel="noreferrer" className="spa-preview-link">
          <GlobeIcon size={14} /> View public page
        </a>
      </div>

      {error && <div className="spa-error"><AlertOctagonIcon size={13} /> {error}</div>}

      <form className="spa-form" onSubmit={handleCreate}>
        <div className="spa-field">
          <label>Account</label>
          <select value={form.aws_account_id} onChange={e => setForm(f => ({ ...f, aws_account_id: e.target.value }))} required>
            <option value="" disabled>Select account…</option>
            {accounts.map(a => <option key={a.id} value={a.id}>{a.account_name}</option>)}
          </select>
        </div>
        <div className="spa-field">
          <label>Public name</label>
          <input value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                 placeholder="API" required />
        </div>
        <div className="spa-field spa-field-wide">
          <label>Resource IDs (comma-separated)</label>
          <input value={form.resource_ids} onChange={e => setForm(f => ({ ...f, resource_ids: e.target.value }))}
                 placeholder="i-0abc123, sg-0def456" required />
        </div>
        <button type="submit" className="spa-btn-add" disabled={saving}>
          <PlusIcon size={13} /> {saving ? "Adding…" : "Add component"}
        </button>
      </form>

      <div className="spa-card">
        <div className="spa-bar">
          <span className="bar-icon">▐</span>
          <span className="bar-title">COMPONENTS</span>
          <span className="bar-count">{components.length} configured</span>
        </div>

        {components.length === 0 ? (
          <div className="spa-empty">No components configured yet — the public page will show nothing until you add at least one.</div>
        ) : (
          <table className="spa-table">
            <thead>
              <tr>
                <th>Public name</th>
                <th>Account</th>
                <th>Maps to</th>
                <th>Shown publicly</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {components.map(c => (
                <tr key={c.id}>
                  <td>{c.name}</td>
                  <td>{c.account_name}</td>
                  <td className="mono spa-resources">{c.resource_ids.join(", ")}</td>
                  <td>
                    <label className="spa-toggle">
                      <input type="checkbox" checked={!!c.enabled} onChange={() => handleToggleEnabled(c)} />
                      <span className="spa-toggle-track"><span className="spa-toggle-thumb" /></span>
                    </label>
                  </td>
                  <td className="spa-actions">
                    <button className="spa-btn-delete" onClick={() => handleDelete(c.id)} title="Delete component">
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
