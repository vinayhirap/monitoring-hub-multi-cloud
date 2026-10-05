// src/utils/evidence.js
// Pure helpers for the monitoring workflow (problem -> resource -> metric -> event -> evidence).
// No fetching, no estimates: everything is derived from rows the existing APIs already return.

const SEV_RANK = { CRITICAL: 0, WARNING: 1, INFO: 2 };
export const sevRank = s => (SEV_RANK[String(s || "").toUpperCase()] ?? 3);

export function tsMs(v) {
  if (v == null || v === "") return null;
  const t = typeof v === "number" ? v : Date.parse(String(v).includes("T") || /Z$|[+-]\d\d:?\d\d$/.test(String(v)) ? v : `${String(v).replace(" ", "T")}Z`);
  return Number.isFinite(t) ? t : null;
}

export function ageText(ms) {
  if (ms == null || !Number.isFinite(ms)) return "—";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60); if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60); if (h < 48) return `${h}h ${m % 60 ? `${m % 60}m` : ""}`.trim();
  return `${Math.floor(h / 24)}d`;
}

/** Summary stats of a chart series ([{t, v}] with null gaps ignored). */
export function seriesStats(points) {
  const v = (points || []).filter(p => p && p.v != null && Number.isFinite(Number(p.v)));
  if (!v.length) return null;
  const nums = v.map(p => Number(p.v));
  const sum = nums.reduce((a, b) => a + b, 0);
  const avg = sum / nums.length;
  const latest = nums[nums.length - 1];
  const max = Math.max(...nums), min = Math.min(...nums);
  const maxAt = v[nums.indexOf(max)].t;
  // trend: mean of the newest quarter vs mean of the oldest quarter (>= 2 points each side)
  let trend = null;
  if (nums.length >= 8) {
    const q = Math.max(2, Math.floor(nums.length / 4));
    const head = nums.slice(0, q).reduce((a, b) => a + b, 0) / q;
    const tail = nums.slice(-q).reduce((a, b) => a + b, 0) / q;
    const delta = tail - head;
    const base = Math.max(Math.abs(head), Math.abs(avg) * 0.1, 1e-9);
    const pct = (delta / base) * 100;
    trend = { delta, pct, dir: Math.abs(pct) < 5 ? "flat" : delta > 0 ? "up" : "down" };
  }
  return { n: nums.length, min, max, avg, latest, maxAt, trend };
}

/**
 * Freshness of a metric's newest datapoint against how often it is actually COLLECTED.
 *
 * It used to judge from the CloudWatch period alone (5 min -> "late" after exactly 15 min), which got two things wrong:
 *  - a 5-minute metric collected every 5 minutes is normally up to ~13 min old (the period must close, CloudWatch publishes it
 *    a few minutes later, then the next collection picks it up), so "fresh" flipped to "late" at 15 min with nothing wrong;
 *  - an HOURLY metric (polled every 1 hr) was called "stale" at 44 minutes.
 * The allowance now comes from the server (stale_after_seconds: the same table the alert engine uses to mark data stale),
 * else is derived from the collection interval. Fresh within the allowance, late within twice it, stale beyond;
 * "idle" instead for event-driven (sparse) metrics.
 */
export function freshness(lastMs, nowMs, periodSecs, pollSecs = 0, staleAfterSecs = 0, sparse = false) {
  if (lastMs == null) return { state: "none", age: null };
  const age = Math.max(0, nowMs - lastMs);
  let allow = 0;
  if (staleAfterSecs > 0) allow = staleAfterSecs;
  else if (pollSecs > 0) allow = Math.max(1200, pollSecs * 2.5);
  else if (periodSecs > 0) allow = Math.max(1200, periodSecs * 4);
  if (!(allow > 0)) return { state: "unknown", age };        // cadence not known: report the age, never a verdict
  const lim = allow * 1000;
  let state = age <= lim ? "fresh" : age <= lim * 2 ? "late" : "stale";
  // EVENT-DRIVEN metrics (Lambda, SQS, SNS, request-based load balancer metrics, ...) are published by AWS only when something
  // happens. No new datapoint then means "no activity", not "the collector is behind", so it is never called late or stale.
  if (sparse && state !== "fresh") state = "idle";
  return { state, age, allowanceMs: lim };
}

/**
 * Age to SHOW. A datapoint is stamped with the START of its period, so a 5-minute point stamped 11:45 only closes at 11:50:
 * measuring from the start made every reading look 5 minutes older than it is. Show time since the period closed.
 */
export function displayAge(ageMs, periodSecs) {
  if (ageMs == null) return null;
  return Math.max(0, ageMs - Math.max(0, periodSecs || 0) * 1000);
}

/** Where the latest value sits against configured thresholds. comparison: ">" (default) or "<". */
export function breachState(value, warn, crit, comparison = ">") {
  if (value == null) return null;
  const lt = comparison === "<" || comparison === "<=";
  const hit = t => t != null && (lt ? value <= t : value >= t);
  if (hit(crit)) return "critical";
  if (hit(warn)) return "warning";
  return "ok";
}

/** Alerts of one resource: exact id match, firing first, then severity, then newest. */
export function alertsForResource(rows, ids) {
  const set = new Set((ids || []).filter(Boolean).map(String));
  const mine = (rows || []).filter(a => set.has(String(a.resource)) || set.has(String(a.resource_name)));
  const firing = a => (a.state === "firing" ? 0 : 1);
  return mine.sort((a, b) => firing(a) - firing(b) || sevRank(a.severity) - sevRank(b.severity)
    || (tsMs(b.triggered_at) || 0) - (tsMs(a.triggered_at) || 0));
}

/** Op events that actually mention this resource (message or detail). Never guesses by account. */
export function eventsForResource(events, ids) {
  const keys = (ids || []).filter(x => x && String(x).length >= 4).map(String);
  if (!keys.length) return [];
  return (events || []).filter(e => {
    const hay = `${e.message || ""} ${typeof e.detail === "string" ? e.detail : JSON.stringify(e.detail || {})}`;
    return keys.some(k => hay.includes(k));
  });
}

/** One chronological evidence timeline from alerts (triggered + resolved) and events. */
export function buildTimeline(alerts, events) {
  const out = [];
  for (const a of alerts || []) {
    const t = tsMs(a.triggered_at);
    if (t != null) out.push({ kind: "alert", at: t, sev: String(a.severity || "").toUpperCase(), key: `a${a.id}t`, alert: a, label: "triggered" });
    const r = tsMs(a.resolved_at);
    if (r != null) out.push({ kind: "alert", at: r, sev: "RESOLVED", key: `a${a.id}r`, alert: a, label: "resolved" });
  }
  for (const e of events || []) {
    const t = tsMs(e.created_at);
    if (t != null) out.push({ kind: "event", at: t, sev: String(e.severity || "INFO").toUpperCase(), key: `e${e.id ?? t}`, event: e });
  }
  return out.sort((a, b) => b.at - a.at);
}

/** Health tone from a 0-100 score (resource absent from the health list == fully healthy). */
export function healthTone(score) {
  if (score == null) return "mute";
  return score >= 90 ? "ok" : score >= 70 ? "warn" : "crit";
}

/** Real lifecycle steps from the alert's own fields (no invented states). */
export function lifecycle(a) {
  const steps = [{ key: "triggered", label: "Triggered", at: a.triggered_at, done: true }];
  steps.push({ key: "ack", label: "Acknowledged", at: a.acked_at, who: a.acked_by, done: !!(a.acked || a.acked_at) });
  steps.push({ key: "resolved", label: "Resolved", at: a.resolved_at, why: a.resolution_reason, done: !!a.resolved_at || a.status === "resolved" });
  return steps;
}

