// src/pages/SecurityFindings.jsx
// Lite CSPM findings -- read-only (system-generated, see
// app/collector/cspm.py). Same list-card shape as OpEvents.jsx (no
// create form -- there's nothing for a person to configure here,
// only findings to review and, implicitly, go fix in AWS).
import { useState, useEffect, useCallback } from "react";
import { getSecurityFindings, getSecurityFindingsSummary } from "../api/api";
import { ShieldIcon, AlertOctagonIcon } from "../components/icons";
import "./SecurityFindings.css";

const CHECK_LABELS = {
  s3_bucket_public: "Public S3 bucket",
  sg_open_to_world: "Security group open to internet",
  ebs_unencrypted: "Unencrypted EBS volume",
  iam_user_no_mfa: "IAM user without MFA",
  iam_stale_access_key: "Stale IAM access key",
};

function SeverityBadge({ severity }) {
  return <span className={`sec-sev sec-sev-${severity.toLowerCase()}`}>● {severity}</span>;
}

export default function SecurityFindings() {
  const [findings, setFindings] = useState([]);
  const [summary, setSummary] = useState([]);
  const [status, setStatus] = useState("open");
  const [severity, setSeverity] = useState("");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    getSecurityFindings(status, severity || null)
      .then(setFindings).catch(e => setError(e.message)).finally(() => setLoading(false));
    getSecurityFindingsSummary().then(setSummary).catch(() => {});
  }, [status, severity]);
  useEffect(() => { load(); }, [load]);

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
                  <td>{f.account_name}</td>
                  <td className="mono">{new Date(f.last_seen_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
