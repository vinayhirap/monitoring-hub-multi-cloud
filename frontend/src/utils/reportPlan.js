// utils/reportPlan.js -- pure helpers for the Reports workflow. No network.
// The backend (app/api/reports.py) takes report_type (WEEKLY|MONTHLY|QUARTERLY|CUSTOM) + scope_type
// (ACCOUNT|RESOURCE|INCIDENT|CLIENT) + scope_id + account_id + period. It has NO content templates:
// every report is the same alert/incident PDF over the chosen period and scope. A "template" here is
// therefore a preset of those real parameters, and says plainly what the PDF contains.

export const CONTENT = [
  "Alert totals for the period: total, critical, warning, still open",
  "Timeline of the most severe alerts",
  "Incident narratives with impact / probable cause and resolution or current status",
];

export const SYNTHETIC_CONTENT = [
  "Availability per check, with status bands (healthy, degraded, poor)",
  "Failed probes per day and average response time per day",
  "Outages: start, end, duration, and any still open",
  "TLS certificates: expiry date, days left, issuer, and renewal status",
  "Recommendations (checks nobody is notified about, certificates due, slow endpoints)",
];

export const COST_CONTENT = [
  "Billed cost of CloudOps's own paid metric calls: total, daily average and highest day",
  "Cost by cloud, by account and per day (per week for long periods)",
  "This month against the same days of last month (actual billed cost, no forecast)",
  "Estimate by polling tier with where-to-cut figures (list price, last 30 days, only when the period ends now)",
];
export const HOST_CONTENT = [
  "Hosts with online state, last check-in and the share of the period they reported",
  "Heartbeat gaps: start, end and duration, and any still open",
  "CPU, memory, swap and fullest-disk use (history is kept 30 days)",
  "Watched services that are not running, and alerts on host metrics",
  "Recommendations (offline hosts, full disks, old agent versions)",
];
export const ENVIRONMENT_CONTENT = [
  "PROD and NON-PROD placement and how each resource was decided, as of now",
  "Resources still waiting for a placement (treated as PROD)",
  "Tag compliance by resource type, and resources missing the four essential tags",
  "Tag compliance trend over the chosen period, from the daily snapshots",
];
export const SECURITY_CONTENT = [
  "Open findings by severity, by type and by age, as of now",
  "Findings first seen and fixed in the period, and the average time to fix",
  "The open findings with their resources (confidential)",
  "Recommendations, starting with high-severity findings open 30 days or more",
];
export const SLO_CONTENT = [
  "Each SLO's target, window, uptime and share of error budget used, as of now",
  "Met, at risk (75% or more used) and breached SLOs, worst first",
  "The bad time each target allows",
];

const CONTENT_BY_SCOPE = { SYNTHETIC: SYNTHETIC_CONTENT, COST: COST_CONTENT, HOST: HOST_CONTENT, ENVIRONMENT: ENVIRONMENT_CONTENT, SECURITY: SECURITY_CONTENT, SLO: SLO_CONTENT };
export const contentFor = tpl => (tpl && CONTENT_BY_SCOPE[tpl.scopeType]) || CONTENT;

/** Scope types that are built from a section of the app, not from alerts. */
export const SECTION_SCOPES = ["SYNTHETIC", "COST", "HOST", "ENVIRONMENT", "SECURITY", "SLO"];
/** Scope types whose scope id is "<account>" or "<account>:<item>" (item picked from a list; empty = whole account). */
export const ITEM_SCOPES = { SYNTHETIC: "check", HOST: "host", SLO: "SLO" };
export const COST_SCOPES = [{ key: "all", label: "All clouds" }, { key: "aws", label: "AWS" }, { key: "azure", label: "Azure" }, { key: "gcp", label: "GCP" }];

/** How the period applies: "period" (figures cover it), "trend" (only a trend uses it), "none" (state as of now; period ignored). */
export const periodMode = scopeType => (scopeType === "SLO" ? "none" : scopeType === "ENVIRONMENT" ? "trend" : "period");
export const PERIOD_NOTES = {
  none: "SLOs are reported as of now: each one is measured over its own rolling window, so no period applies.",
  trend: "Placement and tag figures are as of now. The period sets the range of the compliance trend.",
};
export const reportScopeId = ({ scopeType, accountId, itemId, scopeId }) => {
  if (scopeType === "COST") return scopeId || "all";
  if (scopeType === "ACCOUNT") return String(accountId || "");
  if (ITEM_SCOPES[scopeType]) return itemId ? `${accountId}:${itemId}` : String(accountId || "");
  if (scopeType === "ENVIRONMENT" || scopeType === "SECURITY") return String(accountId || "");
  return String(scopeId || "").trim();
};

export const TEMPLATES = [
  { id: "weekly",    label: "Weekly operations summary", blurb: "Last 7 days for one account", reportType: "WEEKLY",    scopeType: "ACCOUNT" },
  { id: "monthly",   label: "Monthly review",            blurb: "Last 30 days for one account", reportType: "MONTHLY",   scopeType: "ACCOUNT" },
  { id: "quarterly", label: "Quarterly review",          blurb: "Last 90 days for one account", reportType: "QUARTERLY", scopeType: "ACCOUNT" },
  { id: "incident",  label: "Incident report",           blurb: "One incident, with cause and resolution", reportType: "MONTHLY", scopeType: "INCIDENT" },
  { id: "resource",  label: "Resource report",           blurb: "Alert history of one resource", reportType: "MONTHLY", scopeType: "RESOURCE" },
  { id: "synthetic", label: "Synthetic checks report", blurb: "Uptime, outages and certificates for one account or one check", reportType: "MONTHLY", scopeType: "SYNTHETIC" },
  { id: "cost",        label: "Cloud cost report",          blurb: "Billed cost of CloudOps, by cloud, account and day; admin only", reportType: "MONTHLY", scopeType: "COST", adminOnly: true },
  { id: "host",        label: "Host agents report",         blurb: "Servers: uptime, heartbeat gaps, CPU, memory, disk, services", reportType: "MONTHLY", scopeType: "HOST" },
  { id: "environment", label: "Environments and tags",      blurb: "PROD / NON-PROD placement and tag compliance", reportType: "MONTHLY", scopeType: "ENVIRONMENT" },
  { id: "security",    label: "Security findings report",   blurb: "Open findings, ageing, what was fixed", reportType: "MONTHLY", scopeType: "SECURITY" },
  { id: "slo",         label: "SLO report",                 blurb: "Error budget and attainment of each SLO, as of now", reportType: "MONTHLY", scopeType: "SLO" },
  { id: "client",    label: "Stakeholder / client report", blurb: "All accounts, admin only", reportType: "MONTHLY", scopeType: "CLIENT", adminOnly: true },
  { id: "custom",    label: "Custom period",             blurb: "Any range up to 400 days", reportType: "CUSTOM", scopeType: "ACCOUNT" },
];

export const PERIODS = [
  { key: "WEEKLY", label: "7 days", days: 7 }, { key: "MONTHLY", label: "30 days", days: 30 },
  { key: "QUARTERLY", label: "90 days", days: 90 }, { key: "CUSTOM", label: "Custom" },
];

/** hasPerm(code) -> bool. A section template needs the permission of the section it reads (the server checks it too). */
export const TEMPLATE_PERMS = { SYNTHETIC: "synthetic.view", HOST: "hostagents.view", ENVIRONMENT: "environments.view", SECURITY: "security.view", SLO: "slo.view" };
export const templatesFor = (isAdmin, hasPerm = () => true) => TEMPLATES.filter(t => (!t.adminOnly || isAdmin) && (!TEMPLATE_PERMS[t.scopeType] || hasPerm(TEMPLATE_PERMS[t.scopeType])));

const pad = n => String(n).padStart(2, "0");
const iso = d => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}+00:00`;

/** One-click CUSTOM ranges (UTC, ISO-8601 with offset, which the backend's fromisoformat accepts). */
export function quickRanges(now = new Date()) {
  const y = now.getUTCFullYear(), m = now.getUTCMonth();
  const ago = h => new Date(now.getTime() - h * 3600e3);
  return [
    { key: "24h", label: "Last 24 hours", start: iso(ago(24)), end: iso(now) },
    { key: "14d", label: "Last 14 days", start: iso(ago(14 * 24)), end: iso(now) },
    { key: "mtd", label: "Month to date", start: iso(new Date(Date.UTC(y, m, 1))), end: iso(now) },
    { key: "prev", label: "Previous month", start: iso(new Date(Date.UTC(y, m - 1, 1))), end: iso(new Date(Date.UTC(y, m, 1))) },
  ];
}

/** Date window the backend will use, for display. */
export function periodWindow({ reportType, start, end }, now = new Date()) {
  const p = PERIODS.find(x => x.key === reportType);
  if (p && p.days) return { start: new Date(now.getTime() - p.days * 86400e3), end: now };
  const s = Date.parse(start), e = Date.parse(end);
  return Number.isFinite(s) && Number.isFinite(e) ? { start: new Date(s), end: new Date(e) } : null;
}

/** Mirrors the backend's validation so the user hears about problems before submitting. Returns a message or null. */
export function validateRequest({ scopeType, reportType, accountId, scopeId, start, end, isAdmin }) {
  if (scopeType === "COST") {
    if (!isAdmin) return "Cost reports cover every account and are admin-only.";
    if (!COST_SCOPES.some(c => c.key === (scopeId || "all"))) return "Choose all clouds, AWS, Azure or GCP.";
  } else if (scopeType === "CLIENT") {
    if (!isAdmin) return "Client reports span every account and are admin-only.";
    if (!String(scopeId || "").trim()) return "Enter the client or stakeholder name for the report cover.";
  } else {
    if (!accountId) return "Select an account.";
    if ((scopeType === "RESOURCE" || scopeType === "INCIDENT") && !String(scopeId || "").trim()) return `Select ${scopeType === "RESOURCE" ? "a resource" : "an incident"}.`;
  }
  if (reportType === "CUSTOM" && periodMode(scopeType) !== "none") {
    const s = Date.parse(start), e = Date.parse(end);
    if (!Number.isFinite(s) || !Number.isFinite(e)) return "Choose both a start and an end for the custom period.";
    if (e <= s) return "The end must be after the start.";
    if (e - s > 400 * 86400e3) return "A custom period can be at most about 400 days.";
  }
  return null;
}

export const jobIsTerminal = s => s === "COMPLETE" || s === "FAILED" || s === "UNKNOWN";

/** Locally tracked requests (this browser only; the API has no job-list endpoint). Newest first, capped. */
export function trackJob(list, job, cap = 12) {
  const rest = (list || []).filter(j => j.job_id !== job.job_id);
  return [job, ...rest].slice(0, cap);
}
export function updateJob(list, jobId, patch) {
  return (list || []).map(j => (j.job_id === jobId ? { ...j, ...patch } : j));
}

export function describeRequest({ template, reportType, scopeLabel, window: w, tz = "UTC", asOf = false }) {
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];   // fixed names: locale month abbreviations differ between engines
  const fmt = d => {
    const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: tz, day: "2-digit", month: "numeric", year: "numeric" }).formatToParts(d).map(x => [x.type, x.value]));
    return `${p.day} ${MON[Number(p.month) - 1]} ${p.year}`;
  };
  const span = w ? `${fmt(w.start)} \u2013 ${fmt(w.end)}` : asOf ? "as of now" : "period not set";
  return `${template?.label || reportType} \u00b7 ${scopeLabel || "no scope selected"} \u00b7 ${span}`;
}

/** Filter/sort of the report library. */
export function filterLibrary(rows, { q = "", type = "all", account = "all" } = {}) {
  const needle = q.trim().toLowerCase();
  return (rows || []).filter(r => (type === "all" || r.report_type === type) && (account === "all" || String(r.account_id) === String(account))
    && (!needle || `${r.scope_type} ${r.scope_label || ""} ${r.scope_id || ""} ${r.generated_by || ""} ${r.report_type}`.toLowerCase().includes(needle)));
}

export function expiryState(expiresAt, now = Date.now()) {
  const t = Date.parse(expiresAt);
  if (!Number.isFinite(t)) return { state: "unknown" };
  const days = Math.ceil((t - now) / 86400e3);
  return days <= 0 ? { state: "expired", days } : days <= 14 ? { state: "soon", days } : { state: "ok", days };
}

export const fmtSize = b => (b == null ? "\u2014" : b < 1024 ? `${b} B` : b < 1048576 ? `${Math.round(b / 1024)} KB` : `${(b / 1048576).toFixed(1)} MB`);
