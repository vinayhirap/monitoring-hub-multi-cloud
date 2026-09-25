// monitoring-hub/frontend/src/pages/GenericServiceDetail.jsx
//
// Detail page for any service that does NOT have a bespoke, hand-built
// page like ServiceDetail.jsx's EC2/EBS/RDS/S3/ECS/ELB/Lambda views —
// i.e. every AWS-extended service (DynamoDB, SQS, CloudFront, EKS,
// Redshift, ...) and every GCP/Azure service. Rendered by
// ServiceDetailRouter.jsx whenever ServiceDetail.hasCoreDetailPage()
// says no bespoke page exists for the requested service key.
//
// This page is deliberately DATA-DRIVEN instead of hardcoded per
// service: it reads whichever metrics metric_catalog says exist for
// (provider, service) and charts every one of them, for every resource,
// automatically. That's what makes it cover literally every metric for
// every AWS-extended / GCP / Azure service today AND any service or
// metric added to the catalog in the future, with zero further code
// changes to this file — the alternative (a bespoke switch-per-service
// page like ServiceDetail.jsx has for its original 7) would need a new
// hardcoded block every time a service or metric is added, which is
// exactly the maintenance burden this page exists to avoid.
//
// Resource list sourced from GET /api/live/resources-list/{id}/{svc}
// (the shared `resources` table every provider's discovery pipeline
// writes into). Expanding a row fetches
// GET /api/live/metrics/generic/{id}/{svc}/{resourceId}?hours=N — real
// charts from the same metric_history table every bespoke chart in this
// app reads from (see that endpoint's backend comment), not a
// placeholder. Charts are per-resource and lazy (fetched on first
// expand, not for every row up front) since a service can have a large
// resource count.
//
// Brought up to feature parity with the bespoke EC2-style page:
//   - time-range selector (1H..ALL), same set ServiceDetail.jsx uses
//   - real configured warning/critical threshold lines on every chart,
//     from the same /api/settings/thresholds endpoint ServiceDetail
//     reads (threshold rows are already keyed by service+metric_name in
//     metric_catalog, so this needs no per-service mapping either)
//   - proportional, timezone-aware time axis (not the evenly-spaced
//     category axis a naive chart would default to)
//   - search box, state filter chips with live counts, sort
//   - state summary chips in the header (e.g. "Running: 4 · Stopped: 1")
//   - deep-link support: a `?resource=` param (the same one Alerts.jsx
//     already sends every bespoke page) auto-expands and scrolls to
//     that resource's row instead of leaving the user to search for it
import { useEffect, useState, useCallback, useRef, Fragment } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { getResourcesList, getGenericMetrics, getConsoleUrl, getAccount, getThresholds } from "../api/api";
import { CloudServiceIcon } from "../components/cloud-icons";
import { ArrowLeftIcon, ExternalLinkIcon, ChevronDownIcon, AlertTriangleIcon } from "../components/icons";
import { useTimezone } from "../contexts/TimezoneContext";
import AlertBadge from "../components/AlertBadge";
import { useResourceAlerts } from "../hooks/useResourceAlerts";

const TIME_RANGES = [
  { label: "1H",  hours: 1 },
  { label: "3H",  hours: 3 },
  { label: "6H",  hours: 6 },
  { label: "1D",  hours: 24 },
  { label: "1W",  hours: 168 },
  { label: "1M",  hours: 720 },
  { label: "6M",  hours: 4320 },
  { label: "1Y",  hours: 8760 },
  { label: "ALL", hours: 17520 },
];

// Cycled per metric within a resource's chart grid so a service with
// many metrics (e.g. an Azure SQL Database or a GCP GKE cluster) still
// reads as distinct series rather than a wall of identically-colored
// charts -- the same palette ServiceDetail.jsx's bespoke pages use.
const CHART_PALETTE = ["#2bb3ac", "#38bdf8", "#7c6ee0", "#fbbf24", "#34d399", "#f472b6", "#ef4444", "#22c55e", "#e879f9", "#f59e0b"];

// Common state/status vocabulary across AWS/GCP/Azure resources, mapped
// to the same green/yellow/red/muted badge language ServiceDetail.jsx
// uses for its 7 bespoke services -- so e.g. a GCP Cloud Run "READY" and
// an AWS EC2 "running" both read as "healthy" at a glance.
const STATE_COLOR = {
  running: "green", active: "green", available: "green", "in-use": "green",
  ready: "green", healthy: "green", succeeded: "green", enabled: "green",
  provisioning: "yellow", pending: "yellow", creating: "yellow", updating: "yellow", starting: "yellow", modifying: "yellow", deploying: "yellow",
  stopped: "muted", disabled: "muted", suspended: "muted", paused: "muted",
  failed: "red", error: "red", terminated: "red", degraded: "red", unhealthy: "red", deleting: "red",
};

// SECURITY/CORRECTNESS: this used to be a raw fetch() -- unlike every
// other network call in this app, it never went through apiFetch(), so
// a mid-session expiry here silently left `account` as null forever
// instead of bouncing to /login like the rest of the app does on a
// 401 (see api.js's apiFetch docstring). getAccount() is the same
// shared helper AccountDetail.jsx/ServiceList.jsx now use.
async function fetchAccount(id) {
  return getAccount(id);
}

// Account-wide configured thresholds, keyed the same way the generic
// metrics endpoint keys its own response (metric_catalog.service +
// metric_name) -- so a chart for ANY service/metric combo can look
// itself up here with no per-service mapping, exactly like ServiceDetail
// .jsx's getThreshold() does for its 7 bespoke services. Best-effort:
// this page still renders full charts with no threshold lines if the
// caller lacks alerts.view or the request otherwise fails.
//
// Was a raw fetch() -- see fetchAccount()'s comment above for why that
// matters; getThresholds() is the same shared helper ServiceDetail.jsx
// now uses too.
async function fetchThresholdMap(accountId) {
  const map = {};
  try {
    const data = await getThresholds(accountId, true);
    (data.thresholds || []).forEach(t => {
      if (!t.metric_name || !t.service) return;
      map[`${t.service}:${t.metric_name}`] = { warning: t.warning_value, critical: t.critical_value };
    });
  } catch { /* thresholds are an overlay, not a requirement -- ignore */ }
  return map;
}

function capitalize(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

function StateBadge({ state }) {
  if (!state) return <span className="mono small muted">—</span>;
  const color = STATE_COLOR[state.toLowerCase()] || "muted";
  return <span className={`state-badge sb-${color}`} style={{ textTransform: "capitalize" }}>{state}</span>;
}

function MetricChart({ title, unit, description, data, color, warningThreshold, criticalThreshold, timeRangeLabel, ianaName }) {
  if (!data || data.length === 0) {
    return (
      <div className="chart-box">
        <div className="chart-title">{title}</div>
        <div className="chart-empty">No data in last {timeRangeLabel}</div>
      </div>
    );
  }
  // Kept as a real epoch-ms number with a proportional (type="number")
  // axis and dataMin/dataMax domain -- NOT a pre-formatted display
  // string -- so gaps in the data show as visual gaps instead of being
  // silently smoothed away by Recharts' default evenly-spaced category
  // axis. Same fix ServiceDetail.jsx's MetricChart already applies to
  // every bespoke chart; a sparse metric here (e.g. an error-count metric
  // that's near-empty most of the time) deserves the same honesty.
  const formatted = data.map(d => ({
    t: new Date(d.t).getTime(),
    v: d.v,
    ...(warningThreshold != null ? { warningThreshold } : {}),
    ...(criticalThreshold != null ? { criticalThreshold } : {}),
  }));
  const latest = data[data.length - 1]?.v ?? 0;
  const unitLabel = unit ? ` ${unit}` : "";
  const fmtTick = (ms) => new Date(ms).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ianaName });
  return (
    <div className="chart-box">
      <div className="chart-header">
        <span className="chart-title" title={description || title}>{title}</span>
        <span className="chart-latest" style={{ color }}>{typeof latest === "number" ? latest.toFixed(2) : latest}{unitLabel}</span>
      </div>
      <ResponsiveContainer width="100%" height={100}>
        <LineChart data={formatted} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
          <CartesianGrid stroke="rgba(99,130,190,0.08)" strokeDasharray="3 3" />
          <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={fmtTick}
                 tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false} scale="time" />
          <YAxis tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false} width={38} />
          <Tooltip
            contentStyle={{ background: "#0b1220", border: "1px solid rgba(99,130,190,0.2)", borderRadius: 6, fontSize: 11 }}
            labelStyle={{ color: "#7a90b8" }}
            labelFormatter={fmtTick}
            formatter={(value, name) => {
              if (name === "warningThreshold") return [`${value}${unitLabel}`, <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><AlertTriangleIcon size={11} /> Warn at</span>];
              if (name === "criticalThreshold") return [`${value}${unitLabel}`, <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><AlertTriangleIcon size={11} /> Crit at</span>];
              return [`${typeof value === "number" ? value.toFixed(2) : value}${unitLabel}`, title];
            }}
            itemStyle={{ color }}
          />
          {warningThreshold != null && (
            <Line type="monotone" dataKey="warningThreshold" stroke="#f59e0b" strokeDasharray="4 4" dot={false} strokeWidth={1} legendType="none" />
          )}
          {criticalThreshold != null && (
            <Line type="monotone" dataKey="criticalThreshold" stroke="#ef4444" strokeDasharray="2 3" dot={false} strokeWidth={1} legendType="none" />
          )}
          <Line type="monotone" dataKey="v" stroke={color} strokeWidth={2} dot={false} activeDot={{ r: 3, fill: color }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function ResourceRow({ r, isLast, accountId, service, timeRange, timeRangeLabel, thresholdMap, autoExpand, alertInfo }) {
  const { ianaName } = useTimezone();
  const [expanded, setExpanded] = useState(false);
  const [metrics, setMetrics] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [consoleLoading, setConsoleLoading] = useState(false);
  const rowRef = useRef(null);
  const autoHandledRef = useRef(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    getGenericMetrics(accountId, service, r.resource_id, timeRange)
      .then(data => setMetrics(data || {}))
      .catch(e => setError(e.message || "Failed to load metrics"))
      .finally(() => setLoading(false));
  }, [accountId, service, r.resource_id, timeRange]);

  function toggle() {
    const next = !expanded;
    setExpanded(next);
    if (next) load();
  }

  // Per-resource console deep link -- distinct from this page's
  // top-of-page openInConsole(), which only ever opens the generic
  // service list (no resource_id). This is what actually lets any of
  // the ~30 extended AWS resource types (and every GCP/Azure service
  // this page also renders) deep-link to the SPECIFIC resource's
  // console page, not just the service list -- see
  // app/aws/federation.py's resource_console_destination() for which
  // resource types get a precise deep link vs. a service-level
  // fallback. stopPropagation so clicking the button doesn't also
  // toggle the row's metric-chart expansion.
  function openConsole(e) {
    e.stopPropagation();
    setConsoleLoading(true);
    getConsoleUrl(accountId, service, { resourceId: r.resource_id, region: r.region, resourceName: r.name })
      .then(res => { if (res?.url) window.open(res.url, "_blank", "noopener,noreferrer"); })
      .catch(() => {
        window.alert("Couldn't open the cloud console for this resource. Check that credentials are configured for this account in Settings.");
      })
      .finally(() => setConsoleLoading(false));
  }

  // Re-fetch whenever the shared time-range selector changes, but only
  // for rows that are actually expanded -- collapsed rows just pick up
  // the new range whenever they're next opened via toggle()'s load().
  useEffect(() => {
    if (expanded) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [timeRange]);

  // Deep-link support: Alerts.jsx links here with ?resource=<id> for any
  // service, not just the 7 bespoke ones -- auto-expand and scroll to
  // this row instead of leaving the user to find it in a long list.
  useEffect(() => {
    if (autoExpand && !autoHandledRef.current) {
      autoHandledRef.current = true;
      setExpanded(true);
      load();
      requestAnimationFrame(() => rowRef.current?.scrollIntoView({ behavior: "smooth", block: "center" }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoExpand]);

  const metricNames = metrics ? Object.keys(metrics) : [];

  return (
    <>
      <tr
        ref={rowRef}
        onClick={toggle}
        className={`inst-row${autoExpand ? " inst-selected" : ""}`}
        style={{ borderBottom: (isLast && !expanded) ? "none" : "1px solid var(--border)" }}
      >
        <td style={{ padding: "10px 14px", width: 20 }}>
          <ChevronDownIcon size={13} style={{ color: "var(--text-muted)", transform: expanded ? "rotate(0deg)" : "rotate(-90deg)", transition: "transform .15s" }} />
        </td>
        <td style={{ padding: "10px 14px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <AlertBadge info={alertInfo} />
            <div>
              <div className="inst-name">{r.name || r.resource_id}</div>
              {r.name && r.name !== r.resource_id && (
                <div className="inst-id mono">{r.resource_id}</div>
              )}
            </div>
          </div>
        </td>
        <td style={{ padding: "10px 14px", color: "var(--text-muted)" }}>{r.region || "—"}</td>
        <td style={{ padding: "10px 14px" }}><StateBadge state={r.instance_state} /></td>
        <td style={{ padding: "10px 14px", color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: 11 }}>
          {r.created_at ? new Date(r.created_at).toLocaleString("en-US", { timeZone: ianaName }) : "—"}
        </td>
        <td style={{ padding: "10px 14px", textAlign: "right" }}>
          <button
            className="btn-icon-console"
            onClick={openConsole}
            disabled={consoleLoading}
            title="Open this resource in the cloud console (you'll sign in with your own credentials)"
          >
            <ExternalLinkIcon size={13} />
          </button>
        </td>
      </tr>
      {expanded && (
        <tr style={{ borderBottom: isLast ? "none" : "1px solid var(--border)" }}>
          <td colSpan={6} style={{ padding: "0 14px 14px 40px", background: "rgba(255,255,255,.015)" }}>
            {loading ? (
              <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "10px 0" }}>Loading metrics…</div>
            ) : error ? (
              <div style={{ fontSize: 12, color: "var(--red)", padding: "10px 0" }}>{error}</div>
            ) : metricNames.length === 0 ? (
              <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "10px 0" }}>
                No metric data collected yet for this resource in the last {timeRangeLabel}. If you expect a
                metric here, confirm it's enabled for this service in <b style={{ color: "var(--text-secondary)" }}>Settings → Metrics</b>,
                or try a wider time range above.
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 10, paddingTop: 10 }}>
                {metricNames.map((name, i) => {
                  const th = thresholdMap[`${service}:${name}`];
                  return (
                    <MetricChart
                      key={name}
                      title={name}
                      unit={metrics[name].unit}
                      description={metrics[name].description}
                      data={metrics[name].series}
                      color={CHART_PALETTE[i % CHART_PALETTE.length]}
                      warningThreshold={th?.warning}
                      criticalThreshold={th?.critical}
                      timeRangeLabel={timeRangeLabel}
                      ianaName={ianaName}
                    />
                  );
                })}
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

export default function GenericServiceDetail({ accountId, service, label }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [account, setAccount] = useState(null);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [consoleLoading, setConsoleLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [sortKey, setSortKey] = useState("name");
  const [timeRange, setTimeRange] = useState(6);
  const [thresholdMap, setThresholdMap] = useState({});
  // CRITICAL/WARNING badge per row -- same server rollup as every other
  // resource page, Overview and the Alerts tabs (extended + directory
  // services previously had no alert indication at all).
  const { lookup: alertLookup } = useResourceAlerts(accountId, service);

  const resourceParam = searchParams.get("resource");

  useEffect(() => {
    fetchAccount(accountId).then(setAccount).catch(() => {});
  }, [accountId]);

  useEffect(() => {
    let cancelled = false;
    fetchThresholdMap(accountId).then(m => { if (!cancelled) setThresholdMap(m); });
    return () => { cancelled = true; };
  }, [accountId]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getResourcesList(accountId, service)
      .then(data => { if (!cancelled) setRows(Array.isArray(data) ? data : []); })
      .catch(e => { if (!cancelled) setError(e.message || "Failed to load resources"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    // Resource list only, on an interval — NOT re-fetching metrics for
    // every expanded row on the same timer, to avoid hammering
    // metric_history with repeat queries for rows the user isn't
    // actively looking at. A resource-count/state change is worth
    // catching passively; a chart refresh isn't as time-critical here.
    const t = setInterval(() => {
      getResourcesList(accountId, service).then(data => { if (!cancelled) setRows(Array.isArray(data) ? data : []); }).catch(() => {});
    }, 15000);
    return () => { cancelled = true; clearInterval(t); };
  }, [accountId, service]);

  function openInConsole() {
    setConsoleLoading(true);
    getConsoleUrl(accountId, service)
      .then(r => { if (r?.url) window.open(r.url, "_blank", "noopener,noreferrer"); })
      .catch(() => {
        window.alert("Couldn't open the cloud console for this service. Check that credentials are configured for this account in Settings.");
      })
      .finally(() => setConsoleLoading(false));
  }

  const stateCounts = rows.reduce((acc, r) => {
    const s = (r.instance_state || "unknown").toLowerCase();
    acc[s] = (acc[s] || 0) + 1;
    return acc;
  }, {});
  const filterStates = ["all", ...Object.keys(stateCounts)];
  // Same reasoning as ServiceDetail.jsx: hide the filter/sort-by-state
  // UI entirely for a resource type with no real lifecycle state (e.g.
  // S3-like buckets), where every row would fall into "unknown" and the
  // filter bar would just be a useless "All N / Unknown N" pair.
  const hasRealStates = Object.keys(stateCounts).some(s => s !== "unknown");

  const visible = rows
    .filter(r => {
      if (search) {
        const hay = `${r.name || ""} ${r.resource_id || ""} ${r.region || ""} ${r.instance_state || ""}`.toLowerCase();
        if (!hay.includes(search.toLowerCase())) return false;
      }
      if (filter !== "all" && (r.instance_state || "unknown").toLowerCase() !== filter) return false;
      return true;
    })
    .sort((a, b) => {
      if (sortKey === "state")  return (a.instance_state || "").localeCompare(b.instance_state || "");
      if (sortKey === "region") return (a.region || "").localeCompare(b.region || "");
      return (a.name || a.resource_id || "").localeCompare(b.name || b.resource_id || "");
    });

  const provider = account?.provider || "aws";
  const rangeLabel = TIME_RANGES.find(t => t.hours === timeRange)?.label || "6H";
  // Top few states only, in the header summary chips -- mirrors
  // ServiceDetail.jsx's "● N running · ◯ N stopped" treatment but
  // generalized to whatever states this service's resources actually
  // report, instead of a hardcoded running/stopped pair.
  const topStates = Object.entries(stateCounts)
    .filter(([s]) => s !== "unknown")
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4);

  return (
    <div className="detail-page">
      <div className="breadcrumb">
        <span className="bc-link" onClick={() => navigate("/overview")}>ALL ACCOUNTS</span>
        <span className="bc-sep">›</span>
        <span className="bc-link" onClick={() => navigate(`/accounts/${accountId}`)}>{account?.account_name ?? `Account ${accountId}`}</span>
        <span className="bc-sep">›</span>
        <span className="bc-link" onClick={() => navigate(`/accounts/${accountId}/services`)}>SERVICES</span>
        <span className="bc-sep">›</span>
        <span className="bc-current">{(label || service || "").toUpperCase()}</span>
      </div>

      <div className="detail-header">
        <div>
          <h1>
            <span style={{ marginRight: 8, display: "inline-flex", verticalAlign: "middle" }}>
              <CloudServiceIcon provider={provider} service={service} size={20} />
            </span>
            {label || service} — <span className="hl">{loading ? "—" : `${rows.length} total`}</span>
          </h1>
          <div className="detail-meta">
            <span className="meta-tag">{provider.toUpperCase()}</span>
            {topStates.map(([s, c]) => (
              <Fragment key={s}>
                <span className="meta-sep">·</span>
                <span className={s === topStates[0][0] ? "meta-running" : "meta-stopped"}>
                  {capitalize(s)}: {c}
                </span>
              </Fragment>
            ))}
          </div>
        </div>
        <div className="detail-header-right">
          <button className="btn-back" onClick={() => navigate(`/accounts/${accountId}/services`)}><ArrowLeftIcon size={13} /> Back</button>
          <button className="btn-aws" onClick={openInConsole} disabled={consoleLoading}>
            <ExternalLinkIcon size={13} /> {consoleLoading ? "Opening…" : "Open Console"}
          </button>
        </div>
      </div>

      <div style={{
        background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: "var(--radius-lg)",
        padding: "12px 16px", marginBottom: 16, fontSize: 12, color: "var(--text-muted)",
      }}>
        Click a resource row below to expand its metric charts — every metric enabled for this service in{" "}
        <b style={{ color: "var(--text-secondary)" }}>Settings → Metrics</b> is charted automatically, same
        underlying data (and, where configured, the same warning/critical threshold lines) as every other
        page in this app. Check the{" "}
        <span className="bc-link" onClick={() => navigate(`/accounts/${accountId}/incidents`)}>Alerts</span> page
        for this account to see anything currently firing.
      </div>

      <div className="inst-toolbar">
        <input
          className="inst-search"
          placeholder={`Search ${label || service} resources…`}
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
        {hasRealStates && (
          <div className="state-filters">
            {filterStates.map(s => (
              <button
                key={s}
                className={`sf-btn ${filter === s ? "sf-active" : ""}`}
                onClick={() => setFilter(s)}
              >
                {s === "all" ? "All" : capitalize(s)}
                <span className="sf-count">{s === "all" ? rows.length : (stateCounts[s] || 0)}</span>
              </button>
            ))}
          </div>
        )}
        <select className="sort-select" value={sortKey} onChange={e => setSortKey(e.target.value)}>
          <option value="name">Sort: Name</option>
          <option value="region">Sort: Region</option>
          {hasRealStates && <option value="state">Sort: State</option>}
        </select>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>Metrics range:</span>
        <div className="time-range-tabs">
          {TIME_RANGES.map(t => (
            <button
              key={t.label}
              className={`tr-btn ${timeRange === t.hours ? "tr-active" : ""}`}
              onClick={() => setTimeRange(t.hours)}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div style={{ color: "var(--text-muted)", fontSize: 13, padding: "40px 0", textAlign: "center" }}>Loading resources…</div>
      ) : error ? (
        <div style={{ color: "var(--red)", fontSize: 13, padding: "40px 0", textAlign: "center" }}>{error}</div>
      ) : rows.length === 0 ? (
        <div style={{
          border: "1px dashed var(--border)", borderRadius: "var(--radius-lg)", padding: "40px 24px",
          textAlign: "center", color: "var(--text-muted)", fontSize: 13,
        }}>
          No resources currently on record for this service. This list refreshes automatically
          once the next discovery cycle finds any.
        </div>
      ) : visible.length === 0 ? (
        <div style={{
          border: "1px dashed var(--border)", borderRadius: "var(--radius-lg)", padding: "40px 24px",
          textAlign: "center", color: "var(--text-muted)", fontSize: 13,
        }}>
          No resources match your search/filter. <span className="bc-link" onClick={() => { setSearch(""); setFilter("all"); }}>Clear filters</span>
        </div>
      ) : (
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: "var(--radius-lg)", overflow: "hidden" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border)", textAlign: "left" }}>
                <th style={{ padding: "10px 14px", width: 20 }}></th>
                <th style={{ padding: "10px 14px", color: "var(--text-muted)", fontWeight: 600, fontSize: 11 }}>NAME / ID</th>
                <th style={{ padding: "10px 14px", color: "var(--text-muted)", fontWeight: 600, fontSize: 11 }}>REGION</th>
                <th style={{ padding: "10px 14px", color: "var(--text-muted)", fontWeight: 600, fontSize: 11 }}>STATE</th>
                <th style={{ padding: "10px 14px", color: "var(--text-muted)", fontWeight: 600, fontSize: 11 }}>DISCOVERED</th>
                <th style={{ padding: "10px 14px", width: 40 }}></th>
              </tr>
            </thead>
            <tbody>
              {visible.map((r, i) => (
                <ResourceRow
                  key={r.resource_id || i}
                  r={r}
                  isLast={i === visible.length - 1}
                  accountId={accountId}
                  service={service}
                  timeRange={timeRange}
                  timeRangeLabel={rangeLabel}
                  thresholdMap={thresholdMap}
                  alertInfo={alertLookup(r.resource_id)}
                  autoExpand={!!resourceParam && (r.resource_id === resourceParam || r.name === resourceParam)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
