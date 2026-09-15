// src/pages/DeployRisk.jsx
// Deploy-risk correlation -- read-only fleet view of recent
// deployments (ingested via POST /api/webhooks/deploy from CI/CD) and
// how many alerts followed each one. See app/api/deploy_risk.py's
// module docstring for the clean/watch/risky classification.
import { useState, useEffect, useCallback, Fragment } from "react";
import { getDeployRisk } from "../api/api";
import { AlertOctagonIcon, ZapIcon } from "../components/icons";
import "./DeployRisk.css";

function RiskBadge({ risk }) {
  const labels = { clean: "Clean", watch: "Watch", risky: "Risky" };
  return <span className={`dr-risk dr-risk-${risk}`}>● {labels[risk] || risk}</span>;
}

export default function DeployRisk() {
  const [deployments, setDeployments] = useState([]);
  const [days, setDays] = useState(7);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(null);

  const load = useCallback(() => {
    getDeployRisk(days).then(setDeployments).catch(e => setError(e.message)).finally(() => setLoading(false));
  }, [days]);
  useEffect(() => { load(); }, [load]);

  const riskyCount = deployments.filter(d => d.risk === "risky").length;

  return (
    <div className="dr-page">
      <div className="c-header">
        <div>
          <h1>Deploy <span className="hl">Risk</span></h1>
          <p className="sub">Recent deployments (from your CI/CD's webhook) alongside how many alerts followed within 45 minutes -- a correlation, not proof, same as every other RCA signal in this app</p>
        </div>
        <div className="dr-field">
          <label>Window</label>
          <select value={days} onChange={e => setDays(Number(e.target.value))}>
            <option value={1}>Last 24 hours</option>
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
          </select>
        </div>
      </div>

      {error && <div className="dr-error"><AlertOctagonIcon size={13} /> {error}</div>}

      <div className="dr-card">
        <div className="dr-bar">
          <span className="bar-icon">▐</span>
          <span className="bar-title">DEPLOYMENTS</span>
          <span className="bar-count">{deployments.length} in window{riskyCount ? ` · ${riskyCount} risky` : ""}</span>
        </div>

        {loading ? (
          <div className="dr-empty">Loading…</div>
        ) : deployments.length === 0 ? (
          <div className="dr-empty">
            No deployments recorded in this window. Point your CI/CD's deploy step at{" "}
            <code>POST /api/webhooks/deploy</code> to start tracking this — see the backend's DEPLOY_WEBHOOK_TOKEN setting.
          </div>
        ) : (
          <table className="dr-table">
            <thead>
              <tr>
                <th>Deployment</th>
                <th>Account</th>
                <th>When</th>
                <th>Risk</th>
                <th>Alerts after</th>
              </tr>
            </thead>
            <tbody>
              {deployments.map(d => (
                <Fragment key={d.id}>
                  <tr className={d.alert_count > 0 ? "dr-row-clickable" : ""}
                      onClick={() => d.alert_count > 0 && setExpanded(expanded === d.id ? null : d.id)}>
                    <td><ZapIcon size={12} /> {d.message}</td>
                    <td>{d.account_name}</td>
                    <td className="mono">{new Date(d.created_at).toLocaleString()}</td>
                    <td><RiskBadge risk={d.risk} /></td>
                    <td className="mono">{d.alert_count}</td>
                  </tr>
                  {expanded === d.id && d.alerts.length > 0 && (
                    <tr className="dr-detail-row">
                      <td colSpan={5}>
                        <div className="dr-detail">
                          {d.alerts.map(a => (
                            <div key={a.id} className="dr-detail-item">
                              <span className={`dr-mini-sev dr-mini-sev-${a.severity.toLowerCase()}`}>{a.severity}</span>
                              {a.metric_name} on <span className="mono">{a.resource_id}</span>
                            </div>
                          ))}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
