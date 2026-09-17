// monitoring-hub/frontend/src/pages/GenericServiceDetail.jsx
//
// Detail page for any service that does NOT have a bespoke, hand-built
// page like ServiceDetail.jsx's EC2/EBS/RDS/S3/ECS/ELB/Lambda views —
// i.e. every AWS-extended service (DynamoDB, SQS, CloudFront, EKS,
// Redshift, ...) and every GCP/Azure service. Rendered by
// ServiceDetailRouter.jsx whenever ServiceDetail.hasCoreDetailPage()
// says no bespoke page exists for the requested service key.
//
// Resource list sourced from GET /api/live/resources-list/{id}/{svc}
// (the shared `resources` table every provider's discovery pipeline
// writes into). Expanding a row fetches
// GET /api/live/metrics/generic/{id}/{svc}/{resourceId} — real charts
// from the same metric_history table every bespoke chart in this app
// reads from (see that endpoint's backend comment), not a placeholder.
// Charts are per-resource and lazy (fetched on first expand, not for
// every row up front) since a service can have a large resource count.
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { getResourcesList, getGenericMetrics, getConsoleUrl } from "../api/api";
import { CloudServiceIcon } from "../components/cloud-icons";
import { ArrowLeftIcon, ExternalLinkIcon, ChevronDownIcon } from "../components/icons";
import { useTimezone } from "../contexts/TimezoneContext";

async function fetchAccount(id) {
  const res = await fetch(`/api/admin/accounts/${id}`);
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
}

function MetricChart({ name, metric }) {
  const data = metric.series.map(p => ({ t: new Date(p.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), v: p.v }));
  return (
    <div style={{ background: "var(--bg-elevated, #10151c)", border: "1px solid var(--border)", borderRadius: "var(--radius)", padding: "10px 12px" }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 2 }}>{name}</div>
      {metric.description && (
        <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 6 }}>{metric.description}{metric.unit ? ` (${metric.unit})` : ""}</div>
      )}
      <ResponsiveContainer width="100%" height={110}>
        <LineChart data={data} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
          <XAxis dataKey="t" tick={{ fontSize: 9, fill: "var(--text-muted)" }} interval="preserveStartEnd" />
          <YAxis tick={{ fontSize: 9, fill: "var(--text-muted)" }} width={36} />
          <Tooltip contentStyle={{ background: "var(--bg-card)", border: "1px solid var(--border)", fontSize: 11 }} />
          <Line type="monotone" dataKey="v" stroke="var(--accent)" dot={false} strokeWidth={1.5} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function ResourceRow({ r, isLast, accountId, service }) {
  const { ianaName } = useTimezone();
  const [expanded, setExpanded] = useState(false);
  const [metrics, setMetrics] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  function toggle() {
    const next = !expanded;
    setExpanded(next);
    if (next && metrics === null && !loading) {
      setLoading(true);
      setError(null);
      getGenericMetrics(accountId, service, r.resource_id)
        .then(data => setMetrics(data || {}))
        .catch(e => setError(e.message || "Failed to load metrics"))
        .finally(() => setLoading(false));
    }
  }

  const metricNames = metrics ? Object.keys(metrics) : [];

  return (
    <>
      <tr
        onClick={toggle}
        style={{ borderBottom: (isLast && !expanded) ? "none" : "1px solid var(--border)", cursor: "pointer" }}
      >
        <td style={{ padding: "10px 14px", width: 20 }}>
          <ChevronDownIcon size={13} style={{ color: "var(--text-muted)", transform: expanded ? "rotate(0deg)" : "rotate(-90deg)", transition: "transform .15s" }} />
        </td>
        <td style={{ padding: "10px 14px" }}>
          <div style={{ fontWeight: 600 }}>{r.name || r.resource_id}</div>
          {r.name && r.name !== r.resource_id && (
            <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{r.resource_id}</div>
          )}
        </td>
        <td style={{ padding: "10px 14px", color: "var(--text-muted)" }}>{r.region || "—"}</td>
        <td style={{ padding: "10px 14px", color: "var(--text-muted)" }}>{r.instance_state || "—"}</td>
        <td style={{ padding: "10px 14px", color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: 11 }}>
          {r.created_at ? new Date(r.created_at).toLocaleString("en-US", { timeZone: ianaName }) : "—"}
        </td>
      </tr>
      {expanded && (
        <tr style={{ borderBottom: isLast ? "none" : "1px solid var(--border)" }}>
          <td colSpan={5} style={{ padding: "0 14px 14px 40px", background: "rgba(255,255,255,.015)" }}>
            {loading ? (
              <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "10px 0" }}>Loading metrics…</div>
            ) : error ? (
              <div style={{ fontSize: 12, color: "var(--red)", padding: "10px 0" }}>{error}</div>
            ) : metricNames.length === 0 ? (
              <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "10px 0" }}>
                No metric data collected yet for this resource. If you expect a metric here, confirm
                it's enabled for this service in <b style={{ color: "var(--text-secondary)" }}>Settings → Metrics</b>.
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 10, paddingTop: 10 }}>
                {metricNames.map(name => <MetricChart key={name} name={name} metric={metrics[name]} />)}
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
  const [account, setAccount] = useState(null);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [consoleLoading, setConsoleLoading] = useState(false);

  useEffect(() => {
    fetchAccount(accountId).then(setAccount).catch(() => {});
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

  const provider = account?.provider || "aws";

  return (
    <div style={{ maxWidth: 1100 }}>
      <div className="breadcrumb">
        <span className="bc-link" onClick={() => navigate("/overview")}>ALL ACCOUNTS</span>
        <span className="bc-sep">›</span>
        <span className="bc-link" onClick={() => navigate(`/accounts/${accountId}`)}>{account?.account_name ?? `Account ${accountId}`}</span>
        <span className="bc-sep">›</span>
        <span className="bc-link" onClick={() => navigate(`/accounts/${accountId}/services`)}>SERVICES</span>
        <span className="bc-sep">›</span>
        <span className="bc-current">{(label || service || "").toUpperCase()}</span>
      </div>

      <div style={{ marginBottom: 24, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <button
            onClick={() => navigate(`/accounts/${accountId}/services`)}
            style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", display: "flex" }}
            aria-label="Back to services"
          >
            <ArrowLeftIcon size={18} />
          </button>
          <CloudServiceIcon provider={provider} service={service} size={22} />
          <h1 style={{ fontSize: 22, fontWeight: 700, letterSpacing: "-0.01em" }}>{label || service}</h1>
          <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
            {rows.length} resource{rows.length === 1 ? "" : "s"}
          </span>
        </div>
        <button
          onClick={openInConsole}
          disabled={consoleLoading}
          style={{
            display: "flex", alignItems: "center", gap: 6, background: "var(--accent-dim)",
            border: "1px solid rgba(43,179,172,.3)", color: "var(--accent)", padding: "8px 16px",
            borderRadius: "var(--radius)", fontSize: 13, fontWeight: 600, cursor: "pointer",
          }}
        >
          <ExternalLinkIcon size={14} /> {consoleLoading ? "Opening…" : "Open in Console"}
        </button>
      </div>

      <div style={{
        background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: "var(--radius-lg)",
        padding: "12px 16px", marginBottom: 16, fontSize: 12, color: "var(--text-muted)",
      }}>
        This service doesn't have a dedicated page yet — click a resource row below to expand its
        metric charts (same underlying data as every other page in this app). Metric collection and
        alerts work normally for anything enabled in{" "}
        <b style={{ color: "var(--text-secondary)" }}>Settings → Metrics</b>; check the{" "}
        <span className="bc-link" onClick={() => navigate(`/accounts/${accountId}/incidents`)}>Alerts</span> page
        for this account to see anything currently firing.
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
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <ResourceRow key={r.resource_id || i} r={r} isLast={i === rows.length - 1} accountId={accountId} service={service} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

