// monitoring-hub/frontend/src/pages/Alerts.jsx
import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useWebSocket } from "../hooks/useWebSocket";
import "./Alerts.css";

const BASE = "";

// ── Shared AudioContext — created once, reused ─────────────────
let _audioCtx = null;

function getAudioCtx() {
  if (!_audioCtx) {
    _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  return _audioCtx;
}

function unlockAudio() {
  const ctx = getAudioCtx();
  if (ctx.state === "suspended") {
    ctx.resume();
  }
}

function playBeep(severity) {
  try {
    const ctx = getAudioCtx();
    const doPlay = () => {
      const isCrit = severity === "CRITICAL";
      const tones  = isCrit ? [880, 660] : [520];
      tones.forEach((freq, i) => {
        const osc  = ctx.createOscillator();
        const gain = ctx.createGain();
        const comp = ctx.createDynamicsCompressor();
        osc.connect(gain);
        gain.connect(comp);
        comp.connect(ctx.destination);
        osc.type = "square";
        osc.frequency.value = freq;
        const t0 = ctx.currentTime + i * 0.3;
        gain.gain.setValueAtTime(0.001, t0);
        gain.gain.linearRampToValueAtTime(0.9, t0 + 0.015);
        gain.gain.setValueAtTime(0.9, t0 + 0.12);
        gain.gain.exponentialRampToValueAtTime(0.001, t0 + 0.35);
        osc.start(t0);
        osc.stop(t0 + 0.4);
      });
    };
    if (ctx.state === "suspended") {
      ctx.resume().then(doPlay);
    } else {
      doPlay();
    }
  } catch (e) {
    console.warn("Beep failed:", e);
  }
}

// ── AWS console deep-link ──────────────────────────────────────
function awsConsoleUrl(resource, region = "") {
  if (!resource) return null;
  if (resource.startsWith("i-"))
    return `https://${region}.console.aws.amazon.com/ec2/home?region=${region}#Instances:instanceId=${resource}`;
  if (resource.startsWith("vol-"))
    return `https://${region}.console.aws.amazon.com/ec2/home?region=${region}#Volumes:volumeId=${resource}`;
  if (resource.includes("lambda") || resource.startsWith("arn:aws:lambda")) {
    const fn = resource.split(":").pop();
    return `https://${region}.console.aws.amazon.com/lambda/home?region=${region}#/functions/${fn}`;
  }
  if (resource.startsWith("db-") || resource.includes("rds"))
    return `https://${region}.console.aws.amazon.com/rds/home?region=${region}#database:`;
  return null;
}

// ── Internal resource detail route ─────────────────────────────
function detailRoute(resource, accountId = 3) {
  if (!resource) return null;
  if (resource.startsWith("i-"))   return `/accounts/${accountId}/ec2?resource=${resource}`;
  if (resource.startsWith("vol-")) return `/accounts/${accountId}/ebs?resource=${resource}`;
  if (resource.includes("lambda")) return `/accounts/${accountId}/lambda`;
  return null;
}

// ── API helper ─────────────────────────────────────────────────
async function apiFetch(path, method = "GET", body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(`${BASE}${path}`, opts);
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new Error(d.detail || `${res.status}`);
  }
  return res.json();
}

const SEV_ORDER = { CRITICAL: 0, WARNING: 1, INFO: 2 };

// ── Main component ─────────────────────────────────────────────
export default function Alerts() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const role     = (user?.role || "viewer").toLowerCase();
  const canAct   = role === "admin" || role === "editor";

  const [alerts,  setAlerts]  = useState([]);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState(null);
  const [tab,     setTab]     = useState("active");
  const [search,  setSearch]  = useState("");
  const [isNOC,   setIsNOC]   = useState(false);
  const [acting,  setActing]  = useState(null);
  const [soundOn, setSoundOn] = useState(true);

  // IDs already present on page load — never beep for these
  const knownIds = useRef(new Set());

  const { lastMessage } = useWebSocket("alerts");

  // Unlock AudioContext on first user interaction anywhere on page
  useEffect(() => {
    const unlock = () => {
      unlockAudio();
      document.removeEventListener("click", unlock);
    };
    document.addEventListener("click", unlock);
    return () => document.removeEventListener("click", unlock);
  }, []);

  const loadAlerts = useCallback(async () => {
    setError(null);
    try {
      const data = await apiFetch("/api/alerts");
      const arr  = Array.isArray(data) ? data : (data.alerts ?? []);
      const sorted = arr.sort((a, b) =>
        (SEV_ORDER[a.severity?.toUpperCase()] ?? 9) -
        (SEV_ORDER[b.severity?.toUpperCase()] ?? 9)
      );
      // Seed knownIds so existing alerts never trigger beep
      sorted.forEach(a => knownIds.current.add(a.id));
      setAlerts(sorted);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAlerts();
    const t = setInterval(loadAlerts, 10000);
    return () => clearInterval(t);
  }, [loadAlerts]);

  // NOC fullscreen mode
  useEffect(() => {
    document.body.classList.toggle("noc-mode", isNOC);
    return () => document.body.classList.remove("noc-mode");
  }, [isNOC]);

  // WebSocket push — beep only for brand-new alerts
  useEffect(() => {
    if (!lastMessage) return;

    if (lastMessage.type === "new_alert") {
      setAlerts(prev => {
        const exists = prev.find(a => a.id === lastMessage.id);
        if (exists) return prev;

        if (soundOn && !knownIds.current.has(lastMessage.id)) {
          playBeep((lastMessage.severity || "").toUpperCase());
        }
        knownIds.current.add(lastMessage.id);

        return [lastMessage, ...prev]
          .slice(0, 200)
          .sort((a, b) =>
            (SEV_ORDER[a.severity?.toUpperCase()] ?? 9) -
            (SEV_ORDER[b.severity?.toUpperCase()] ?? 9)
          );
      });
    }

    if (lastMessage.type === "alert_resolved" && lastMessage.id) {
      setAlerts(prev =>
        prev.map(a => a.id === lastMessage.id ? { ...a, status: "resolved" } : a)
      );
    }

    if (lastMessage.type === "alert_acknowledged" && lastMessage.id) {
      setAlerts(prev =>
        prev.map(a => a.id === lastMessage.id ? { ...a, status: "acknowledged" } : a)
      );
    }
  }, [lastMessage, soundOn]);

  async function handleAck(id) {
    if (!canAct) return;
    setActing(id);
    try {
      await apiFetch(`/api/alerts/${id}/ack`, "PATCH").catch(() =>
        apiFetch(`/api/alerts/${id}/ack`, "POST")
      );
      setAlerts(prev =>
        prev.map(a => a.id === id ? { ...a, status: "acknowledged" } : a)
      );
    } catch (e) {
      alert("Ack failed: " + e.message);
    } finally {
      setActing(null);
    }
  }

  async function handleResolve(id) {
    if (!canAct) return;
    setActing(id);
    try {
      await apiFetch(`/api/alerts/${id}/resolve`, "PATCH").catch(() =>
        apiFetch(`/api/alerts/${id}/resolve`, "POST")
      );
      setAlerts(prev =>
        prev.map(a => a.id === id ? { ...a, status: "resolved" } : a)
      );
    } catch (e) {
      alert("Resolve failed: " + e.message);
    } finally {
      setActing(null);
    }
  }

  const filtered = alerts.filter(a => {
    const s = (a.status || "").toLowerCase();
    if (tab === "active"       && s !== "active")       return false;
    if (tab === "critical"     && (a.severity || "").toUpperCase() !== "CRITICAL") return false;
    if (tab === "acknowledged" && s !== "acknowledged") return false;
    if (tab === "resolved"     && s !== "resolved")     return false;
    if (search) {
      const q = search.toLowerCase();
      return (
        (a.metric_name || "").toLowerCase().includes(q) ||
        (a.resource    || "").toLowerCase().includes(q) ||
        (a.severity    || "").toLowerCase().includes(q)
      );
    }
    return true;
  });

  const counts = {
    all:          alerts.length,
    active:       alerts.filter(a => (a.status || "").toLowerCase() === "active").length,
    critical:     alerts.filter(a => (a.severity || "").toUpperCase() === "CRITICAL").length,
    acknowledged: alerts.filter(a => (a.status || "").toLowerCase() === "acknowledged").length,
    resolved:     alerts.filter(a => (a.status || "").toLowerCase() === "resolved").length,
  };

  return (
    <div className="alerts-page">
      <div className="alerts-header">
        <div>
          <h1>Active <span className="accent">Alerts</span></h1>
          <p className="alerts-sub">Real-time CloudWatch alarm feed across all accounts</p>
        </div>
        <div className="alerts-header-right">
          <button
            className="btn-refresh"
            onClick={() => {
              unlockAudio();
              setSoundOn(v => !v);
            }}
            title={soundOn ? "Mute alert sound" : "Enable alert sound"}
            style={{ fontSize: 14, padding: "6px 10px" }}
          >
            {soundOn ? "🔔" : "🔕"}
          </button>
          <button className="btn-refresh" onClick={loadAlerts}>↻ Refresh</button>
          <button
            className={`btn-refresh${isNOC ? " noc-active-btn" : ""}`}
            onClick={() => setIsNOC(v => !v)}
            title="Toggle NOC fullscreen"
            style={{ fontWeight: 600 }}
          >
            {isNOC ? "⊠ Exit NOC" : "⊞ NOC Mode"}
          </button>
          <div className="live-pill"><span className="live-dot" />LIVE</div>
        </div>
      </div>

      <div className="alerts-tabs">
        {[
          ["all",          "All"],
          ["active",       "Active"],
          ["critical",     "Critical"],
          ["acknowledged", "Acknowledged"],
          ["resolved",     "Resolved"],
        ].map(([key, label]) => (
          <button
            key={key}
            className={`atab ${tab === key ? "atab-active" : ""}`}
            onClick={() => setTab(key)}
          >
            {label}
            <span className={`atab-count ${tab === key ? "atab-count-active" : ""}`}>
              {counts[key]}
            </span>
          </button>
        ))}
        <input
          className="alerts-search"
          placeholder="Search metric, resource…"
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
      </div>

      {loading ? (
        <div className="alerts-loading">Loading alerts…</div>
      ) : error ? (
        <div className="alerts-error">
          ⚠ {error} <button onClick={loadAlerts}>Retry</button>
        </div>
      ) : (
        <div className="alerts-table-wrap">
          <table className="alerts-table">
            <thead>
              <tr>
                <th>SEVERITY</th>
                <th>METRIC</th>
                <th>VALUE / THRESHOLD</th>
                <th>RESOURCE</th>
                <th>STATUS</th>
                <th>TRIGGERED</th>
                <th>CONSOLE</th>
                {canAct && <th>ACTION</th>}
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={canAct ? 8 : 7} className="atbl-empty">
                    No alerts match filter.
                  </td>
                </tr>
              ) : (
                filtered.map((a, idx) => {
                  const sev        = (a.severity || "INFO").toUpperCase();
                  const status     = (a.status   || "active").toLowerCase();
                  const isActing   = acting === a.id;
                  const route      = detailRoute(a.resource, a.account_id);
                  const consoleUrl = awsConsoleUrl(a.resource, a.region);

                  return (
                    <tr key={a.id ?? idx} className={`alert-row sev-row-${sev.toLowerCase()}`}>

                      <td><SevBadge sev={sev} /></td>

                      <td className="alert-metric">
                        <div>{metricLabel(a.metric_name)}</div>
                        <div style={{fontSize:"11px", color:"#888"}}>
                          {(a.service || "").toUpperCase()}
                        </div>
                      </td>

                      <td className="mono small">
                        <span className="alert-val">{fmt(a.current_value)}</span>
                        <span className="alert-sep"> / </span>
                        <span className="alert-thr">{fmt(a.threshold)}</span>
                      </td>

                      <td className="alert-resource">
                        {route ? (
                          <span
                            className="res-deeplink"
                            onClick={e => { e.stopPropagation(); navigate(route); }}
                            title={a.resource}
                          >
                            {a.resource_name || a.resource || "—"}
                          </span>
                        ) : (
                          <span title={a.resource}>
                            {a.resource_name || a.resource || "—"}
                          </span>
                        )}
                        {a.account_name && (
                          <div style={{fontSize:"11px", color:"#888"}}>
                            {a.account_name}
                          </div>
                        )}
                      </td>

                      <td><StatusBadge status={status} /></td>

                      <td className="mono small">
                        {a.triggered_at ? shortDateTime(a.triggered_at) : "—"}
                      </td>

                      <td>
                        <div className="console-links">
                          {route && (
                            <button
                              className="btn-console-detail"
                              onClick={e => { e.stopPropagation(); navigate(route); }}
                              title="Open resource detail with CloudWatch charts"
                            >
                              📊 Metrics
                            </button>
                          )}
                          {consoleUrl && (
                            <a
                              href={consoleUrl}
                              target="_blank"
                              rel="noreferrer"
                              className="btn-console-aws"
                              onClick={e => e.stopPropagation()}
                              title="Open in AWS Management Console"
                            >
                              ☁ Console
                            </a>
                          )}
                        </div>
                      </td>

                      {canAct && (
                        <td>
                          <div className="alert-actions">
                            {status !== "acknowledged" && status !== "resolved" && (
                              <button
                                className="btn-ack"
                                disabled={isActing}
                                onClick={e => { e.stopPropagation(); handleAck(a.id); }}
                              >
                                {isActing ? "…" : "Ack"}
                              </button>
                            )}
                            {status !== "resolved" && (
                              <button
                                className="btn-resolve"
                                disabled={isActing}
                                onClick={e => { e.stopPropagation(); handleResolve(a.id); }}
                              >
                                {isActing ? "…" : "Resolve"}
                              </button>
                            )}
                          </div>
                        </td>
                      )}
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>

          {!canAct && (
            <div style={{ padding: "8px 16px", color: "#666", fontSize: "12px" }}>
              👁 View-only — contact an Admin or Editor to acknowledge/resolve alerts.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Sub-components ─────────────────────────────────────────────

function SevBadge({ sev }) {
  const cls = {
    CRITICAL: "sev-badge sev-critical",
    WARNING:  "sev-badge sev-warning",
    INFO:     "sev-badge sev-info",
  }[sev] || "sev-badge sev-info";
  return <span className={cls}>● {sev}</span>;
}

function StatusBadge({ status }) {
  const cls = {
    active:       "st-badge st-active",
    acknowledged: "st-badge st-ack",
    resolved:     "st-badge st-resolved",
  }[status] || "st-badge st-active";
  return <span className={cls}>{status.toUpperCase()}</span>;
}

function fmt(v) {
  if (v == null) return "—";
  const n = parseFloat(v);
  if (isNaN(n)) return String(v);
  return n % 1 === 0 ? String(n) : n.toFixed(1);
}

const METRIC_LABELS = {
  cpuutilization:    "CPU %",
  networkin:         "Net In",
  networkout:        "Net Out",
  diskreadbytes:     "Disk Read",
  diskwritebytes:    "Disk Write",
  volumequeuelength: "Queue Len",
  burstbalance:      "Burst Bal",
  dbconnections:     "DB Conns",
  freestorage:       "Free Storage",
  readiops:          "Read IOPS",
  writeiops:         "Write IOPS",
  readlatency:       "Read Latency",
  writelatency:      "Write Latency",
  freeablememory:    "Free Mem",
  errors5xx:         "5xx Errors",
  errors4xx:         "4xx Errors",
  responselatency:   "Latency",
  healthyhosts:      "Healthy Hosts",
  unhealthyhosts:    "Unhealthy Hosts",
  requestcount:      "Requests",
  memutilization:    "Mem %",
  invocations:       "Invocations",
  errors:            "Errors",
  duration:          "Duration",
  throttles:         "Throttles",
};

function metricLabel(name) {
  return METRIC_LABELS[(name || "").toLowerCase()] || name;
}

function shortDateTime(iso) {
  try {
    return new Date(iso).toLocaleString("en-US", {
      month:  "numeric",
      day:    "numeric",
      year:   "numeric",
      hour:   "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}
