// src/pages/EscalationPolicies.jsx
// Roadmap phase 9 (2026-09-13). Minimal CRUD — list, create, delete.
// See app/collector/escalation.py's docstring: escalating here changes
// what the Alerts page shows, it does not send a notification yet
// (SMTP wiring, item #3, is deferred).
import { useState, useEffect, useCallback } from "react";

const BASE = "/api/escalation-policies";
async function api(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    ...options, credentials: "include",
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
  return res.json();
}

export default function EscalationPolicies() {
  const [policies, setPolicies] = useState([]);
  const [groups, setGroups] = useState([]);
  const [error, setError] = useState(null);
  const [form, setForm] = useState({ severity: "CRITICAL", ack_sla_minutes: 15, escalate_to_group_id: "" });

  const load = useCallback(() => {
    api("").then(setPolicies).catch((e) => setError(e.message));
    api("/groups").then(setGroups).catch((e) => setError(e.message));
  }, []);
  useEffect(() => { load(); }, [load]);

  const handleCreate = async (e) => {
    e.preventDefault();
    try {
      await api("", { method: "POST", body: JSON.stringify(form) });
      setForm({ severity: "CRITICAL", ack_sla_minutes: 15, escalate_to_group_id: "" });
      load();
    } catch (err) { setError(err.message); }
  };

  const handleDelete = async (id) => {
    await api(`/${id}`, { method: "DELETE" });
    load();
  };

  return (
    <div className="p-6">
      <h1 className="text-xl font-semibold mb-1">Escalation Policies</h1>
      <p className="text-xs text-gray-500 mb-4">
        An unacked alert past its SLA is reassigned to the target group and logged —
        no notification is sent yet (SMTP not wired).
      </p>
      {error && <div className="text-red-600 text-sm mb-3">{error}</div>}

      <form onSubmit={handleCreate} className="flex gap-2 items-center bg-gray-50 border rounded p-3 mb-4 text-sm">
        <select className="border rounded px-2 py-1" value={form.severity}
                onChange={(e) => setForm((f) => ({ ...f, severity: e.target.value }))}>
          <option value="CRITICAL">Critical</option>
          <option value="WARNING">Warning</option>
        </select>
        <span>unacked for</span>
        <input type="number" min="1" className="border rounded px-2 py-1 w-20" value={form.ack_sla_minutes}
               onChange={(e) => setForm((f) => ({ ...f, ack_sla_minutes: e.target.value }))} />
        <span>min → escalate to</span>
        <select className="border rounded px-2 py-1" value={form.escalate_to_group_id}
                onChange={(e) => setForm((f) => ({ ...f, escalate_to_group_id: e.target.value }))}>
          <option value="">Select group…</option>
          {groups.map((g) => <option key={g.id} value={g.id}>{g.level} — {g.name}</option>)}
        </select>
        <button type="submit" className="px-3 py-1 rounded bg-blue-600 text-white">Add</button>
      </form>

      <div className="border rounded bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-gray-500">
            <tr>
              <th className="px-3 py-2">Account</th>
              <th className="px-3 py-2">Severity</th>
              <th className="px-3 py-2">SLA (min)</th>
              <th className="px-3 py-2">Escalates to</th>
              <th className="px-3 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {policies.map((p) => (
              <tr key={p.id} className="border-t">
                <td className="px-3 py-2">{p.account_name || "All accounts (global)"}</td>
                <td className="px-3 py-2">{p.severity}</td>
                <td className="px-3 py-2">{p.ack_sla_minutes}</td>
                <td className="px-3 py-2">{p.group_name}</td>
                <td className="px-3 py-2 text-right">
                  <button className="text-red-500 text-xs" onClick={() => handleDelete(p.id)}>Delete</button>
                </td>
              </tr>
            ))}
            {policies.length === 0 && !error && (
              <tr><td colSpan={5} className="px-3 py-6 text-center text-gray-400">No escalation policies configured.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
