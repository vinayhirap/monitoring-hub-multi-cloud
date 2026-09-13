// src/pages/OpEvents.jsx
// Roadmap phase 5 (2026-09-13) — structured collector/discovery/alert-eval
// failure log. A debugging surface for operators with operations.view.
//
// Rebuilt from the original Tailwind-utility-class scaffold, which never
// used this app's design tokens at all (raw bg-white/border/text-gray-500
// classes) -- it rendered as a plain white box regardless of the site's
// actual dark/light theme, clashing with every other page. This version
// follows the same card/table/badge language as Compliance.jsx and
// Alerts.jsx so it reads as part of the same product.
import { useState, useEffect, useCallback } from "react";
import { getOpEvents } from "../api/api";
import { useTimezone } from "../contexts/TimezoneContext";
import { RotateCcwIcon, AlertOctagonIcon, AlertTriangleIcon, InfoIcon } from "../components/icons";
import "./OpEvents.css";

const SEVERITY_ICON = { ERROR: AlertOctagonIcon, WARNING: AlertTriangleIcon, INFO: InfoIcon };

function SevBadge({ sev }) {
  const cls = { ERROR: "oe-sev oe-sev-error", WARNING: "oe-sev oe-sev-warning", INFO: "oe-sev oe-sev-info" }[sev] || "oe-sev oe-sev-info";
  return <span className={cls}>● {sev}</span>;
}

function EventRow({ e, ianaName }) {
  const [expanded, setExpanded] = useState(false);
  const Icon = SEVERITY_ICON[e.severity] || InfoIcon;
  const hasDetail = e.detail && Object.keys(e.detail).length > 0;

  return (
    <div className={`oe-row ${expanded ? "oe-row-expanded" : ""}`} onClick={() => hasDetail && setExpanded(v => !v)}>
      <div className="oe-row-main">
        <span className="oe-icon-wrap"><Icon size={14} /></span>
        <div className="oe-body">
          <div className="oe-top-line">
            <span className="oe-type">{e.event_type}</span>
            <SevBadge sev={e.severity} />
            {e.account_name && <span className="oe-account">{e.account_name}</span>}
          </div>
          <div className="oe-message">{e.message}</div>
        </div>
        <div className="oe-time">
          <div className="oe-ts">{new Date(e.created_at).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: ianaName })}</div>
          <div className="oe-date">{new Date(e.created_at).toLocaleDateString("en-GB", { timeZone: ianaName })}</div>
        </div>
        {hasDetail && <span className="oe-expand">{expanded ? "▲" : "▼"}</span>}
      </div>
      {expanded && hasDetail && (
        <pre className="oe-detail">{JSON.stringify(e.detail, null, 2)}</pre>
      )}
    </div>
  );
}

export default function OpEvents() {
  const { ianaName } = useTimezone();
  const [events, setEvents] = useState([]);
  const [severity, setSeverity] = useState("");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastFetch, setLastFetch] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    const params = severity ? { severity } : {};
    getOpEvents(params)
      .then(data => { setEvents(Array.isArray(data) ? data : []); setError(null); setLastFetch(new Date()); })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [severity]);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="opevents-page">
      <div className="c-header">
        <div>
          <h1>Operational <span className="hl">Events</span></h1>
          <p className="sub">Collector, discovery, and alert-evaluation failures — not a full application log</p>
        </div>
        <div className="c-header-actions">
          <select className="oe-sev-select" value={severity} onChange={e => setSeverity(e.target.value)}>
            <option value="">All severities</option>
            <option value="ERROR">Error</option>
            <option value="WARNING">Warning</option>
            <option value="INFO">Info</option>
          </select>
          <button className="c-btn" onClick={load}><RotateCcwIcon size={13} /> Refresh</button>
        </div>
      </div>

      {error && (
        <div className="oe-error">⚠ {error} <button onClick={load}>Retry</button></div>
      )}

      <div className="oe-card">
        <div className="oe-bar">
          <span className="bar-icon">▐</span>
          <span className="bar-title">EVENT LOG</span>
          <span className="bar-count">{events.length} {events.length === 1 ? "entry" : "entries"}</span>
          {lastFetch && (
            <span className="bar-sync">· synced {lastFetch.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: ianaName })}</span>
          )}
        </div>

        {loading ? (
          <div className="oe-empty">Loading events…</div>
        ) : events.length === 0 ? (
          <div className="oe-empty">No events recorded — the collector hasn't logged any failures.</div>
        ) : (
          events.map(e => <EventRow key={e.id} e={e} ianaName={ianaName} />)
        )}
      </div>
    </div>
  );
}
