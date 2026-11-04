// src/utils/emailCenterView.js
//
// Pure helpers for the Email & Reports page (pages/EmailCenter.jsx and pages/emailcenter/*): form <-> API payload
// mapping, client-side checks that mirror the backend's (the backend stays the authority), and display labels.
// Kept free of React so it is unit-tested with `node --test` like the other utils.
import { reportScopeId, ITEM_SCOPES } from "./reportPlan.js";

const EMAIL_RE = /^[^@\s,;<>"'\\]+@[^@\s,;<>"'\\]+\.[^@\s,;<>"'\\]+$/;

/** "a@b.co, c@d.co\ne@f.co" -> { emails: [...unique], bad: [first invalid text, ...] } */
export function parseTypedEmails(text) {
  const emails = [], bad = [], seen = new Set();
  String(text || "").split(/[,;\n]/).map(s => s.trim()).filter(Boolean).forEach(a => {
    if (a.length > 254 || !EMAIL_RE.test(a)) { bad.push(a.slice(0, 60)); return; }
    if (!seen.has(a.toLowerCase())) { seen.add(a.toLowerCase()); emails.push(a); }
  });
  return { emails, bad };
}

export const SCOPE_WORDS = {
  ACCOUNT: "Account", RESOURCE: "Resource", SYNTHETIC: "Synthetic checks", HOST: "Host agents",
  ENVIRONMENT: "Environments", SECURITY: "Security findings", SLO: "SLOs", COST: "Cloud cost", CLIENT: "Client",
};
export const REPORT_TYPE_WORDS = { WEEKLY: "Weekly", MONTHLY: "Monthly", QUARTERLY: "Quarterly", YEARLY: "Yearly" };
export const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
export const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

// ── recipient lists ──────────────────────────────────────────────────

export const emptyListForm = () => ({ name: "", description: "", user_ids: [], group_ids: [], emails: "" });

export const listToForm = l => ({
  name: l?.name || "", description: l?.description || "",
  user_ids: [...(l?.user_ids || [])], group_ids: [...(l?.group_ids || [])], emails: (l?.emails || []).join(", "),
});

export function listFormProblem(f) {
  if (!String(f.name || "").trim()) return "Give the list a name.";
  const { bad, emails } = parseTypedEmails(f.emails);
  if (bad.length) return `Not a valid email address: ${bad[0]}`;
  if (!f.user_ids.length && !f.group_ids.length && !emails.length) return "Add at least one user, group or email address.";
  return "";
}

export const listFormToPayload = f => ({
  name: f.name.trim(), description: (f.description || "").trim(),
  user_ids: f.user_ids.map(Number), group_ids: f.group_ids.map(Number), emails: parseTypedEmails(f.emails).emails,
});

/** Toggle an id in an id array (immutably). */
export const toggleId = (ids, id) => (ids.includes(id) ? ids.filter(x => x !== id) : [...ids, id]);

// ── schedules ────────────────────────────────────────────────────────

export const emptyScheduleForm = (defaultTz = "Asia/Kolkata") => ({
  name: "", enabled: true, report_type: "MONTHLY", scope_type: "ACCOUNT", account_id: "", item_id: "", resource_id: "",
  cost_provider: "all", client_name: "", run_weekday: 0, run_month: 4, run_day: 1, run_hour: 8, run_minute: 0,
  timezone: defaultTz, recipient_list_id: "", extra_emails: "",
});

/** The pieces of a stored scope_id: "7" -> {account:7}, "7:12" -> {account:7, item:12}. */
export function splitScope(scopeType, scopeId) {
  const s = String(scopeId ?? "");
  if (ITEM_SCOPES[scopeType]) { const [a, i] = s.split(":"); return { account: a || "", item: i || "" }; }
  return { account: s, item: "" };
}

export function scheduleToForm(r, defaultTz = "Asia/Kolkata") {
  const f = emptyScheduleForm(defaultTz);
  const sp = splitScope(r.scope_type, r.scope_id);
  return {
    ...f, name: r.name, enabled: !!r.enabled, report_type: r.report_type, scope_type: r.scope_type,
    account_id: r.account_id != null ? String(r.account_id) : "", item_id: ITEM_SCOPES[r.scope_type] ? sp.item : "",
    resource_id: r.scope_type === "RESOURCE" ? String(r.scope_id) : "",
    cost_provider: r.scope_type === "COST" ? String(r.scope_id || "all") : "all",
    client_name: r.scope_type === "CLIENT" ? String(r.scope_id || "") : "",
    run_weekday: r.run_weekday ?? 0, run_month: r.run_month ?? 4, run_day: r.run_day ?? 1,
    run_hour: r.run_hour ?? 8, run_minute: r.run_minute ?? 0, timezone: r.timezone || defaultTz,
    recipient_list_id: r.recipient_list_id != null ? String(r.recipient_list_id) : "",
    extra_emails: (r.extra_emails || []).join(", "),
  };
}

/** The scope_id the backend expects for this form (same shapes as the Reports page). */
export function formScopeId(f) {
  if (f.scope_type === "COST") return f.cost_provider || "all";
  if (f.scope_type === "CLIENT") return String(f.client_name || "").trim();
  if (f.scope_type === "RESOURCE") return String(f.resource_id || "").trim();
  return reportScopeId({ scopeType: f.scope_type, accountId: f.account_id, itemId: f.item_id, scopeId: "" });
}

const needsAccount = t => !["COST", "CLIENT"].includes(t);

export function scheduleFormProblem(f) {
  if (!String(f.name || "").trim()) return "Give the schedule a name.";
  if (needsAccount(f.scope_type) && !f.account_id) return "Choose an account.";
  if (f.scope_type === "RESOURCE" && !String(f.resource_id || "").trim()) return "Enter the resource id.";
  if (f.scope_type === "CLIENT" && !String(f.client_name || "").trim()) return "Enter the client name shown on the report.";
  if (f.report_type !== "WEEKLY" && !(Number(f.run_day) >= 1 && Number(f.run_day) <= 28)) return "Day of month must be 1 to 28.";
  const { bad, emails } = parseTypedEmails(f.extra_emails);
  if (bad.length) return `Not a valid email address: ${bad[0]}`;
  if (!f.recipient_list_id && !emails.length) return "Choose a recipient list or type at least one email address.";
  return "";
}

export function scheduleFormToPayload(f) {
  const out = {
    name: f.name.trim(), enabled: !!f.enabled, report_type: f.report_type, scope_type: f.scope_type,
    scope_id: formScopeId(f), account_id: needsAccount(f.scope_type) ? Number(f.account_id) : null,
    run_hour: Number(f.run_hour), run_minute: Number(f.run_minute), timezone: f.timezone,
    recipient_list_id: f.recipient_list_id ? Number(f.recipient_list_id) : null,
    extra_emails: parseTypedEmails(f.extra_emails).emails,
  };
  if (f.report_type === "WEEKLY") out.run_weekday = Number(f.run_weekday);
  else { out.run_day = Number(f.run_day); if (f.report_type === "YEARLY") out.run_month = Number(f.run_month); }
  return out;
}

/** "Account U4RAD" / "SLOs in U4RAD" for a stored schedule, given the options' accounts. */
export function scopeLabel(row, accounts = []) {
  const acct = accounts.find(a => String(a.id) === String(row.account_id));
  const where = acct ? acct.name : (row.account_id != null ? `account ${row.account_id}` : "");
  const sp = splitScope(row.scope_type, row.scope_id);
  switch (row.scope_type) {
    case "ACCOUNT": return where ? `Account ${where}` : "Account";
    case "RESOURCE": return `Resource ${row.scope_id}${where ? ` in ${where}` : ""}`;
    case "COST": return `Cloud cost: ${row.scope_id === "all" || !row.scope_id ? "all clouds" : String(row.scope_id).toUpperCase()}`;
    case "CLIENT": return `Client: ${row.scope_id}`;
    default: return `${SCOPE_WORDS[row.scope_type] || row.scope_type}: ${sp.item ? `#${sp.item}` : "all"}${where ? ` in ${where}` : ""}`;
  }
}

// ── alert & incident rules ───────────────────────────────────────────

// Same ids and order as app/email/alert_rules.EVENTS; the backend stays the authority on what is valid.
export const RULE_EVENTS = [
  { key: "alert_opened", label: "Alert opened", hint: "A new alert is raised." },
  { key: "alert_escalated", label: "Alert escalated", hint: "An alert stays open long enough to be escalated." },
  { key: "alert_resolved", label: "Alert resolved", hint: "An alert closes, by itself or by a person." },
  { key: "incident_opened", label: "Incident opened", hint: "Related alerts are grouped into a new incident." },
  { key: "incident_resolved", label: "Incident resolved", hint: "An incident closes." },
];
export const RULE_SEVERITIES = [
  { key: "WARNING", label: "Warning and Critical" },
  { key: "CRITICAL", label: "Critical only" },
];
export const ruleEventLabel = key => RULE_EVENTS.find(e => e.key === key)?.label || key;

export const emptyRuleForm = () => ({
  name: "", enabled: true, events: ["alert_opened", "alert_resolved"], min_severity: "WARNING",
  account_id: "", recipient_list_id: "", extra_emails: "",
});

export const ruleToForm = r => ({
  name: r?.name || "", enabled: r ? !!r.enabled : true,
  events: RULE_EVENTS.map(e => e.key).filter(k => (r?.events || []).includes(k)),
  min_severity: r?.min_severity || "WARNING",
  account_id: r?.account_id != null ? String(r.account_id) : "",
  recipient_list_id: r?.recipient_list_id != null ? String(r.recipient_list_id) : "",
  extra_emails: (r?.extra_emails || []).join(", "),
});

/** Toggle an event, keeping the canonical order (so the stored CSV and the display never reshuffle). */
export const toggleRuleEvent = (events, key) =>
  RULE_EVENTS.map(e => e.key).filter(k => (k === key ? !events.includes(k) : events.includes(k)));

export function ruleFormProblem(f) {
  if (!String(f.name || "").trim()) return "Give the rule a name.";
  if (!f.events.length) return "Choose at least one event.";
  const { bad, emails } = parseTypedEmails(f.extra_emails);
  if (bad.length) return `Not a valid email address: ${bad[0]}`;
  if (!f.recipient_list_id && !emails.length) return "Choose a recipient list or type at least one email address.";
  return "";
}

export const ruleFormToPayload = f => ({
  name: f.name.trim(), enabled: !!f.enabled, events: [...f.events], min_severity: f.min_severity,
  account_id: f.account_id ? Number(f.account_id) : null,
  recipient_list_id: f.recipient_list_id ? Number(f.recipient_list_id) : null,
  extra_emails: parseTypedEmails(f.extra_emails).emails,
});

/** "All accounts" / the account's name for a stored rule, given the options' accounts. */
export function ruleAccountLabel(rule, accounts = []) {
  if (rule.account_id == null) return "All accounts";
  const a = accounts.find(x => String(x.id) === String(rule.account_id));
  return a ? a.name : `Account ${rule.account_id}`;
}

/** Colour tone for a schedule's last_status / a delivery status (matches the ui kit's Badge tones). */
export function statusTone(s) {
  switch (String(s || "").toUpperCase()) {
    case "SENT": return "ok";
    case "PARTIAL": case "SKIPPED": return "warn";
    case "FAILED": return "crit";
    case "RUNNING": return "info";
    default: return "mute";
  }
}
export const deliveryTone = s => statusTone(s === "sent" ? "SENT" : s === "failed" ? "FAILED" : s === "skipped" ? "SKIPPED" : "");

/** Delivery-log paging: the id to ask for next (older rows), or null when this page was the last one. */
export const nextBeforeId = (rows, limit) => (rows && rows.length >= limit ? rows[rows.length - 1].id : null);

/** The problems list for the SMTP tab, with the http warning shown once even when config_problems() already has it. */
export function smtpProblems(smtp) {
  const warn = smtp?.public_url_warning || "";
  const rest = (smtp?.problems || []).filter(p => p !== warn);
  return { problems: rest, urlWarning: warn };
}
