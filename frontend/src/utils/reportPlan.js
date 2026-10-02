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

export const TEMPLATES = [
  { id: "weekly",    label: "Weekly operations summary", blurb: "Last 7 days for one account", reportType: "WEEKLY",    scopeType: "ACCOUNT" },
  { id: "monthly",   label: "Monthly review",            blurb: "Last 30 days for one account", reportType: "MONTHLY",   scopeType: "ACCOUNT" },
  { id: "quarterly", label: "Quarterly review",          blurb: "Last 90 days for one account", reportType: "QUARTERLY", scopeType: "ACCOUNT" },
  { id: "incident",  label: "Incident report",           blurb: "One incident, with cause and resolution", reportType: "MONTHLY", scopeType: "INCIDENT" },
  { id: "resource",  label: "Resource report",           blurb: "Alert history of one resource", reportType: "MONTHLY", scopeType: "RESOURCE" },
  { id: "client",    label: "Stakeholder / client report", blurb: "All accounts, admin only", reportType: "MONTHLY", scopeType: "CLIENT", adminOnly: true },
  { id: "custom",    label: "Custom period",             blurb: "Any range up to 400 days", reportType: "CUSTOM", scopeType: "ACCOUNT" },
];

export const PERIODS = [
  { key: "WEEKLY", label: "7 days", days: 7 }, { key: "MONTHLY", label: "30 days", days: 30 },
  { key: "QUARTERLY", label: "90 days", days: 90 }, { key: "CUSTOM", label: "Custom" },
];

export const templatesFor = isAdmin => TEMPLATES.filter(t => !t.adminOnly || isAdmin);

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
  if (scopeType === "CLIENT") {
    if (!isAdmin) return "Client reports span every account and are admin-only.";
    if (!String(scopeId || "").trim()) return "Enter the client or stakeholder name for the report cover.";
  } else {
    if (!accountId) return "Select an account.";
    if ((scopeType === "RESOURCE" || scopeType === "INCIDENT") && !String(scopeId || "").trim()) return `Select ${scopeType === "RESOURCE" ? "a resource" : "an incident"}.`;
  }
  if (reportType === "CUSTOM") {
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

export function describeRequest({ template, reportType, scopeLabel, window: w, tz = "UTC" }) {
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];   // fixed names: locale month abbreviations differ between engines
  const fmt = d => {
    const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: tz, day: "2-digit", month: "numeric", year: "numeric" }).formatToParts(d).map(x => [x.type, x.value]));
    return `${p.day} ${MON[Number(p.month) - 1]} ${p.year}`;
  };
  const span = w ? `${fmt(w.start)} \u2013 ${fmt(w.end)}` : "period not set";
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
