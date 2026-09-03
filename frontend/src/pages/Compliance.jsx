import { useState, useEffect, useCallback } from "react";
import { getAuditLogs } from "../api/api";
import { useAuth } from "../auth/AuthContext";
import "./Compliance.css";
import { useTimezone } from "../contexts/TimezoneContext";
import {
  SearchIcon, BarChartIcon, BuildingIcon, LockIcon, CheckCircleIcon,
  CheckIcon, AlertOctagonIcon, PlusIcon, MinusIcon, UserIcon, TrashIcon,
  SettingsIcon, RefreshCwIcon, SaveIcon, ClipboardIcon, RotateCcwIcon,
  DownloadIcon, AlertTriangleIcon,
} from "../components/icons";

const ACTION_ICONS = {
  "Viewed resource detail":  SearchIcon,
  "Viewed service metrics":  BarChartIcon,
  "Viewed account detail":   BuildingIcon,
  "User login":              LockIcon,
  "Alert acknowledged":      CheckCircleIcon,
  "Alert resolved":          CheckIcon,
  "Alert triggered":         AlertOctagonIcon,
  "Account onboarded":       PlusIcon,
  "Account removed":         MinusIcon,
  "User created":            UserIcon,
  "User deleted":            TrashIcon,
  "Threshold updated":       SettingsIcon,
  "Role changed":            RefreshCwIcon,
  "Settings saved":          SaveIcon,
};

function getIcon(action) {
  if (!action) return ClipboardIcon;
  for (const [key, icon] of Object.entries(ACTION_ICONS)) {
    if (action.toLowerCase().includes(key.toLowerCase())) return icon;
  }
  return ClipboardIcon;
}

function formatUTC(iso) {
  try {
    return new Date(iso).toISOString().replace("T", " ").substring(0, 19) + " UTC";
  } catch { return iso ?? "—"; }
}

function formatDate(iso) {
  try {
    return new Date(iso).toLocaleDateString("en-US", {
      month: "numeric", day: "numeric", year: "numeric"
    });
  } catch { return ""; }
}

function AuditRow({ log }) {
  const [expanded, setExpanded] = useState(false);
  const action = log.action ?? "System action";
  const actor  = log.actor  ?? "System";
  const role   = log.payload?.role ?? "ADMIN";
  const detail = log.payload?.detail ?? "";

  return (
    <div
      className={`audit-row ${expanded ? "expanded" : ""}`}
      onClick={() => setExpanded(x => !x)}
    >
      <div className="ar-time">
        <div className="ar-ts">{formatUTC(log.created_at)}</div>
        <div className="ar-date">{formatDate(log.created_at)}</div>
      </div>
      <div className="ar-icon-wrap">{(() => { const Icon = getIcon(action); return <Icon size={15} />; })()}</div>
      <div className="ar-body">
        <div className="ar-action">{action}</div>
        <div className="ar-detail">
          <span className="ar-actor">{actor}</span>
          {detail && <span className="ar-extra">{detail}</span>}
          <span className={`ar-role ${(role || "").toLowerCase()}`}>{role}</span>
        </div>
        {expanded && log.payload && Object.keys(log.payload).length > 0 && (
          <pre className="ar-payload">{JSON.stringify(log.payload, null, 2)}</pre>
        )}
      </div>
      <span className="ar-expand">{expanded ? "▲" : "▼"}</span>
    </div>
  );
}

export default function Compliance() {
  const { user } = useAuth();
  const isAdmin = (user?.role || "viewer").toLowerCase() === "admin";
  const { ianaName } = useTimezone();
  const [logs,        setLogs]        = useState([]);
  const [loading,     setLoading]     = useState(true);
  const [error,       setError]       = useState(null);
  const [search,      setSearch]      = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [lastFetch,   setLastFetch]   = useState(null);

  const loadLogs = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getAuditLogs(200);
      if (Array.isArray(data)) {
        setLogs(data.map(l => ({
          ...l,
          action:  l.action  ?? l.payload?.action ?? "System action",
          actor:   l.actor   ?? l.payload?.actor  ?? "System",
          payload: typeof l.payload === "string"
            ? (() => { try { return JSON.parse(l.payload); } catch { return {}; } })()
            : (l.payload ?? {}),
        })));
        setLastFetch(new Date());
        setError(null);
      }
    } catch (e) {
      setError("Could not load audit logs. Check backend connection.");
      console.error("Audit log error:", e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadLogs();
  }, [loadLogs]);

  useEffect(() => {
    if (!autoRefresh) return;
    const t = setInterval(loadLogs, 30000);
    return () => clearInterval(t);
  }, [autoRefresh, loadLogs]);

  function exportCSV() {
    const rows = [
      ["Timestamp", "Action", "Actor", "Detail", "Role"],
      ...logs.map(l => [
        l.created_at ?? "",
        l.action ?? "",
        l.actor  ?? "",
        l.payload?.detail ?? "",
        l.payload?.actor_role ?? l.payload?.role ?? (l.actor === "admin" ? "ADMIN" : "SYSTEM"),
      ])
    ];
    const csv = rows
      .map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(","))
      .join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = `audit-log-${Date.now()}.csv`;
    a.click();
  }

  const visible = logs.filter(l => {
    if (!search) return true;
    const q = search.toLowerCase();
    return (
      (l.action  ?? "").toLowerCase().includes(q) ||
      (l.actor   ?? "").toLowerCase().includes(q) ||
      (l.payload?.detail ?? "").toLowerCase().includes(q) ||
      (l.payload?.role   ?? "").toLowerCase().includes(q)
    );
  });

  return (
    <div className="compliance-page">
      <div className="c-header">
        <div>
          <h1>Audit <span className="hl">Log</span></h1>
          <p className="sub">Full activity history for all admin and viewer actions</p>
        </div>
        <div className="c-header-actions">
          <label className="ar-toggle" title="Auto-refresh every 30s">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={e => setAutoRefresh(e.target.checked)}
            />
            <span className="ar-track">
              <span className="ar-thumb" />
            </span>
            <span className="ar-label">Auto-refresh</span>
          </label>
          <button className="c-btn" onClick={loadLogs}><RotateCcwIcon size={13} /> Refresh</button>
          {isAdmin && (
          <button className="c-btn-primary" onClick={exportCSV} disabled={logs.length === 0}>
            <DownloadIcon size={13} /> Export CSV
          </button>
          )}
        </div>
      </div>

      <div className="audit-card">
        <div className="audit-bar">
          <div className="audit-bar-left">
            <span className="bar-icon">▐</span>
            <span className="bar-title">ACTIVITY FEED</span>
            <span className="bar-count">{visible.length} entries</span>
            {lastFetch && (
              <span className="bar-sync">· synced {lastFetch.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: ianaName })}</span>
            )}
          </div>
          <input
            className="c-search"
            placeholder="Search action, actor, detail…"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>

        {loading && (
          <div className="c-loading">
            <span className="c-spin">◌</span> Loading audit logs from database…
          </div>
        )}

        {!loading && error && (
          <div className="c-error">
            <AlertTriangleIcon size={14} /> {error}
            <button onClick={loadLogs} className="c-retry">Retry</button>
          </div>
        )}

        {!loading && !error && (
          <div className="audit-feed">
            {visible.length === 0 ? (
              <div className="c-empty">
                {search
                  ? `No entries match "${search}"`
                  : "No audit log entries yet. Actions performed in the system will appear here automatically."}
              </div>
            ) : (
              visible.map(log => <AuditRow key={log.id} log={log} />)
            )}
          </div>
        )}
      </div>
    </div>
  );
}
