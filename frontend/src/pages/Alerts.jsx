// monitoring-hub/frontend/src/pages/Alerts.jsx
import { useEffect, useState, useCallback, useRef, Fragment } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useWebSocket } from "../hooks/useWebSocket";
import "./Alerts.css";
import { useTimezone, formatInTz } from "../contexts/TimezoneContext";
import { InfoIcon, DownloadIcon } from "../components/icons";
import { rcaReportUrl } from "../api/api";

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
// NOTE: We no longer build a raw console.aws.amazon.com URL on the client.
// A plain URL like that has no account context — clicking it just opens
// whatever AWS account the browser is already signed into, which is why
// the button used to land on the WRONG account. Instead we ask the backend
// for an account-locked sign-in link scoped to THIS alert's account
// (see openConsole / GET /alerts/{id}/console-url).
function hasConsoleTarget(resource) {
  // Previously guessed AWS resource-ID shapes (i-.../vol-.../arn:aws:...)
  // -- an Azure ARM path or GCP asset name never matches any of those,
  // so the console button silently never appeared for non-AWS alerts at
  // all. The backend endpoint this gates (/api/alerts/{id}/console-url)
  // now dispatches through get_provider() for any cloud, so any alert
  // with a resource at all is worth attempting -- the existing
  // try/catch in openConsole() below already surfaces a clear error for
  // the genuine case where a console link truly isn't available.
  return !!resource;
}

// ── Internal resource detail route ─────────────────────────────
// Deep-links straight to the resource's row + metrics panel on the
// ServiceDetail page (which reads the `resource` query param and
// auto-selects the matching row instead of making the user search).
const ROUTE_SEGMENT_BY_SERVICE = {
  ec2: "ec2", ebs: "ebs", rds: "rds", lambda: "lambda",
  s3: "s3", elb: "elb", alb: "alb", ecs: "ecs",
};

function detailRoute(resource, accountId, service) {
  if (!resource || !accountId) return null;
  const seg = ROUTE_SEGMENT_BY_SERVICE[(service || "").toLowerCase()];
  if (seg) return `/accounts/${accountId}/${seg}?resource=${encodeURIComponent(resource)}`;

  // `service` covers every provider/tier now (AWS-extended, GCP, Azure),
  // not just the 7 with a bespoke page — ServiceDetailRouter.jsx already
  // knows how to route any of those to GenericServiceDetail.jsx, which
  // now reads this same `?resource=` param to auto-expand the matching
  // row. Previously this fell through to `return null` for anything
  // outside ROUTE_SEGMENT_BY_SERVICE, silently hiding the "view metrics"
  // deep link for every alert on a non-bespoke service.
  if (service) return `/accounts/${accountId}/${service.toLowerCase()}?resource=${encodeURIComponent(resource)}`;

  // Fallback if `service` wasn't provided at all — guess from the resource id shape
  if (resource.startsWith("i-"))   return `/accounts/${accountId}/ec2?resource=${encodeURIComponent(resource)}`;
  if (resource.startsWith("vol-")) return `/accounts/${accountId}/ebs?resource=${encodeURIComponent(resource)}`;
  if (resource.includes("lambda")) return `/accounts/${accountId}/lambda?resource=${encodeURIComponent(resource)}`;
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
  const { ianaName } = useTimezone();
  const role     = (user?.role || "viewer").toLowerCase();
  const canAct   = role === "admin" || role === "editor";

  const [alerts,  setAlerts]  = useState([]);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState(null);
  // Authoritative tab-badge counts, fetched separately from the (capped)
  // row list -- see api.js's getAlertCounts / app/api/alerts.py's
  // /counts endpoint. Falls back to counting the row list only until
  // the first successful fetch, so badges aren't blank on first paint.
  const [counts,  setCounts]  = useState(null);
  const [tab,     setTab]     = useState("active");
  const [search,  setSearch]  = useState("");
  // Account filter -- options are derived from whatever accounts already
  // appear in the full (uncapped) alerts list rather than a separate
  // /api/alerts/accounts fetch: /api/alerts already returns every alert
  // this user can see (RBAC-scoped server-side, see
  // app/api/alerts.py's _filter_rows_by_scope), across every tab, so its
  // account_id/account_name pairs are already the correct, permission-
  // scoped dropdown contents with no extra request needed.
  const [accountId, setAccountId] = useState("");
  const [acting,  setActing]  = useState(null);
  const [soundOn, setSoundOn] = useState(true);
  const [openingConsole, setOpeningConsole] = useState(null);
  // Deep RCA (2026-09-14): which alert row (if any) has its "Why did
  // this happen?" explanation expanded, plus a per-alert-id cache so
  // re-expanding a row already viewed this session doesn't refetch.
  const [expandedExplainId, setExpandedExplainId] = useState(null);
  const [explainCache, setExplainCache] = useState({});
  const [explainLoading, setExplainLoading] = useState(null);

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
    // Tab badges come from a separate, uncapped endpoint -- deliberately
    // NOT derived from the row list above, which is paginated and can be
    // crowded out by a burst of resolved noise (see api.js/alerts.py
    // comments). Fetched alongside but independently, so a failure here
    // doesn't block the row list from loading.
    try {
      setCounts(await apiFetch("/api/alerts/counts"));
    } catch {
      // keep showing the last-known counts rather than blanking badges
    }
  }, []);

  useEffect(() => {
    loadAlerts();
    const t = setInterval(loadAlerts, 10000);
    return () => clearInterval(t);
  }, [loadAlerts]);

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

    // Backend auto-resolved a batch (account removed / orphaned resource
    // cleanup) — just reload rather than trying to patch rows we may not
    // even have IDs for.
    if (lastMessage.type === "bulk_alerts_changed") {
      loadAlerts();
    }
  }, [lastMessage, soundOn, loadAlerts]);

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

  // Opens THIS alert's resource in THIS alert's AWS account. We can't just
  // link straight to console.aws.amazon.com — that ignores which account
  // is intended and opens whatever account the browser is already signed
  // into. Instead we ask the backend for an account-locked sign-in URL scoped to
  // the correct account, then open that.
  async function openConsole(id) {
    // Open the tab synchronously (on the click) so browsers don't block it
    // as a popup once the async fetch resolves.
    //
    // SECURITY: window.open("", "_blank") with no third argument leaves
    // this new tab's `window.opener` pointing back at THIS page -- unlike
    // every other window.open() call in this codebase (ServiceList.jsx,
    // AccountDetail.jsx, ServiceDetail.jsx), which all pass
    // "noopener,noreferrer" directly. Whatever eventually loads in `tab`
    // (here: the AWS account-locked sign-in URL) would otherwise get script-level
    // access to navigate the ORIGINAL tab via window.opener.location --
    // classic reverse tabnabbing. Can't pass "noopener" as a literal
    // argument here the way the other call sites do, since this call
    // has to happen before we know the destination URL (that's the
    // whole point of the two-step pattern) -- so we sever the opener
    // link explicitly instead, immediately after opening, which
    // achieves the same isolation without losing the synchronous-open
    // popup-blocker workaround.
    const tab = window.open("", "_blank");
    if (tab) tab.opener = null;
    setOpeningConsole(id);
    try {
      const { url } = await apiFetch(`/api/alerts/${id}/console-url`, { method: "POST" });
      if (tab) tab.location.href = url;
      else window.open(url, "_blank", "noopener,noreferrer");
    } catch (e) {
      if (tab) tab.close();
      alert("Couldn't open console: " + e.message);
    } finally {
      setOpeningConsole(null);
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

  // Deep RCA (2026-09-14): lazy-fetches /alerts/{id}/explain the first
  // time a row is expanded, caches the result per alert id for the
  // rest of this session, and toggles the expanded row closed if it's
  // clicked again.
  async function toggleExplain(id) {
    if (expandedExplainId === id) {
      setExpandedExplainId(null);
      return;
    }
    setExpandedExplainId(id);
    if (explainCache[id]) return;
    setExplainLoading(id);
    try {
      const data = await apiFetch(`/api/alerts/${id}/explain`);
      setExplainCache(prev => ({ ...prev, [id]: data }));
    } catch (e) {
      setExplainCache(prev => ({ ...prev, [id]: { error: e.message } }));
    } finally {
      setExplainLoading(null);
    }
  }

  // Not-genuine feedback (2026-09-14) -- optimistic UI update (flip the
  // badge immediately), reverted if the request actually fails, same
  // pattern as handleAck/handleResolve's optimistic status updates
  // above.
  async function handleMarkFalsePositive(id, marked) {
    if (!canAct) return;
    setAlerts(prev => prev.map(a => a.id === id ? { ...a, marked_false_positive: marked } : a));
    try {
      await apiFetch(`/api/alerts/${id}/false-positive`, "PATCH", { marked });
    } catch (e) {
      setAlerts(prev => prev.map(a => a.id === id ? { ...a, marked_false_positive: !marked } : a));
      alert("Couldn't update: " + e.message);
    }
  }

  const accountOptions = Array.from(
    new Map(
      alerts
        .filter(a => a.account_id != null)
        .map(a => [a.account_id, a.account_name || `Account ${a.account_id}`])
    ).entries()
  ).sort((a, b) => a[1].localeCompare(b[1]));

  const filtered = alerts.filter(a => {
    if (accountId && String(a.account_id) !== accountId) return false;
    const s = (a.status || "").toLowerCase();
    // "Active" means confirmed live — a resource still sending fresh data
    // that's breaching right now. Stale ones (no fresh data in 20+ min)
    // move to their own tab so they don't clutter the feed you actually
    // watch, without silently resolving/hiding them.
    if (tab === "active"       && (s !== "active" || a.stale)) return false;
    if (tab === "stale"        && (s !== "active" || !a.stale)) return false;
    // "Critical" means currently open and critical -- same definition as
    // the Overview banner/tiles (status active + severity CRITICAL), not
    // "ever was critical". Without the status check, a resolved alert
    // that broke critical stays in this tab forever.
    if (tab === "critical"     && (s !== "active" || (a.severity || "").toUpperCase() !== "CRITICAL")) return false;
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

  // Fall back to counting the (capped) row list only until the first
  // /api/alerts/counts response lands, so badges show *something* on
  // first paint instead of "0" -- this fallback is expected to briefly
  // disagree with reality under the same crowding-out conditions the
  // real counts object fixes, and is replaced within one loadAlerts()
  // cycle (~10s, or immediately on mount).
  const fallbackCounts = {
    all:          alerts.length,
    active:       alerts.filter(a => (a.status || "").toLowerCase() === "active" && !a.stale).length,
    stale:        alerts.filter(a => (a.status || "").toLowerCase() === "active" && a.stale).length,
    critical:     alerts.filter(a => (a.status || "").toLowerCase() === "active" && (a.severity || "").toUpperCase() === "CRITICAL").length,
    acknowledged: alerts.filter(a => (a.status || "").toLowerCase() === "acknowledged").length,
    resolved:     alerts.filter(a => (a.status || "").toLowerCase() === "resolved").length,
  };
  const displayCounts = counts ?? fallbackCounts;

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
          <div className="live-pill"><span className="live-dot" />LIVE</div>
        </div>
      </div>

      <div className="alerts-tabs">
        {[
          ["all",          "All"],
          ["active",       "Active"],
          ["stale",        "Stale"],
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
              {displayCounts[key]}
            </span>
          </button>
        ))}
        <select
          className="alerts-account-filter"
          value={accountId}
          onChange={e => setAccountId(e.target.value)}
        >
          <option value="">All accounts</option>
          {accountOptions.map(([id, name]) => (
            <option key={id} value={String(id)}>{name}</option>
          ))}
        </select>
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
                  const route      = detailRoute(a.resource, a.account_id, a.service);
                  const canOpenAws   = hasConsoleTarget(a.resource);
                  const isOpeningAws = openingConsole === a.id;

                  return (
                    <Fragment key={a.id ?? idx}>
                    <tr className={`alert-row sev-row-${sev.toLowerCase()}`}>

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
                        {a.triggered_at ? shortDateTime(a.triggered_at, ianaName) : "—"}
                        {a.stale && (
                          <div
                            className="alert-stale-flag"
                            title="No fresh metric data for this resource in a while — the resource may have been decommissioned, or the collector/VictoriaMetrics pipeline may be down for it. This alert has NOT been auto-resolved; verify before dismissing."
                            style={{ color: "#c98a2b", fontSize: 11, marginTop: 2 }}
                          >
                            ⚠ stale — no data {timeSince(a.last_seen_at)}
                          </div>
                        )}
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
                          {canOpenAws && (
                            <button
                              className="btn-console-aws"
                              disabled={isOpeningAws}
                              onClick={e => { e.stopPropagation(); openConsole(a.id); }}
                              title="Open in cloud console (correct account)"
                            >
                              {isOpeningAws ? "☁ Opening…" : "☁ Console"}
                            </button>
                          )}
                          {/* Deep RCA (2026-09-14): plain-English probable-
                              root-cause explanation for THIS alert, fetched
                              lazily on first expand. Uses the app's own
                              icon set (icons.jsx), not an emoji, matching
                              the earlier fix on ServiceList's buttons. */}
                          <button
                            className="btn-console-detail"
                            onClick={e => { e.stopPropagation(); toggleExplain(a.id); }}
                            title="Why did this happen?"
                          >
                            <InfoIcon size={13} /> Why?
                          </button>
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
                            {/* Not-genuine feedback (2026-09-14) -- closes
                                the loop with app/collector/
                                threshold_tuning.py's manually_confirmed
                                path: marking a chronic false alert here
                                lets the system switch that threshold to
                                dynamic faster than waiting for the
                                automatic chronic-mean/chronic-noise
                                detection alone. */}
                            <button
                              className={`btn-false-positive ${a.marked_false_positive ? "is-marked" : ""}`}
                              disabled={isActing}
                              title={a.marked_false_positive
                                ? "Marked as not genuine — click to undo"
                                : "This alert isn't a real issue (helps the system self-tune)"}
                              onClick={e => { e.stopPropagation(); handleMarkFalsePositive(a.id, !a.marked_false_positive); }}
                            >
                              {a.marked_false_positive ? "✓ Not genuine" : "Not genuine?"}
                            </button>
                          </div>
                        </td>
                      )}
                    </tr>

                    {expandedExplainId === a.id && (
                      <tr className="alert-explain-row">
                        <td colSpan={canAct ? 8 : 7}>
                          {explainLoading === a.id ? (
                            <div className="alert-explain-loading">Analyzing…</div>
                          ) : explainCache[a.id]?.error ? (
                            <div className="alert-explain-error">
                              Couldn't load explanation: {explainCache[a.id].error}
                            </div>
                          ) : explainCache[a.id] ? (
                            <div className="alert-explain">
                              <div className="alert-explain-head">
                                <span className={`explain-confidence explain-confidence-${explainCache[a.id].confidence}`}>
                                  {explainCache[a.id].confidence} confidence
                                </span>
                                {explainCache[a.id].summary_source === "llm" && (
                                  <span className="explain-ai-badge" title="This summary was polished by an LLM from the facts below -- it never adds facts not already gathered">
                                    ✨ AI-polished
                                  </span>
                                )}
                                <a
                                  className="explain-rca-report-link"
                                  href={rcaReportUrl(a.id, "pdf")}
                                  target="_blank"
                                  rel="noreferrer"
                                  title="Download a full RCA report (timeline, probable cause, recommendations) as a PDF"
                                >
                                  <DownloadIcon size={12} /> RCA Report
                                </a>
                              </div>
                              <p className="explain-summary">{explainCache[a.id].summary}</p>
                            </div>
                          ) : null}
                        </td>
                      </tr>
                    )}
                    </Fragment>
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

function timeSince(iso) {
  if (!iso) return "";
  try {
    const ms = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(ms / 60000);
    if (mins < 60) return `${mins}m`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h`;
    return `${Math.floor(hrs / 24)}d`;
  } catch {
    return "";
  }
}

function shortDateTime(iso, ianaName) {
  // Timezone-aware version of the previous browser-local formatter —
  // ianaName is threaded in from the calling component's useTimezone()
  // since this is a plain helper, not a component, and can't call
  // hooks itself.
  return formatInTz(iso, ianaName, {
    month:  "numeric",
    day:    "numeric",
    year:   "numeric",
    hour:   "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }) || iso;
}
