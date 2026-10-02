// src/pages/Reports.jsx -- Reports + Analytics Explorer.
// Flow: 1 Template -> 2 Scope and period -> 3 Review and generate -> status -> Report library.
// Same endpoints, permissions and feature flag as before (generate / job status / list / download / email).
// The backend has no content templates, scheduling or job-list endpoint; templates here are presets of the
// real parameters, and "Recent requests" are the jobs started from this browser (status read from the API).
import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useAuth } from "../auth/AuthContext";
import { useTimezone } from "../contexts/TimezoneContext";
import {
  getLiveAccounts, generateReport, getReportJobStatus, listReports, reportDownloadUrl, emailReport,
  listReportScopeResources, listReportScopeIncidents,
} from "../api/api";
import { getCached, setCached } from "../utils/dataCache";
import { DownloadIcon } from "../components/icons";
import { PageHeader, Panel, Badge, StatusBeacon, SegmentedControl, FilterBar, DataTable, EmptyState } from "../components/ui";
import {
  CONTENT, PERIODS, templatesFor, quickRanges, periodWindow, validateRequest, jobIsTerminal, trackJob, updateJob,
  describeRequest, filterLibrary, expiryState, fmtSize,
} from "../utils/reportPlan";
import "./Reports.css";

const JOBS_KEY = "reports:jobs";
const STATUS_TONE = { QUEUED: "mute", PROCESSING: "info", COMPLETE: "ok", FAILED: "crit", UNKNOWN: "warn" };

export default function Reports() {
  const { hasPermission, hasFeature, user } = useAuth();
  const { ianaName } = useTimezone();
  const isAdmin = (user?.role || "").toLowerCase() === "admin";
  const canGenerate = hasPermission("reports.generate");
  const templates = useMemo(() => templatesFor(isAdmin), [isAdmin]);

  const [accounts, setAccounts] = useState([]);
  const [tplId, setTplId] = useState("weekly");
  const tpl = templates.find(t => t.id === tplId) || templates[0];
  const [reportType, setReportType] = useState("WEEKLY");
  const [accountId, setAccountId] = useState("");
  const [scopeId, setScopeId] = useState("");
  const [manualId, setManualId] = useState(false);
  const [resources, setResources] = useState([]);
  const [incidents, setIncidents] = useState([]);
  const [pStart, setPStart] = useState("");
  const [pEnd, setPEnd] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [jobs, setJobs] = useState(() => getCached(JOBS_KEY)?.data || []);
  const jobsRef = useRef(jobs);
  const [library, setLibrary] = useState(undefined);        // undefined loading, null unavailable
  const [lf, setLf] = useState({ q: "", type: "all", account: "all" });
  const [emailTo, setEmailTo] = useState({});
  const [notice, setNotice] = useState("");
  const [libNotice, setLibNotice] = useState(null);       // {kind: "ok"|"err", text}: result of an email, shown next to the library, not at the top of the page

  useEffect(() => { jobsRef.current = jobs; setCached(JOBS_KEY, jobs); }, [jobs]);

  const refreshLibrary = useCallback(() => { listReports({}).then(r => setLibrary(Array.isArray(r) ? r : [])).catch(() => setLibrary(null)); }, []);
  useEffect(() => { getLiveAccounts().then(a => setAccounts(Array.isArray(a) ? a : [])).catch(() => setAccounts([])); refreshLibrary(); }, [refreshLibrary]);

  // Pick a template: sets the real parameters it stands for
  const pickTemplate = t => {
    setTplId(t.id); setReportType(t.reportType); setScopeId(""); setManualId(false); setError(""); setNotice("");
    if (t.reportType === "CUSTOM" && !pStart) { const r = quickRanges()[1]; setPStart(r.start); setPEnd(r.end); }
  };

  const scopeType = tpl?.scopeType || "ACCOUNT";
  // Account names: one row per (account, region) in /api/live/accounts -- the report scope is the DB row id
  const accountOptions = useMemo(() => {
    const seen = new Map();
    accounts.forEach(a => { if (!seen.has(a.id)) seen.set(a.id, a); });
    return [...seen.values()];
  }, [accounts]);
  const acct = accountOptions.find(a => String(a.id) === String(accountId));
  const acctLabel = acct ? (acct.account_name ? `${acct.account_name}${acct.region ? ` · ${acct.region}` : ""}` : acct.account_id) : "";

  useEffect(() => {
    setScopeId(""); setManualId(false);
    if (!accountId) { setResources([]); setIncidents([]); return; }
    if (scopeType === "RESOURCE") listReportScopeResources(accountId).then(r => setResources(Array.isArray(r) ? r : [])).catch(() => setResources([]));
    if (scopeType === "INCIDENT") listReportScopeIncidents(accountId).then(r => setIncidents(Array.isArray(r) ? r : [])).catch(() => setIncidents([]));
  }, [accountId, scopeType]);

  const win = periodWindow({ reportType, start: pStart, end: pEnd });
  const scopeLabel = scopeType === "CLIENT" ? (scopeId ? `Client: ${scopeId} (all accounts)` : "")
    : scopeType === "ACCOUNT" ? (acctLabel ? `Account ${acctLabel}` : "")
    : scopeType === "RESOURCE" ? (scopeId ? `Resource ${resources.find(r => r.resource_id === scopeId)?.name || scopeId}${acctLabel ? ` in ${acctLabel}` : ""}` : "")
    : (scopeId ? `Incident #${scopeId}${acctLabel ? ` in ${acctLabel}` : ""}` : "");
  const problem = validateRequest({ scopeType, reportType, accountId, scopeId: scopeType === "ACCOUNT" ? accountId : scopeId, start: pStart, end: pEnd, isAdmin });
  const summary = describeRequest({ template: tpl, reportType, scopeLabel, window: win, tz: ianaName });

  // Poll every non-terminal tracked job; one self-scheduling loop for all of them
  const live = jobs.filter(j => !jobIsTerminal(j.status)).map(j => j.job_id).join(",");
  useEffect(() => {
    if (!live) return undefined;
    let dead = false, t; const fails = {};
    const tick = async () => {
      for (const id of live.split(",").map(Number)) {
        try {
          const job = await getReportJobStatus(id); fails[id] = 0;
          if (dead) return;
          setJobs(l => updateJob(l, id, { status: job.status, error: job.error_message || null, attempts: job.attempts }));
          if (jobIsTerminal(job.status)) refreshLibrary();
        } catch {
          fails[id] = (fails[id] || 0) + 1;
          if (fails[id] >= 6 && !dead) setJobs(l => updateJob(l, id, { status: "UNKNOWN", error: "Lost connection while checking this job. Reload to see its latest state." }));
        }
      }
      if (!dead) t = setTimeout(tick, 2500);
    };
    t = setTimeout(tick, 1500);
    return () => { dead = true; clearTimeout(t); };
  }, [live, refreshLibrary]);

  async function handleGenerate() {
    setError(""); setNotice("");
    if (problem) { setError(problem); return; }
    setSubmitting(true);
    try {
      const resp = await generateReport({
        reportType, scopeType, scopeId: scopeType === "ACCOUNT" ? accountId : scopeId.trim(),
        accountId: scopeType === "CLIENT" ? null : (accountId || null),
        periodStart: reportType === "CUSTOM" ? pStart : undefined, periodEnd: reportType === "CUSTOM" ? pEnd : undefined,
      });
      setJobs(l => trackJob(l, { job_id: resp.job_id, status: resp.status || "QUEUED", label: summary, requestedAt: Date.now() }));
      setNotice("Queued. It will appear in the library when it is ready.");
    } catch (e) { setError(e.message || "Failed to queue the report"); } finally { setSubmitting(false); }
  }

  async function handleEmail(id) {
    const to = (emailTo[id] || "").trim();
    if (!to) return;
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(to)) { setLibNotice({ kind: "err", text: `"${to}" is not a valid email address.` }); return; }
    try { await emailReport(id, to); setLibNotice({ kind: "ok", text: `Report #${id} emailed to ${to}.` }); setEmailTo(m => ({ ...m, [id]: "" })); refreshLibrary(); }
    catch (e) { setLibNotice({ kind: "err", text: /501/.test(e.message || "") ? "Email delivery isn't set up yet: an admin must configure SMTP on the server." : "Email failed. Check the address and try again." }); }
  }

  if (!hasFeature("reports")) return <div className="reports-page"><PageHeader title="Reports" /><EmptyState title="Reports is not enabled on this environment" /></div>;
  if (!hasPermission("reports.view")) return <div className="reports-page"><PageHeader title="Reports" /><EmptyState title="You do not have access to Reports" /></div>;

  const fmt = d => new Date(d).toLocaleString("en-GB", { timeZone: ianaName, day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  const fmtD = d => new Date(d).toLocaleDateString("en-GB", { timeZone: ianaName, day: "2-digit", month: "short", year: "numeric" });
  const rows = library ? filterLibrary(library, lf) : [];
  const typeChips = [{ key: "all", label: "All", count: library?.length }, ...["WEEKLY", "MONTHLY", "QUARTERLY", "CUSTOM"].map(k => ({ key: k, label: k[0] + k.slice(1).toLowerCase(), count: library?.filter(r => r.report_type === k).length }))];
  const libAccounts = library ? [...new Map(library.filter(r => r.account_id != null).map(r => [r.account_id, accountOptions.find(a => a.id === r.account_id)?.account_name || `Account ${r.account_id}`])).entries()] : [];

  const columns = [
    { key: "report", header: "Report", sort: r => `${r.report_type}${r.scope_label || r.scope_id}`, render: r => (
      <div><b>{r.scope_label || r.scope_id}</b><div className="rp-dim">{r.report_type[0] + r.report_type.slice(1).toLowerCase()} · {r.scope_type.toLowerCase()}</div></div>) },
    { key: "period", header: "Period", sort: r => Date.parse(r.period_start), render: r => <span className="mono">{fmtD(r.period_start)} – {fmtD(r.period_end)}</span> },
    { key: "generated", header: "Generated", sort: r => Date.parse(r.generated_at), render: r => (<div><span className="mono">{fmt(r.generated_at)}</span><div className="rp-dim">{r.generated_by ? `by ${r.generated_by}` : ""}</div></div>) },
    { key: "size", header: "Size", num: true, sort: r => r.size_bytes || 0, render: r => <span className="mono">{fmtSize(r.size_bytes)}</span> },
    { key: "exp", header: "Retention", sort: r => Date.parse(r.expires_at), render: r => { const e = expiryState(r.expires_at);
        return <Badge tone={e.state === "expired" ? "crit" : e.state === "soon" ? "warn" : "mute"}>{e.state === "expired" ? "expired" : e.state === "unknown" ? "—" : `${e.days}d left`}</Badge>; } },
    { key: "act", header: "", render: r => (
      <div className="rp-actions">
        {hasPermission("reports.download") && expiryState(r.expires_at).state !== "expired" && <a className="ui-btn" href={reportDownloadUrl(r.id)} target="_blank" rel="noreferrer"><DownloadIcon size={12} /> Download</a>}
        {hasPermission("reports.email") && expiryState(r.expires_at).state !== "expired" && (
          <span className="rp-mail">
            <input type="email" aria-label={`Email report ${r.id} to`} placeholder="name@company.com" value={emailTo[r.id] || ""} onChange={e => setEmailTo({ ...emailTo, [r.id]: e.target.value })} />
            <button type="button" className="ui-btn" onClick={() => handleEmail(r.id)} disabled={!(emailTo[r.id] || "").trim()}>Send</button>
          </span>)}
        {r.emailed_at && <span className="rp-dim" title={r.emailed_to || ""}>emailed {fmtD(r.emailed_at)}</span>}
      </div>) },
  ];

  return (
    <div className="reports-page">
      <PageHeader title="Reports" subtitle="Generate stakeholder-ready alert and incident reports from real monitoring data. Stored in S3 and retained for one year." />

      {canGenerate && (
        <Panel title="1 · Choose a template" subtitle="A template presets the report type and scope; the contents are listed in step 3">
          <div className="rp-tpls" role="group" aria-label="Report template">
            {templates.map(t => (
              <button key={t.id} type="button" aria-pressed={tplId === t.id} className={`rp-tpl${tplId === t.id ? " is-on" : ""}`} onClick={() => pickTemplate(t)}>
                <b>{t.label}</b><span>{t.blurb}</span>
              </button>))}
          </div>
        </Panel>
      )}

      {canGenerate && (
        <Panel title="2 · Scope and period">
          <div className="rp-form">
            {scopeType !== "CLIENT" && (
              <label className="rp-f"><span>Account</span>
                <select value={accountId} onChange={e => setAccountId(e.target.value)} data-testid="rp-account">
                  <option value="">Select an account</option>
                  {accountOptions.map(a => <option key={a.id} value={a.id}>{a.account_name ? `${a.account_name}${a.region ? ` · ${a.region}` : ""} (${a.account_id})` : a.account_id}</option>)}
                </select></label>)}
            {scopeType === "CLIENT" && (
              <label className="rp-f"><span>Client or stakeholder name</span>
                <input value={scopeId} onChange={e => setScopeId(e.target.value)} placeholder="Shown on the report cover" maxLength={80} />
                <small>Spans every account. Admin only.</small></label>)}
            {scopeType === "RESOURCE" && (
              <label className="rp-f"><span>Resource</span>
                {manualId ? <input value={scopeId} onChange={e => setScopeId(e.target.value)} placeholder="resource id" />
                  : <select value={scopeId} onChange={e => setScopeId(e.target.value)} disabled={!accountId}>
                    <option value="">{accountId ? "Select a resource" : "Select an account first"}</option>
                    {resources.map(r => <option key={r.resource_id} value={r.resource_id}>[{r.resource_type}] {r.name || r.resource_id}</option>)}
                  </select>}
                {accountId && <button type="button" className="rp-link" onClick={() => { setManualId(v => !v); setScopeId(""); }}>{manualId ? "Pick from list" : "Enter an id instead"}</button>}</label>)}
            {scopeType === "INCIDENT" && (
              <label className="rp-f"><span>Incident</span>
                {manualId ? <input value={scopeId} onChange={e => setScopeId(e.target.value)} placeholder="incident id" />
                  : <select value={scopeId} onChange={e => setScopeId(e.target.value)} disabled={!accountId}>
                    <option value="">{accountId ? "Select (most recent 50)" : "Select an account first"}</option>
                    {incidents.map(i => <option key={i.id} value={i.id}>#{i.id} [{i.severity}/{i.status}] {i.title}</option>)}
                  </select>}
                {accountId && <button type="button" className="rp-link" onClick={() => { setManualId(v => !v); setScopeId(""); }}>{manualId ? "Pick from list" : "Not listed? Enter an id"}</button>}</label>)}
            <div className="rp-f"><span>Period</span>
              <SegmentedControl label="Period" value={reportType} onChange={k => { setReportType(k); if (k === "CUSTOM" && !pStart) { const r = quickRanges()[1]; setPStart(r.start); setPEnd(r.end); } }} options={PERIODS.map(p => ({ key: p.key, label: p.label }))} />
            </div>
            {reportType === "CUSTOM" && (
              <div className="rp-custom">
                <div className="rp-chips">{quickRanges().map(r => <button key={r.key} type="button" className="rp-chip" onClick={() => { setPStart(r.start); setPEnd(r.end); }}>{r.label}</button>)}</div>
                <label className="rp-f"><span>From (UTC)</span><input type="datetime-local" value={pStart ? pStart.slice(0, 16) : ""} onChange={e => setPStart(e.target.value ? `${e.target.value}:00+00:00` : "")} /></label>
                <label className="rp-f"><span>To (UTC)</span><input type="datetime-local" value={pEnd ? pEnd.slice(0, 16) : ""} onChange={e => setPEnd(e.target.value ? `${e.target.value}:00+00:00` : "")} /></label>
              </div>)}
          </div>
        </Panel>
      )}

      {canGenerate && (
        <Panel title="3 · Review and generate">
          <div className="rp-review">
            <div>
              <div className="rp-sum" data-testid="rp-summary">{summary}</div>
              <ul className="rp-incl" aria-label="What the report contains">{CONTENT.map(c => <li key={c}>{c}</li>)}</ul>
              <p className="rp-dim">Only data from accounts you can access is included. Delivery by email needs SMTP to be set up on the server; scheduled reports are not available yet.</p>
            </div>
            <div className="rp-go">
              <button type="button" className="c-btn-primary" onClick={handleGenerate} disabled={submitting || !!problem} data-testid="rp-generate">{submitting ? "Queuing…" : "Generate report"}</button>
              {problem && <div className="rp-hint">{problem}</div>}
              {error && <div className="rp-err" role="alert">{error}</div>}
              {notice && <div className="rp-ok" role="status">{notice}</div>}
            </div>
          </div>
        </Panel>
      )}

      {jobs.length > 0 && (
        <Panel title="Recent requests" subtitle="Started from this browser; status comes from the server" actions={<button type="button" className="rp-link" onClick={() => setJobs(l => l.filter(j => !jobIsTerminal(j.status)))}>Clear finished</button>}>
          <ul className="rp-jobs">
            {jobs.map(j => (
              <li key={j.job_id}>
                <Badge tone={STATUS_TONE[j.status] || "mute"}><StatusBeacon tone={STATUS_TONE[j.status] || "mute"} pulse={j.status === "PROCESSING"} />{j.status.toLowerCase()}</Badge>
                <span className="rp-job-l">{j.label}</span>
                <span className="rp-dim">#{j.job_id} · {fmt(j.requestedAt)}</span>
                {j.status === "FAILED" && <span className="rp-err">{j.error || "Generation failed. Contact an administrator."}</span>}
                {j.status === "UNKNOWN" && <span className="rp-err">{j.error}</span>}
              </li>))}
          </ul>
        </Panel>
      )}

      <Panel title="Report library" subtitle="Everything you can access, newest first" flush>
        <div className="rp-libbar">
          <FilterBar chips={typeChips} active={lf.type} onChange={k => setLf({ ...lf, type: k })} search={lf.q} onSearch={q => setLf({ ...lf, q })} placeholder="Search scope, author…"
            right={libAccounts.length > 1 && <select className="ui-input" aria-label="Filter by account" value={lf.account} onChange={e => setLf({ ...lf, account: e.target.value })}><option value="all">All accounts</option>{libAccounts.map(([id, n]) => <option key={id} value={id}>{n}</option>)}</select>} />
        </div>
        {libNotice && <div className={`rp-libnote ${libNotice.kind}`} role={libNotice.kind === "err" ? "alert" : "status"}>{libNotice.text}<button type="button" className="rp-link" onClick={() => setLibNotice(null)} aria-label="Dismiss message">Dismiss</button></div>}
        {library === null ? <EmptyState title="The report library isn't available" body="It couldn't be loaded for your role right now." />
          : <DataTable columns={columns} rows={rows} rowKey={r => r.id} loading={library === undefined} initialSort={{ key: "generated", dir: "desc" }}
              empty={<EmptyState title={library && library.length ? "No reports match these filters" : "No reports yet"} body={library && library.length ? "Clear the search or filters." : canGenerate ? "Choose a template above and generate your first report." : "Ask an Admin or Editor to generate one."} />} />}
      </Panel>
    </div>
  );
}
