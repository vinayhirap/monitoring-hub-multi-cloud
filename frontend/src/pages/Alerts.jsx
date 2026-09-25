// monitoring-hub/frontend/src/pages/Alerts.jsx
import { useEffect, useState, useCallback, useRef, Fragment } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useWebSocket } from "../hooks/useWebSocket";
import "./Alerts.css";
import { useTimezone, formatInTz } from "../contexts/TimezoneContext";
import { InfoIcon, DownloadIcon } from "../components/icons";
import { rcaReportUrl } from "../api/api";
import { clearAllCached } from "../utils/dataCache";

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
// NOTE (updated 2026-09-19, see app/aws/federation.py's
// build_federated_console_url docstring for the full story): this
// links straight to the specific resource's console page. AWS has no
// supported way to force a fresh, account-specific sign-in prompt AND
// land on a deep-linked resource page for native IAM users without
// either a SAML/SSO identity-provider relationship or minting real
// temporary credentials (the latter being exactly the impersonation
// this app avoids) -- so if the browser already has an AWS session
// for a DIFFERENT account, this can open under that wrong account. If
// no AWS session exists yet in the browser, AWS's own native sign-in
// prompt appears and correctly returns the person to this exact page
// after they sign in with their own credentials.
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
// SECURITY/CORRECTNESS: these two were missing the 401-session-expiry
// handling every other network call in this app gets via api.js's
// shared apiFetch() (redirect to /login, clear the stale data cache --
// see that function's own docstring). This file predates api.js's
// apiFetch and still can't call it directly here (different signature:
// positional method/body below vs. an options object there), so the
// same 401 behavior is replicated locally instead, in both helpers.
function _handleUnauthorized() {
  clearAllCached();
  if (window.location.pathname !== "/login") {
    window.location.href = "/login";
  }
}

async function apiFetch(path, method = "GET", body) {
  const opts = { method, credentials: "include", headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(`${BASE}${path}`, opts);
  if (res.status === 401) {
    _handleUnauthorized();
    throw new Error("401 (session expired)");
  }
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new Error(d.detail || `${res.status}`);
  }
  return res.json();
}

// List fetch that also returns the server's full match count (X-Total-Count),
// so the UI can page instead of silently truncating at a fixed row cap.
async function apiFetchList(path) {
  const res = await fetch(`${BASE}${path}`, { credentials: "include", headers: { "Content-Type": "application/json" } });
  if (res.status === 401) {
    _handleUnauthorized();
    throw new Error("401 (session expired)");
  }
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new Error(d.detail || `${res.status}`);
  }
  const data = await res.json();
  const total = parseInt(res.headers.get("X-Total-Count") ?? "", 10);
  return { rows: Array.isArray(data) ? data : (data.alerts ?? []), total: Number.isNaN(total) ? null : total };
}

const PAGE_SIZE = 100;

// ── Main component ─────────────────────────────────────────────
export default function Alerts() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { ianaName } = useTimezone();
  const role     = (user?.role || "viewer").toLowerCase();
  const canAct   = role === "admin" || role === "editor";

  const [alerts,  setAlerts]  = useState([]);
  const [total,   setTotal]   = useState(0);
  const [limit,   setLimit]   = useState(PAGE_SIZE);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState(null);
  // Tab badges (/api/alerts/counts) and the row list (/api/alerts?tab=...)
  // are now defined by the SAME server-side rules (app/alert_rules.py), and
  // both honour the account filter, so a badge always equals the number of
  // rows its tab lists.
  const [counts,  setCounts]  = useState(null);
  // Seeded once from ?tab=/?q= on mount (Search.jsx's row click links
  // here this way, since with server-defined tabs + pagination, just
  // navigating to a bare /alerts can easily land on a page/tab that
  // doesn't include the alert the person searched for and clicked).
  // Deliberately a one-time lazy-initializer, not a synced effect, so
  // the person's own subsequent tab/search changes aren't fought by
  // the URL on every render.
  const [tab,     setTab]     = useState(() => searchParams.get("tab") || "active");
  const [search,  setSearch]  = useState(() => searchParams.get("q") || "");
  const [debouncedSearch, setDebouncedSearch] = useState("");
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

  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);

  // changing tab / account / search starts a fresh page
  useEffect(() => { setLimit(PAGE_SIZE); }, [tab, accountId, debouncedSearch]);

  const loadAlerts = useCallback(async () => {
    setError(null);
    try {
      const qs = new URLSearchParams({ tab, limit: String(limit), offset: "0" });
      if (accountId) qs.set("account_id", accountId);
      if (debouncedSearch) qs.set("q", debouncedSearch);
      const { rows, total: t } = await apiFetchList(`/api/alerts?${qs}`);
      // Seed knownIds so existing alerts never trigger a beep
      rows.forEach(a => knownIds.current.add(a.id));
      setAlerts(rows);
      setTotal(t ?? rows.length);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
    try {
      setCounts(await apiFetch(`/api/alerts/counts${accountId ? `?account_id=${accountId}` : ""}`));
    } catch {
      // keep showing the last-known counts rather than blanking badges
    }
  }, [tab, limit, accountId, debouncedSearch]);

  useEffect(() => {
    loadAlerts();
    const t = setInterval(loadAlerts, 10000);
    return () => clearInterval(t);
  }, [loadAlerts]);

  // WebSocket push. The list is server-defined per tab, so every event just
  // triggers a reload instead of splicing a partial message into the rows
  // (the old code inserted the raw push payload as a row -- a half-empty
  // "ghost" alert until the next poll). Beep only for genuinely new ids.
  useEffect(() => {
    if (!lastMessage) return;
    const pushedId = lastMessage.id ?? lastMessage.alert_id;

    if (lastMessage.type === "new_alert") {
      if (soundOn && pushedId != null && !knownIds.current.has(pushedId)) {
        playBeep((lastMessage.severity || "").toUpperCase());
      }
      if (pushedId != null) knownIds.current.add(pushedId);
      loadAlerts();
    } else if (
      lastMessage.type === "alert_resolved" ||
      lastMessage.type === "alert_acknowledged" ||
      lastMessage.type === "bulk_alerts_changed"
    ) {
      loadAlerts();
    }
  }, [lastMessage, soundOn, loadAlerts]);

  // Every action reloads from the server: with server-defined tabs, patching
  // one row locally would leave it in a tab it no longer belongs to.
  async function handleAck(id) {
    if (!canAct) return;
    setActing(id);
    try {
      await apiFetch(`/api/alerts/${id}/ack`, "PATCH");
      await loadAlerts();
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
      await apiFetch(`/api/alerts/${id}/resolve`, "PATCH");
      await loadAlerts();
    } catch (e) {
      alert("Resolve failed: " + e.message);
    } finally {
      setActing(null);
    }
  }

  // Mute suppresses an OPEN alert for a while (server-enforced: it stops
  // counting as critical/warning everywhere, is not escalated and is off the
  // public status page). It is not a resolve -- if it is still breaching when
  // the mute lapses it counts again by itself.
  async function handleMute(id, minutes) {
    if (!canAct) return;
    setActing(id);
    try {
      await apiFetch(`/api/alerts/${id}/${minutes ? `mute?minutes=${minutes}` : "unmute"}`, "PATCH");
      await loadAlerts();
    } catch (e) {
      alert("Mute failed: " + e.message);
    } finally {
      setActing(null);
    }
  }

  // Opens THIS alert's specific resource page in THIS alert's AWS
  // account. We can't just link straight to console.aws.amazon.com
  // client-side without knowing the target account, so we ask the
  // backend to build the correct resource-specific URL (see
  // app/aws/federation.py's resource_console_destination) -- see that
  // file's build_federated_console_url docstring for why this is a
  // direct resource link rather than a wrapped sign-in URL.
  // 2026-09-20: this called apiFetch(url, { method: "POST" }) but this file's
  // apiFetch takes the method as a plain STRING, so fetch() received the method
  // "[object Object]" and threw -- the Console button never worked.
  async function openConsole(id) {
    // Open the tab synchronously (on the click) so browsers don't block it
    // as a popup once the async fetch resolves.
    //
    // SECURITY: window.open("", "_blank") with no third argument leaves
    // this new tab's `window.opener` pointing back at THIS page -- unlike
    // every other window.open() call in this codebase (ServiceList.jsx,
    // AccountDetail.jsx, ServiceDetail.jsx), which all pass
    // "noopener,noreferrer" directly. Whatever eventually loads in `tab`
    // (here: the resource-specific console URL) would otherwise get script-level
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
      const { url } = await apiFetch(`/api/alerts/${id}/console-url`, "POST");
      if (tab) tab.location.href = url;
      else window.open(url, "_blank", "noopener,noreferrer");
    } catch (e) {
      if (tab) tab.close();
      alert("Couldn't open console: " + e.message);
    } finally {
      setOpeningConsole(null);
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
  // badge immediately), reverted if the request actually fails.
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

  // Dropdown options come from the counts endpoint (RBAC-scoped, independent
  // of the current filter) instead of being derived from whichever rows
  // happen to be loaded.
  const accountOptions = (counts?.accounts ?? []).map(a => [a.id, a.name]);
  const filtered = alerts;
  const displayCounts = counts ?? { all: 0, active: 0, stale: 0, critical: 0, acknowledged: 0, resolved: 0, suppressed: 0 };

  return (
    <div className="alerts-page">
      <div className="alerts-header">
        <div>
          <h1>Active <span className="accent">Alerts</span></h1>
          <p className="alerts-sub">Live alerts across all accounts — counts here match the Overview banner and every resource badge</p>
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
          ["suppressed",   "Muted / Maint."],
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
                        <span className="alert-val" title={String(a.current_value ?? "")}>{fmt(a.current_value)}</span>
                        <span className="alert-sep"> / </span>
                        <span className="alert-thr" title={String(a.threshold ?? "")}>{fmt(a.threshold)}</span>
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

                      <td><StatusBadge status={a.state || status} detail={a.silenced_reason || (a.muted_until ? `Muted until ${shortDateTime(a.muted_until, ianaName)}` : a.resolution_reason ? `Resolved: ${a.resolution_reason.replace(/_/g, " ")}` : "")} /></td>

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
                            {status !== "resolved" && a.state !== "suppressed" && (
                              <button
                                className="btn-ack"
                                disabled={isActing}
                                title="Mute for 1 hour: stops counting as critical/warning and stops escalating; not resolved"
                                onClick={e => { e.stopPropagation(); handleMute(a.id, 60); }}
                              >
                                Mute 1h
                              </button>
                            )}
                            {a.state === "suppressed" && a.muted_until && (
                              <button
                                className="btn-ack"
                                disabled={isActing}
                                onClick={e => { e.stopPropagation(); handleMute(a.id, 0); }}
                              >
                                Unmute
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

          <div style={{ padding: "8px 16px", color: "var(--text-muted)", fontSize: 12, display: "flex", gap: 12, alignItems: "center" }}>
            <span>Showing {filtered.length} of {total}</span>
            {filtered.length < total && limit < 1000 && (
              <button className="btn-refresh" onClick={() => setLimit(l => Math.min(l + PAGE_SIZE, 1000))}>Show more</button>
            )}
            {filtered.length < total && limit >= 1000 && <span>(narrow with search or the account filter to see the rest)</span>}
          </div>

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

function StatusBadge({ status, detail }) {
  // `status` is the server's derived state (app/alert_rules.py):
  // firing | stale | suppressed | acknowledged | resolved (legacy: active)
  const cls = {
    firing:       "st-badge st-active",
    active:       "st-badge st-active",
    stale:        "st-badge st-resolved",
    suppressed:   "st-badge st-ack",
    acknowledged: "st-badge st-ack",
    resolved:     "st-badge st-resolved",
  }[status] || "st-badge st-active";
  const label = { firing: "ACTIVE", stale: "NO DATA", suppressed: "MUTED" }[status] || status.toUpperCase();
  return <span className={cls} title={detail || undefined}>{label}</span>;
}

// Compact, unit-agnostic number formatting. Raw values like 712199220386 or
// 0.03 were shown as "712199220386" / "0.0"; the exact value is in the tooltip.
function fmt(v) {
  if (v == null) return "—";
  const n = parseFloat(v);
  if (isNaN(n)) return String(v);
  const abs = Math.abs(n);
  if (abs >= 1e12) return (n / 1e12).toFixed(2) + "T";
  if (abs >= 1e9)  return (n / 1e9).toFixed(2) + "G";
  if (abs >= 1e6)  return (n / 1e6).toFixed(2) + "M";
  if (abs >= 1e4)  return (n / 1e3).toFixed(1) + "K";
  if (abs !== 0 && abs < 0.1) return n.toPrecision(2);
  return n % 1 === 0 ? String(n) : n.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
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
