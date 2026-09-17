// src/pages/SecurityFindings.jsx
// Lite CSPM findings -- read-only (system-generated, see
// app/collector/cspm.py). Same list-card shape as OpEvents.jsx (no
// create form -- there's nothing for a person to configure here,
// only findings to review and either go fix directly via the console
// link below, or in AWS/Azure/GCP's own security tooling).
//
// 2026-09-17: added an Account filter (previously the page could only
// be narrowed by status/severity -- every other scoped account_id-
// bearing account_id in this app already gets a per-account filter
// except this one), a Provider column (findings from Azure/GCP
// accounts were indistinguishable from AWS ones in this table even
// though app/collector/cspm.py now checks all three), and a per-row
// "Open in console" action wired to the new
// /api/security-findings/{id}/console-url endpoint.
import { useState, useEffect, useCallback } from "react";
import {
  getSecurityFindings, getSecurityFindingsSummary,
  getSecurityFindingsAccounts, getSecurityFindingConsoleUrl,
} from "../api/api";
import { ShieldIcon, AlertOctagonIcon, ExternalLinkIcon } from "../components/icons";
import "./SecurityFindings.css";

const CHECK_LABELS = {
  s3_bucket_public: "Public S3 bucket",
  sg_open_to_world: "Security group open to internet",
  ebs_unencrypted: "Unencrypted EBS volume",
  iam_user_no_mfa: "IAM user without MFA",
  iam_stale_access_key: "Stale IAM access key",
  azure_nsg_open_to_world: "NSG open to internet",
  azure_storage_public_access: "Storage account allows public blob access",
  azure_storage_insecure_transport: "Storage account allows unencrypted (HTTP) access",
  gcp_firewall_open_to_world: "Firewall rule open to internet",
  gcp_gcs_bucket_public: "Public GCS bucket",
};

const PROVIDER_LABELS = { aws: "AWS", azure: "Azure", gcp: "GCP" };

function SeverityBadge({ severity }) {
  return <span className={`sec-sev sec-sev-${severity.toLowerCase()}`}>● {severity}</span>;
}

function ProviderBadge({ provider }) {
  if (!provider) return null;
  return <span className={`sec-provider sec-provider-${provider}`}>{PROVIDER_LABELS[provider] || provider}</span>;
}

export default function SecurityFindings() {
  const [findings, setFindings] = useState([]);
  const [summary, setSummary] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [status, setStatus] = useState("open");
  const [severity, setSeverity] = useState("");
  const [accountId, setAccountId] = useState("");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [openingConsole, setOpeningConsole] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    getSecurityFindings(status, severity || null, accountId || null)
      .then(setFindings).catch(e => setError(e.message)).finally(() => setLoading(false));
    getSecurityFindingsSummary().then(setSummary).catch(() => {});
  }, [status, severity, accountId]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { getSecurityFindingsAccounts().then(setAccounts).catch(() => {}); }, []);

  // Opens THIS finding's resource in THIS finding's account, on
  // whichever cloud it actually lives on -- same two-step
  // synchronous-tab-open pattern as Alerts.jsx's openConsole (avoids
  // the popup blocker, and severs window.opener so the opened tab
  // can't navigate this page back -- see that file's comment for the
  // full reverse-tabnabbing rationale, unchanged here).
  async function openConsole(findingId) {
    const tab = window.open("", "_blank");
    if (tab) tab.opener = null;
    setOpeningConsole(findingId);
    try {
      const { url } = await getSecurityFindingConsoleUrl(findingId);
      if (tab) tab.location.href = url;
      else window.open(url, "_blank", "noopener,noreferrer");
    } catch (e) {
      if (tab) tab.close();
      alert("Couldn't open console: " + e.message);
    } finally {
      setOpeningConsole(null);
    }
  }

  const totalOpen = summary.reduce((sum, s) => sum + s.total_open, 0);
  const totalHigh = summary.reduce((sum, s) => sum + (s.high_count || 0), 0);

  return (
    <div className="sec-page">
      <div className="c-header">
        <div>
          <h1>Security <span className="hl">Findings</span></h1>
          <p className="sub">Public buckets, security groups open to the internet, unencrypted volumes, and IAM hygiene -- checked hourly across every onboarded account</p>
        </div>
        <div className="sec-summary-chip">
          <ShieldIcon size={16} />
          <span>{totalOpen} open{totalHigh ? `, ${totalHigh} high severity` : ""}</span>
        </div>
      </div>

      {error && <div className="sec-error"><AlertOctagonIcon size={13} /> {error}</div>}

      <div className="sec-filters">
        <div className="sec-field">
          <label>Status</label>
          <select value={status} onChange={e => setStatus(e.target.value)}>
            <option value="open">Open</option>
            <option value="resolved">Resolved</option>
            <option value="all">All</option>
          </select>
        </div>
        <div className="sec-field">
          <label>Severity</label>
          <select value={severity} onChange={e => setSeverity(e.target.value)}>
            <option value="">All</option>
            <option value="HIGH">High</option>
            <option value="MEDIUM">Medium</option>
            <option value="LOW">Low</option>
          </select>
        </div>
        <div className="sec-field">
          <label>Account</label>
          <select value={accountId} onChange={e => setAccountId(e.target.value)}>
            <option value="">All accounts</option>
            {accounts.map(a => (
              <option key={a.account_id} value={a.account_id}>
                {a.account_name}{a.provider ? ` (${PROVIDER_LABELS[a.provider] || a.provider})` : ""}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="sec-card">
        <div className="sec-bar">
          <span className="bar-icon">▐</span>
          <span className="bar-title">FINDINGS</span>
          <span className="bar-count">{findings.length}</span>
        </div>

        {loading ? (
          <div className="sec-empty">Loading…</div>
        ) : findings.length === 0 ? (
          <div className="sec-empty">No {status === "all" ? "" : status} findings — nice work, or checks haven't run yet (they need extra IAM permissions on the monitoring role, see the deployment notes).</div>
        ) : (
          <table className="sec-table">
            <thead>
              <tr>
                <th>Severity</th>
                <th>Finding</th>
                <th>Resource</th>
                <th>Account</th>
                <th>Last seen</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {findings.map(f => (
                <tr key={f.id}>
                  <td><SeverityBadge severity={f.severity} /></td>
                  <td>
                    <div className="sec-title">{CHECK_LABELS[f.check_id] || f.check_id}</div>
                    <div className="sec-desc">{f.description}</div>
                  </td>
                  <td className="mono sec-resource">{f.resource_id}</td>
                  <td>
                    <div>{f.account_name}</div>
                    <ProviderBadge provider={f.account_provider} />
                  </td>
                  <td className="mono">{new Date(f.last_seen_at).toLocaleString()}</td>
                  <td>
                    <button
                      className="btn-console-sec"
                      disabled={openingConsole === f.id}
                      onClick={() => openConsole(f.id)}
                      title="Open this resource in its cloud console"
                    >
                      {openingConsole === f.id
                        ? "Opening…"
                        : <><ExternalLinkIcon size={12} /> Console</>}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
