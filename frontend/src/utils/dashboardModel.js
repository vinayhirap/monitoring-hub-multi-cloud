// src/utils/dashboardModel.js
// Pure derivations for the Overview dashboard. NO fetching, NO React, NO invented data:
// every number comes from rows the existing APIs already return
//   rows      <- GET /api/live/accounts           (account x region, alert counts, last_synced_at, active_services)
//   firing    <- GET /api/alerts?tab=active       (firing alerts, triggered_at, service, resource_name, ...)
//   resolved  <- GET /api/alerts?tab=resolved     (newest resolved_at first)
//   incidents <- GET /api/incidents/{id}?status=active (per account row)
//   events    <- GET /api/op-events               (system events; null when caller lacks operations.view)
//   fleet     <- GET /api/incidents/fleet-summary + fleet-detail
// Anything an endpoint cannot supply is returned as null, never estimated.

export const FIRING_LIMIT = 1000;      // must match the fetch limit in useDashboardData
export const RESOLVED_LIMIT = 500;
export const FRESH_MIN = 15;           // collector low tier / discovery cycle is 15 min
export const STALE_MIN = 60;           // a full extended-tier cycle has passed with no sync
const HOUR = 3600e3;

/** Backend timestamps are UTC. Alerts carry an explicit Z; DB datetimes elsewhere come back
 *  naive, so a missing zone designator is read as UTC (the system-wide convention). */
export function parseTs(v) {
  if (!v) return null;
  if (typeof v === "number") return v;
  let s = String(v).trim();
  if (!/(Z|[+-]\d{2}:?\d{2})$/i.test(s)) s = s.replace(" ", "T") + "Z";
  const t = Date.parse(s);
  return Number.isNaN(t) ? null : t;
}

export function ageLabel(ms) {
  if (ms == null) return "—";
  const m = Math.max(0, Math.floor(ms / 60000));
  if (m < 1) return "<1m";
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60 ? `${m % 60}m` : ""}`.trim();
  return `${Math.floor(h / 24)}d`;
}

const sevKey = s => { const u = String(s || "").toUpperCase(); return u === "CRITICAL" ? "crit" : u === "WARNING" ? "warn" : "info"; };
const SEV_RANK = { crit: 0, warn: 1, info: 2 };
const sum = (rows, k) => rows.reduce((n, r) => n + (Number(r[k]) || 0), 0);
export const SERVICE_LABEL = { ec2: "EC2", ebs: "EBS", rds: "RDS", s3: "S3", lambda: "Lambda", elb: "ELB", ecs: "ECS" };
export const serviceLabel = k => SERVICE_LABEL[k] || String(k || "other").toUpperCase();

export function summarize({ rows, firing, resolved, incidents, events, fleet, now = Date.now(), scopeIds = null }) {
  const inScope = id => !scopeIds || scopeIds.has(id);
  const R = rows.filter(r => inScope(r.id));
  const F = (firing || []).filter(a => inScope(a.account_id));
  const D = (resolved || []).filter(a => inScope(a.account_id));
  const INC = incidents == null ? null : incidents.filter(i => inScope(i.account_row_id));
  const EV = events == null ? null : events.filter(e => !scopeIds || e.aws_account_id == null || scopeIds.has(e.aws_account_id));

  // ── headline numbers: the same per-row figures the Overview banner always used. NOTE these count
  //    DISTINCT ALERTING RESOURCES (live_data.py _get_active_alert_counts_by_account), not raw alert
  //    rows; raw firing rows are counted separately below so both are shown and never conflated.
  const critical = sum(R, "critical_alerts");
  const warning = sum(R, "warning_alerts");
  const critAlerts = F.filter(a => sevKey(a.severity) === "crit").length;
  const warnAlerts = F.filter(a => sevKey(a.severity) === "warn").length;
  const anomalies = F.filter(a => a.metric_name === "multivariate_anomaly");
  const critResources = new Set(F.filter(a => sevKey(a.severity) === "crit").map(a => `${a.account_id}|${a.resource}`));

  // ── freshness / coverage (per account x region)
  const freshRows = R.map(r => {
    const t = parseTs(r.last_synced_at);
    const age = t == null ? null : now - t;
    const level = age == null || age < -120000 ? "unknown" : age <= FRESH_MIN * 60000 ? "fresh" : age <= STALE_MIN * 60000 ? "aging" : "stale";
    return { row: r, age: age != null && age < 0 ? 0 : age, level, services: (r.active_services || []).length };
  }).sort((a, b) => ({ stale: 0, unknown: 1, aging: 2, fresh: 3 }[a.level] - { stale: 0, unknown: 1, aging: 2, fresh: 3 }[b.level]) || (b.age ?? 0) - (a.age ?? 0));
  const freshCount = freshRows.filter(f => f.level === "fresh").length;
  const staleCount = freshRows.filter(f => f.level === "stale").length;

  // ── what changed: hourly alert activity, last 24h
  const hourStart = Math.floor(now / HOUR) * HOUR;
  const t0 = hourStart - 23 * HOUR;
  const buckets = Array.from({ length: 24 }, (_, i) => ({ t: t0 + i * HOUR, crit: 0, warn: 0, info: 0, resolved: 0 }));
  const seen = new Set();
  for (const a of [...F, ...D]) {
    if (seen.has(a.id)) continue; seen.add(a.id);
    const t = parseTs(a.triggered_at);
    if (t != null && t >= t0 && t <= now) buckets[Math.min(23, Math.floor((t - t0) / HOUR))][sevKey(a.severity)]++;
  }
  for (const a of D) {
    const t = parseTs(a.resolved_at);
    if (t != null && t >= t0 && t <= now) buckets[Math.min(23, Math.floor((t - t0) / HOUR))].resolved++;
  }
  const countSince = (list, field, h) => list.filter(a => { const t = parseTs(a[field]); return t != null && t >= now - h * HOUR && t <= now; }).length;
  const uniq = [...new Map([...F, ...D].map(a => [a.id, a])).values()];
  const windows = {
    new1h: countSince(uniq, "triggered_at", 1), new6h: countSince(uniq, "triggered_at", 6), new24h: countSince(uniq, "triggered_at", 24),
    res1h: countSince(D, "resolved_at", 1), res6h: countSince(D, "resolved_at", 6), res24h: countSince(D, "resolved_at", 24),
  };
  const oldestResolved = D.length ? Math.min(...D.map(a => parseTs(a.resolved_at) ?? Infinity)) : null;
  const capped = {
    firing: (firing || []).length >= FIRING_LIMIT,
    resolved: (resolved || []).length >= RESOLVED_LIMIT && oldestResolved != null && oldestResolved > t0,
  };

  // ── where: region x service matrix
  const svcCount = new Map();
  for (const a of F) svcCount.set(a.service || "other", (svcCount.get(a.service || "other") || 0) + 1);
  for (const r of R) for (const s of r.active_services || []) if (!svcCount.has(s)) svcCount.set(s, 0);
  const cols = [...svcCount.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([k]) => k).slice(0, 8);
  const matrixRows = [...R].sort((a, b) => ({ critical: 0, warning: 1, healthy: 2 }[a.status] ?? 3) - ({ critical: 0, warning: 1, healthy: 2 }[b.status] ?? 3)
    || String(a.account_name).localeCompare(String(b.account_name))).map(r => {
    const cells = {};
    for (const c of cols) {
      const al = F.filter(a => a.account_id === r.id && (a.service || "other") === c);
      const crit = al.filter(a => sevKey(a.severity) === "crit").length, warn = al.filter(a => sevKey(a.severity) === "warn").length;
      cells[c] = al.length ? { crit, warn, total: al.length } : (r.active_services || []).includes(c) ? { crit: 0, warn: 0, total: 0, ok: true } : null;
    }
    return { row: r, cells };
  });

  // ── top problematic resources (firing alerts grouped per resource)
  const health = new Map(((fleet?.detail?.critical_resources) || []).map(h => [`${h.aws_account_id}|${h.resource_id}`, h.health_score]));
  const g = new Map();
  for (const a of F) {
    const k = `${a.account_id}|${a.resource}`;
    const e = g.get(k) || { key: k, resource: a.resource, name: a.resource_name || a.resource, service: a.service, account_id: a.account_id,
      account_name: a.account_name, region: a.region, crit: 0, warn: 0, info: 0, oldest: null, metrics: new Set(), attention: false };
    e[sevKey(a.severity)]++;
    e.metrics.add(a.metric_name);
    const t = parseTs(a.triggered_at); if (t != null && (e.oldest == null || t < e.oldest)) e.oldest = t;
    e.attention = e.attention || !!a.needs_attention;
    g.set(k, e);
  }
  const topResources = [...g.values()].map(e => ({ ...e, metrics: [...e.metrics], total: e.crit + e.warn + e.info, score: health.get(e.key) ?? null,
    age: e.oldest == null ? null : now - e.oldest }))
    .sort((a, b) => b.crit - a.crit || b.warn - a.warn || (b.age ?? 0) - (a.age ?? 0)).slice(0, 8);

  // ── incidents (correlated, system-generated)
  const activeInc = INC == null ? null : INC.filter(i => i.status === "active")
    .map(i => ({ ...i, sev: sevKey(i.severity), startedMs: parseTs(i.started_at), lastSeenMs: parseTs(i.last_seen_at) }))
    .sort((a, b) => SEV_RANK[a.sev] - SEV_RANK[b.sev] || (b.lastSeenMs ?? 0) - (a.lastSeenMs ?? 0));

  // ── verdict
  const incCrit = activeInc ? activeInc.filter(i => i.sev === "crit").length : 0;
  const sysErr = EV ? EV.filter(e => String(e.severity).toUpperCase() === "ERROR" && (parseTs(e.created_at) ?? 0) >= now - HOUR).length : 0;
  let level = "healthy";
  if (!R.length) level = "unknown";
  else if (critical > 0 || incCrit > 0) level = "critical";
  else if (warning > 0 || staleCount > 0 || sysErr > 0 || (activeInc && activeInc.length)) level = "warning";
  const reasons = [];
  const raw = n => (n ? ` (${n} alert${n === 1 ? "" : "s"})` : "");
  if (critical > 0) reasons.push(`${critical} resource${critical === 1 ? "" : "s"} with critical alerts${raw(critAlerts)}`);
  if (warning > 0) reasons.push(`${warning} resource${warning === 1 ? "" : "s"} with warning alerts${raw(warnAlerts)}`);
  if (activeInc && activeInc.length) reasons.push(`${activeInc.length} active incident${activeInc.length === 1 ? "" : "s"}`);
  if (staleCount > 0) reasons.push(`${staleCount} region${staleCount === 1 ? "" : "s"} not synced for over ${STALE_MIN} min`);
  if (sysErr > 0) reasons.push(`${sysErr} system error event${sysErr === 1 ? "" : "s"} in the last hour`);
  if (level === "healthy") reasons.push(`All ${R.length} region${R.length === 1 ? "" : "s"} reporting, no firing alerts`);
  const affected = R.filter(r => r.status !== "healthy").length;

  // ── feed: what changed in the last 24h, newest first
  const feed = [];
  const since = now - 24 * HOUR;
  for (const a of uniq) {
    const t = parseTs(a.triggered_at);
    if (t != null && t >= since && sevKey(a.severity) !== "info" && a.state !== "resolved")
      feed.push({ t, kind: "Triggered", sev: sevKey(a.severity), text: a.resource_name || a.resource, sub: `${a.metric_name} · ${a.account_name}${a.region ? ` · ${a.region}` : ""}`, to: `/alerts?tab=active&q=${encodeURIComponent(a.resource || "")}` });
  }
  for (const a of D) {
    const t = parseTs(a.resolved_at);
    if (t != null && t >= since) feed.push({ t, kind: "Resolved", sev: "ok", text: a.resource_name || a.resource, sub: `${a.metric_name} · ${a.account_name}`, to: `/alerts?tab=resolved&q=${encodeURIComponent(a.resource || "")}` });
  }
  for (const i of activeInc || []) if (i.startedMs != null && i.startedMs >= since)
    feed.push({ t: i.startedMs, kind: "Incident", sev: i.sev, text: i.title, sub: `${i.alert_count} correlated alert${i.alert_count === 1 ? "" : "s"}`, to: `/accounts/${i.account_row_id}/incidents` });
  for (const e of EV || []) {
    const t = parseTs(e.created_at);
    if (t != null && t >= since && String(e.severity).toUpperCase() !== "INFO")
      feed.push({ t, kind: "System", sev: String(e.severity).toUpperCase() === "ERROR" ? "crit" : "warn", text: String(e.message || e.event_type).slice(0, 140), sub: `${e.event_type}${e.account_name ? ` · ${e.account_name}` : ""}`, to: "/op-events" });
  }
  feed.sort((a, b) => b.t - a.t);

  return {
    verdict: { level, reasons, affected, total: R.length },
    // undefined = that source has not answered yet (show a skeleton); null = the caller may not read it. Never the same thing.
    pending: { firing: firing === undefined, resolved: resolved === undefined, incidents: incidents === undefined, events: events === undefined, fleet: fleet === undefined },
    kpi: { critical, warning, critAlerts, warnAlerts, critResources: critResources.size, incidents: activeInc ? activeInc.length : null, anomalies: anomalies.length,
      // SAME rule AND SAME UNIT as the Alerts "Needs attention" tab: firing ALERTS on resources with health < 70
      attention: F.filter(a => a.needs_attention).length,
      attentionResources: [...g.values()].filter(e => e.attention).length,
      capacity: fleet?.summary ? fleet.summary.capacity_risk_count : null,
      flapping: fleet?.summary ? fleet.summary.likely_flapping_count : null, ...windows, fresh: freshCount, regions: R.length, stale: staleCount },
    activity: { buckets, windows, capped },
    matrix: { cols, rows: matrixRows },
    topResources, anomalies, incidents: activeInc,
    freshness: freshRows,
    fleetView: fleet ? { summary: fleet.summary ?? null, detail: fleet.detail ?? null } : null,
    feed: feed.slice(0, 14), feedTotal: feed.length,
  };
}
