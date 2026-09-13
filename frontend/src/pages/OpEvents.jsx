// src/pages/OpEvents.jsx
// Roadmap phase 5 (2026-09-13) — structured collector/discovery/alert-eval
// failure log. Deliberately minimal: a filterable table, no charts. This
// is a debugging surface for operators with operations.view, not a
// dashboard.
import { useState, useEffect, useCallback } from "react";
import { getOpEvents } from "../api/api";

const SEVERITY_COLOR = { ERROR: "text-red-600", WARNING: "text-amber-600", INFO: "text-gray-500" };

export default function OpEvents() {
  const [events, setEvents] = useState([]);
  const [severity, setSeverity] = useState("");
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    const params = severity ? { severity } : {};
    getOpEvents(params).then(setEvents).catch((e) => setError(e.message));
  }, [severity]);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-semibold">Operational Events</h1>
        <select className="border rounded px-2 py-1 text-sm" value={severity}
                onChange={(e) => setSeverity(e.target.value)}>
          <option value="">All severities</option>
          <option value="ERROR">Error</option>
          <option value="WARNING">Warning</option>
          <option value="INFO">Info</option>
        </select>
      </div>
      {error && <div className="text-red-600 text-sm mb-3">{error}</div>}
      <div className="border rounded bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-left text-gray-500">
            <tr>
              <th className="px-3 py-2">Time (UTC)</th>
              <th className="px-3 py-2">Type</th>
              <th className="px-3 py-2">Severity</th>
              <th className="px-3 py-2">Account</th>
              <th className="px-3 py-2">Message</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.id} className="border-t">
                <td className="px-3 py-2 font-mono text-xs whitespace-nowrap">{e.created_at}</td>
                <td className="px-3 py-2 font-mono text-xs">{e.event_type}</td>
                <td className={`px-3 py-2 font-medium ${SEVERITY_COLOR[e.severity] || ""}`}>{e.severity}</td>
                <td className="px-3 py-2">{e.account_name || "—"}</td>
                <td className="px-3 py-2">{e.message}</td>
              </tr>
            ))}
            {events.length === 0 && !error && (
              <tr><td colSpan={5} className="px-3 py-6 text-center text-gray-400">No events recorded.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
