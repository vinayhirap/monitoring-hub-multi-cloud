// src/pages/Reports.jsx
// CloudOps client/stakeholder report engine UI. Generation runs async
// on the backend (app/reports/worker.py) -- this page enqueues, polls
// job status, then lists/downloads/emails completed reports. Follows
// the same card/table language as Incidents.jsx / Compliance.jsx.
import { useState, useEffect, useCallback } from "react";
import { useAuth } from "../auth/AuthContext";
import {
  getLiveAccounts, generateReport, getReportJobStatus,
  listReports, reportDownloadUrl, emailReport,
  listReportScopeResources, listReportScopeIncidents,
} from "../api/api";
import "./Reports.css";

const REPORT_TYPES = ["WEEKLY", "MONTHLY", "QUARTERLY", "CUSTOM"];
// CLIENT was in the original spec ("select a client/account/resource/
// incident") but this app has no actual "client" entity distinct from
// an AWS account -- no table, no ID scheme, nothing to populate a
// dropdown from or validate a typed value against. Rather than expose
// a scope type with nothing behind it, it's left out of the UI until
// there's a real client concept; app/api/reports.py's ScopeType enum
// still accepts "CLIENT" so the API isn't blocked on this decision.
const SCOPE_TYPES = ["ACCOUNT", "RESOURCE", "INCIDENT"];

export default function Reports() {
  const { hasPermission, hasFeature } = useAuth();
  const [accounts, setAccounts] = useState([]);
  const [reportType, setReportType] = useState("WEEKLY");
  const [scopeType, setScopeType] = useState("ACCOUNT");
  const [accountId, setAccountId] = useState("");
  const [scopeId, setScopeId] = useState("");
  const [scopeResources, setScopeResources] = useState([]);
  const [scopeIncidents, setScopeIncidents] = useState([]);
  const [manualScopeId, setManualScopeId] = useState(false);
  const [periodStart, setPeriodStart] = useState("");
  const [periodEnd, setPeriodEnd] = useState("");
  const [pending, setPending] = useState(null); // { job_id, status }
  const [reports, setReports] = useState([]);
  const [emailTargets, setEmailTargets] = useState({});
  const [error, setError] = useState("");

  const refreshHistory = useCallback(() => {
    listReports({}).then(setReports).catch(() => setReports([]));
  }, []);

  useEffect(() => {
    getLiveAccounts().then(setAccounts).catch(() => setAccounts([]));
    refreshHistory();
  }, [refreshHistory]);

  // Populate the RESOURCE/INCIDENT pick-lists whenever the account or
  // scope type changes. scopeId is reset each time so a stale
  // selection from a different account/scope can't slip through --
  // e.g. picking an incident under Account A, then switching to
  // Account B, must not silently submit Account A's incident id.
  useEffect(() => {
    setScopeId("");
    setManualScopeId(false);
    if (!accountId) { setScopeResources([]); setScopeIncidents([]); return; }
    if (scopeType === "RESOURCE") {
      listReportScopeResources(accountId).then(setScopeResources).catch(() => setScopeResources([]));
    } else if (scopeType === "INCIDENT") {
      listReportScopeIncidents(accountId).then(setScopeIncidents).catch(() => setScopeIncidents([]));
    }
  }, [accountId, scopeType]);

  // Poll a queued job until it completes/fails, then refresh history.
  useEffect(() => {
    if (!pending || pending.status === "COMPLETE" || pending.status === "FAILED") return;
    const t = setInterval(async () => {
      try {
        const job = await getReportJobStatus(pending.job_id);
        setPending(job);
        if (job.status === "COMPLETE" || job.status === "FAILED") {
          clearInterval(t);
          refreshHistory();
        }
      } catch {
        clearInterval(t);
      }
    }, 2500);
    return () => clearInterval(t);
  }, [pending, refreshHistory]);

  async function handleGenerate(e) {
    e.preventDefault();
    setError("");
    if (scopeType === "ACCOUNT" && !accountId) { setError("Select an account."); return; }
    if (scopeType !== "ACCOUNT" && !accountId) { setError("Select the account this resource/incident belongs to first."); return; }
    if (scopeType !== "ACCOUNT" && !scopeId) { setError("Select or enter a resource/incident id."); return; }
    try {
      const resp = await generateReport({
        reportType, scopeType,
        scopeId: scopeType === "ACCOUNT" ? accountId : scopeId,
        accountId: accountId || null,
        periodStart: reportType === "CUSTOM" ? periodStart : undefined,
        periodEnd: reportType === "CUSTOM" ? periodEnd : undefined,
      });
      setPending(resp);
    } catch (err) {
      setError(err.message || "Failed to queue report");
    }
  }

  async function handleEmail(reportId) {
    const to = emailTargets[reportId];
    if (!to) return;
    try {
      await emailReport(reportId, to);
      alert(`Report emailed to ${to}`);
    } catch (err) {
      alert(err.message.includes("501") ? "SMTP is not configured yet -- ask an admin to set SMTP_HOST in .env." : "Email failed.");
    }
  }

  if (!hasFeature("reports")) {
    return <div className="reports-page"><p>Reports is not enabled on this environment.</p></div>;
  }
  if (!hasPermission("reports.view")) {
    return <div className="reports-page"><p>You do not have access to Reports.</p></div>;
  }

  return (
    <div className="reports-page">
      <h1>Reports</h1>
      <p className="reports-sub">Generate professional, stakeholder-ready monitoring/incident reports, stored in S3 for 1 year.</p>

      {hasPermission("reports.generate") && (
        <form className="reports-form" onSubmit={handleGenerate}>
          <div className="reports-form-row">
            <label>Report type
              <select value={reportType} onChange={(e) => setReportType(e.target.value)}>
                {REPORT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
            <label>Scope
              <select value={scopeType} onChange={(e) => setScopeType(e.target.value)}>
                {SCOPE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
            <label>Account
              <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
                <option value="">-- select --</option>
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name ? `${a.name} (${a.account_id})` : a.account_id}
                  </option>
                ))}
              </select>
            </label>
            {scopeType === "RESOURCE" && (
              <label>Resource
                {manualScopeId ? (
                  <input value={scopeId} onChange={(e) => setScopeId(e.target.value)} placeholder="resource id" />
                ) : (
                  <select value={scopeId} onChange={(e) => setScopeId(e.target.value)} disabled={!accountId}>
                    <option value="">{accountId ? "-- select --" : "select an account first"}</option>
                    {scopeResources.map((r) => (
                      <option key={r.resource_id} value={r.resource_id}>
                        [{r.resource_type}] {r.name || r.resource_id}
                      </option>
                    ))}
                  </select>
                )}
                {accountId && (
                  <button type="button" className="reports-link-btn" onClick={() => { setManualScopeId(!manualScopeId); setScopeId(""); }}>
                    {manualScopeId ? "pick from list instead" : "enter id manually"}
                  </button>
                )}
              </label>
            )}
            {scopeType === "INCIDENT" && (
              <label>Incident
                {manualScopeId ? (
                  <input value={scopeId} onChange={(e) => setScopeId(e.target.value)} placeholder="incident id" />
                ) : (
                  <select value={scopeId} onChange={(e) => setScopeId(e.target.value)} disabled={!accountId}>
                    <option value="">{accountId ? "-- select (most recent 50) --" : "select an account first"}</option>
                    {scopeIncidents.map((i) => (
                      <option key={i.id} value={i.id}>
                        #{i.id} [{i.severity}/{i.status}] {i.title}
                      </option>
                    ))}
                  </select>
                )}
                {accountId && (
                  <button type="button" className="reports-link-btn" onClick={() => { setManualScopeId(!manualScopeId); setScopeId(""); }}>
                    {manualScopeId ? "pick from list instead" : "not in list? enter id manually"}
                  </button>
                )}
              </label>
            )}
            {reportType === "CUSTOM" && (
              <>
                <label>From <input type="datetime-local" value={periodStart} onChange={(e) => setPeriodStart(e.target.value)} /></label>
                <label>To <input type="datetime-local" value={periodEnd} onChange={(e) => setPeriodEnd(e.target.value)} /></label>
              </>
            )}
            <button type="submit" disabled={pending && pending.status !== "COMPLETE" && pending.status !== "FAILED"}>
              Generate Report
            </button>
          </div>
          {error && <p className="reports-error">{error}</p>}
          {pending && (
            <p className="reports-status">
              Job #{pending.job_id}: <strong>{pending.status}</strong>
              {pending.status === "FAILED" && pending.error_message ? ` -- ${pending.error_message}` : ""}
            </p>
          )}
        </form>
      )}

      <h2>Report History</h2>
      <table className="reports-table">
        <thead>
          <tr><th>Type</th><th>Scope</th><th>Period</th><th>Generated</th><th>Size</th><th>Expires</th><th></th></tr>
        </thead>
        <tbody>
          {reports.map((r) => (
            <tr key={r.id}>
              <td>{r.report_type}</td>
              <td>{r.scope_type}: {r.scope_label || r.scope_id}</td>
              <td>{new Date(r.period_start).toLocaleDateString()} - {new Date(r.period_end).toLocaleDateString()}</td>
              <td>{new Date(r.generated_at).toLocaleString()}</td>
              <td>{Math.round(r.size_bytes / 1024)} KB</td>
              <td>{new Date(r.expires_at).toLocaleDateString()}</td>
              <td className="reports-actions">
                {hasPermission("reports.download") && (
                  <a href={reportDownloadUrl(r.id)} target="_blank" rel="noreferrer">Download</a>
                )}
                {hasPermission("reports.email") && (
                  <span>
                    <input
                      type="email" placeholder="email@client.com" className="reports-email-input"
                      value={emailTargets[r.id] || ""}
                      onChange={(e) => setEmailTargets({ ...emailTargets, [r.id]: e.target.value })}
                    />
                    <button type="button" onClick={() => handleEmail(r.id)}>Send</button>
                  </span>
                )}
              </td>
            </tr>
          ))}
          {reports.length === 0 && <tr><td colSpan={7}>No reports generated yet.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}
