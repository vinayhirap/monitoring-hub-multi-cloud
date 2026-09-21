// src/pages/Reports.jsx
// CloudOps client/stakeholder report engine UI. Generation runs async
// on the backend (app/reports/worker.py) -- this page enqueues, polls
// job status, then lists/downloads/emails completed reports.
//
// 2026-09-20: rebuilt to match the rest of the app's actual design
// system instead of ad-hoc styling -- .c-header/.hl/.sub and
// .c-btn/.c-btn-primary (from Compliance.css, reused app-wide since
// Vite bundles all page CSS into one file) for the page header and
// buttons, and the same filters/card/bar/table/badge shape
// SecurityFindings.jsx and Compliance.jsx already use, just with a
// reports-specific class prefix. Also fixed: the Account dropdown was
// reading a.name, which doesn't exist on GET /api/live/accounts's
// response (the real field is account_name, confirmed against that
// endpoint's own query) -- it silently fell back to the bare AWS
// account number every time.
import { useState, useEffect, useCallback } from "react";
import { useAuth } from "../auth/AuthContext";
import { useTimezone } from "../contexts/TimezoneContext";
import {
  getLiveAccounts, generateReport, getReportJobStatus,
  listReports, reportDownloadUrl, emailReport,
  listReportScopeResources, listReportScopeIncidents,
} from "../api/api";
import { ReportIcon, DownloadIcon } from "../components/icons";
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
  const { ianaName } = useTimezone();
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
  // Self-scheduling poll (setTimeout, not setInterval) keyed only on
  // the job id -- NOT on the whole `pending` object, so a status
  // update mid-poll doesn't tear down and rebuild the loop every
  // 2.5s. Tolerates a handful of transient failures (one flaky
  // request no longer kills polling silently, which is exactly what
  // was happening before: a single network hiccup hit the catch
  // block, called clearInterval, and never resumed -- from then on
  // the status just sat on PROCESSING until someone manually
  // reloaded the page).
  useEffect(() => {
    if (!pending || pending.status === "COMPLETE" || pending.status === "FAILED") return;
    let cancelled = false;
    let consecutiveErrors = 0;
    const jobId = pending.job_id;

    async function poll() {
      if (cancelled) return;
      try {
        const job = await getReportJobStatus(jobId);
        consecutiveErrors = 0;
        if (cancelled) return;
        setPending(job);
        if (job.status === "COMPLETE" || job.status === "FAILED") {
          refreshHistory();
          return; // terminal state -- stop polling
        }
      } catch {
        consecutiveErrors += 1;
        if (consecutiveErrors >= 6) { // ~15s of consecutive failures
          if (!cancelled) {
            setPending((p) => p && ({
              ...p, status: "UNKNOWN",
              error_message: "Lost connection while checking status -- reload the page to see the latest state.",
            }));
          }
          return;
        }
      }
      if (!cancelled) setTimeout(poll, 2500);
    }
    const t = setTimeout(poll, 2500);
    return () => { cancelled = true; clearTimeout(t); };
  }, [pending?.job_id, pending?.status, refreshHistory]);

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
    return (
      <div className="reports-page">
        <div className="c-header"><div><h1>Reports</h1></div></div>
        <div className="reports-empty-state">Reports is not enabled on this environment.</div>
      </div>
    );
  }
  if (!hasPermission("reports.view")) {
    return (
      <div className="reports-page">
        <div className="c-header"><div><h1>Reports</h1></div></div>
        <div className="reports-empty-state">You do not have access to Reports.</div>
      </div>
    );
  }

  const fmt = (d) => new Date(d).toLocaleString("en-US", { timeZone: ianaName });
  const fmtDate = (d) => new Date(d).toLocaleDateString("en-US", { timeZone: ianaName });

  return (
    <div className="reports-page">
      <div className="c-header">
        <div>
          <h1>CloudOps <span className="hl">Reports</span></h1>
          <p className="sub">Generate professional, stakeholder-ready monitoring/incident reports, stored in S3 for 1 year.</p>
        </div>
      </div>

      {hasPermission("reports.generate") && (
        <form className="reports-card" onSubmit={handleGenerate}>
          <div className="reports-bar">
            <ReportIcon size={13} />
            <span className="bar-title">GENERATE REPORT</span>
          </div>
          <div className="reports-filters">
            <div className="reports-field">
              <label>Report type</label>
              <select value={reportType} onChange={(e) => setReportType(e.target.value)}>
                {REPORT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <div className="reports-field">
              <label>Scope</label>
              <select value={scopeType} onChange={(e) => setScopeType(e.target.value)}>
                {SCOPE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <div className="reports-field">
              <label>Account</label>
              <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
                <option value="">-- select --</option>
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.account_name ? `${a.account_name} (${a.account_id})` : a.account_id}
                  </option>
                ))}
              </select>
            </div>
            {scopeType === "RESOURCE" && (
              <div className="reports-field">
                <label>Resource</label>
                <div className="reports-field-inline">
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
                    <button
                      type="button" className="reports-toggle-btn"
                      title={manualScopeId ? "Pick from list instead" : "Enter ID manually"}
                      onClick={(e) => { e.currentTarget.blur(); setManualScopeId(!manualScopeId); setScopeId(""); }}
                    >
                      {manualScopeId ? "☰" : "✎"}
                    </button>
                  )}
                </div>
              </div>
            )}
            {scopeType === "INCIDENT" && (
              <div className="reports-field">
                <label>Incident</label>
                <div className="reports-field-inline">
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
                    <button
                      type="button" className="reports-toggle-btn"
                      title={manualScopeId ? "Pick from list instead" : "Not in the list? Enter the ID manually"}
                      onClick={(e) => { e.currentTarget.blur(); setManualScopeId(!manualScopeId); setScopeId(""); }}
                    >
                      {manualScopeId ? "☰" : "✎"}
                    </button>
                  )}
                </div>
              </div>
            )}
            {reportType === "CUSTOM" && (
              <>
                <div className="reports-field">
                  <label>From</label>
                  <input type="datetime-local" value={periodStart} onChange={(e) => setPeriodStart(e.target.value)} />
                </div>
                <div className="reports-field">
                  <label>To</label>
                  <input type="datetime-local" value={periodEnd} onChange={(e) => setPeriodEnd(e.target.value)} />
                </div>
              </>
            )}
            <div className="reports-field reports-field-submit">
              <label>&nbsp;</label>
              <button type="submit" className="c-btn-primary" disabled={pending && !["COMPLETE", "FAILED", "UNKNOWN"].includes(pending.status)}>
                Generate Report
              </button>
            </div>
          </div>
          {error && <div className="reports-error">{error}</div>}
          {pending && (
            <div className={`reports-status reports-status-${pending.status.toLowerCase()}`}>
              {pending.status === "QUEUED" && <><span className="reports-spinner" /> Queued -- waiting to start...</>}
              {pending.status === "PROCESSING" && <><span className="reports-spinner" /> Generating your report...</>}
              {pending.status === "COMPLETE" && <>Report ready -- see it in the history below.</>}
              {pending.status === "FAILED" && <>Report generation failed{pending.error_message ? `: ${pending.error_message}` : "."}</>}
              {pending.status === "UNKNOWN" && <>{pending.error_message}</>}
            </div>
          )}
        </form>
      )}

      <div className="reports-card">
        <div className="reports-bar">
          <span className="bar-title">REPORT HISTORY</span>
          <span className="bar-count">{reports.length}</span>
        </div>
        {reports.length === 0 ? (
          <div className="reports-empty">No reports generated yet.</div>
        ) : (
          <table className="reports-table">
            <thead>
              <tr>
                <th>Type</th><th>Scope</th><th>Period</th><th>Generated</th><th>Size</th><th>Expires</th><th></th>
              </tr>
            </thead>
            <tbody>
              {reports.map((r) => (
                <tr key={r.id}>
                  <td>{r.report_type}</td>
                  <td>{r.scope_type}: {r.scope_label || r.scope_id}</td>
                  <td className="mono">{fmtDate(r.period_start)} - {fmtDate(r.period_end)}</td>
                  <td className="mono">{fmt(r.generated_at)}</td>
                  <td className="mono">{Math.round(r.size_bytes / 1024)} KB</td>
                  <td className="mono">{fmtDate(r.expires_at)}</td>
                  <td className="reports-actions">
                    {hasPermission("reports.download") && (
                      <a className="c-btn" href={reportDownloadUrl(r.id)} target="_blank" rel="noreferrer">
                        <DownloadIcon size={12} /> Download
                      </a>
                    )}
                    {hasPermission("reports.email") && (
                      <span className="reports-email-row">
                        <input
                          type="email" placeholder="email@client.com" className="reports-email-input"
                          value={emailTargets[r.id] || ""}
                          onChange={(e) => setEmailTargets({ ...emailTargets, [r.id]: e.target.value })}
                        />
                        <button type="button" className="c-btn" onClick={() => handleEmail(r.id)}>Send</button>
                      </span>
                    )}
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
