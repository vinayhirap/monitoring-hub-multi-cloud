// monitoring-hub/frontend/src/pages/GenericServiceDetail.jsx
//
// Detail page for any service that does NOT have a bespoke, hand-built
// page like ServiceDetail.jsx's EC2/EBS/RDS/S3/ECS/ELB/Lambda views —
// i.e. every AWS-extended service (DynamoDB, SQS, CloudFront, EKS,
// Redshift, ...) and every GCP/Azure service. Rendered by
// ServiceDetailRouter.jsx whenever ServiceDetail.hasCoreDetailPage()
// says no bespoke page exists for the requested service key.
//
// Deliberately generic: one resource table (name, region, tags, state,
// discovered-at) sourced from GET /api/live/resources-list/{id}/{svc},
// which reads the same shared `resources` table every provider's
// discovery pipeline writes into — see the backend comment on
// live_resource_counts in app/api/live_data.py for the full list of
// discovery modules that feed it. No per-service custom rendering, on
// purpose: a bespoke chart-heavy page per extended/GCP/Azure service
// is a real, larger follow-up (needs a per-service metric picker
// against metric_history), not something to fake here with placeholder
// charts.
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getResourcesList, getConsoleUrl } from "../api/api";
import { CloudServiceIcon } from "../components/cloud-icons";
import { ArrowLeftIcon, ExternalLinkIcon } from "../components/icons";

async function fetchAccount(id) {
  const res = await fetch(`/api/admin/accounts/${id}`);
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
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
        This service doesn't have a dedicated metrics chart page yet — this is a live resource
        listing sourced from the same discovery data that powers alerting for this service.
        Metric history and alerts still work normally for any metric enabled for it in{" "}
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
                <th style={{ padding: "10px 14px", color: "var(--text-muted)", fontWeight: 600, fontSize: 11 }}>NAME / ID</th>
                <th style={{ padding: "10px 14px", color: "var(--text-muted)", fontWeight: 600, fontSize: 11 }}>REGION</th>
                <th style={{ padding: "10px 14px", color: "var(--text-muted)", fontWeight: 600, fontSize: 11 }}>STATE</th>
                <th style={{ padding: "10px 14px", color: "var(--text-muted)", fontWeight: 600, fontSize: 11 }}>DISCOVERED</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.resource_id || i} style={{ borderBottom: i === rows.length - 1 ? "none" : "1px solid var(--border)" }}>
                  <td style={{ padding: "10px 14px" }}>
                    <div style={{ fontWeight: 600 }}>{r.name || r.resource_id}</div>
                    {r.name && r.name !== r.resource_id && (
                      <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{r.resource_id}</div>
                    )}
                  </td>
                  <td style={{ padding: "10px 14px", color: "var(--text-muted)" }}>{r.region || "—"}</td>
                  <td style={{ padding: "10px 14px", color: "var(--text-muted)" }}>{r.instance_state || "—"}</td>
                  <td style={{ padding: "10px 14px", color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: 11 }}>
                    {r.created_at ? new Date(r.created_at).toLocaleString() : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
