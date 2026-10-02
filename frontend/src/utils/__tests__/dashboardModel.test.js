// Run: node --test src/utils/__tests__/
// Fixtures are shaped exactly like the backend rows (app/api/alerts.py, live_data.py, incidents.py, op_events.py)
// and are used ONLY to verify the derivations; the product never ships or renders them.
import test from "node:test";
import assert from "node:assert/strict";
import { summarize, parseTs, ageLabel } from "../dashboardModel.js";

const NOW = Date.parse("2026-10-01T12:30:00Z");
const iso = minAgo => new Date(NOW - minAgo * 60000).toISOString().replace(".000Z", "Z");
const row = (id, name, region, status, c, w, synced, svcs = ["ec2", "rds"]) => ({ id, account_name: name, account_id: String(1000 + id), region, status,
  critical_alerts: c, warning_alerts: w, last_synced_at: synced, active_services: svcs });
const alert = (id, sev, acct, res, svc, trig, extra = {}) => ({ id, severity: sev, account_id: acct, account_name: "A", resource: res, resource_name: res, service: svc,
  metric_name: "cpu_utilization", triggered_at: trig, state: "firing", region: "ap-south-1", ...extra });

const rows = [row(1, "AuroGov", "ap-south-1", "critical", 1, 1, "2026-10-01T12:25:00"), row(2, "U4RAD", "ap-south-1", "healthy", 0, 0, "2026-10-01T10:00:00"), row(3, "U4RAD", "ap-south-2", "warning", 0, 1, null)];
const firing = [alert(1, "CRITICAL", 1, "i-aaa", "ec2", iso(20)), alert(2, "CRITICAL", 1, "i-aaa", "ec2", iso(300), { metric_name: "multivariate_anomaly" }),
  alert(3, "WARNING", 1, "db-1", "rds", iso(90)), alert(4, "WARNING", 3, "i-ccc", "ec2", iso(2000))];
const resolved = [alert(10, "WARNING", 1, "i-old", "ec2", iso(200), { state: "resolved", resolved_at: iso(30) }), alert(11, "CRITICAL", 2, "i-x", "ec2", iso(1000), { state: "resolved", resolved_at: iso(700) })];

test("parseTs: Z, offset, and naive-as-UTC", () => {
  assert.equal(parseTs("2026-10-01T12:30:00Z"), NOW);
  assert.equal(parseTs("2026-10-01T18:00:00+05:30"), NOW);
  assert.equal(parseTs("2026-10-01T12:30:00"), NOW);
  assert.equal(parseTs(null), null);
});
test("ageLabel", () => { assert.equal(ageLabel(30e3), "<1m"); assert.equal(ageLabel(5 * 60e3), "5m"); assert.equal(ageLabel(150 * 60e3), "2h 30m"); assert.equal(ageLabel(null), "—"); });

const s = summarize({ rows, firing, resolved, incidents: [], events: [], fleet: { summary: { critical_resource_count: 1, capacity_risk_count: 2, likely_flapping_count: 0 }, detail: { critical_resources: [{ aws_account_id: 1, resource_id: "i-aaa", health_score: 31 }] } }, now: NOW });

test("verdict is critical with accurate reasons", () => {
  assert.equal(s.verdict.level, "critical");
  assert.equal(s.verdict.reasons[0], "1 resource with critical alerts (2 alerts)");
  assert.equal(s.verdict.reasons[1], "2 resources with warning alerts (2 alerts)");
  assert.equal(s.verdict.affected, 2);
});
test("headline = distinct resources (per-row figures, same as old banner); raw alert rows reported separately", () => { assert.equal(s.kpi.critical, 1); assert.equal(s.kpi.warning, 2); assert.equal(s.kpi.critAlerts, 2); assert.equal(s.kpi.warnAlerts, 2); });
test("what changed: windows count triggered and resolved correctly", () => {
  assert.equal(s.kpi.new1h, 1); assert.equal(s.kpi.new6h, 4); assert.equal(s.kpi.new24h, 5);
  assert.equal(s.kpi.res1h, 1); assert.equal(s.kpi.res24h, 2);
});
test("hourly buckets: 24 buckets, totals match 24h window, severity split", () => {
  const b = s.activity.buckets; assert.equal(b.length, 24);
  assert.equal(b.reduce((n, x) => n + x.crit + x.warn + x.info, 0), 5);
  assert.equal(b[23].crit, 1);                       // alert 1: 12:10 falls in the 12:00 bucket
  assert.equal(b.reduce((n, x) => n + x.resolved, 0), 2);
});
test("matrix: columns by firing volume, cells carry crit/warn, healthy-active cells marked ok, inactive null", () => {
  assert.deepEqual(s.matrix.cols.slice(0, 2), ["ec2", "rds"]);
  const a = s.matrix.rows.find(r => r.row.id === 1).cells;
  assert.deepEqual([a.ec2.crit, a.ec2.warn, a.rds.warn], [2, 0, 1]);
  const u = s.matrix.rows.find(r => r.row.id === 2).cells;
  assert.equal(u.ec2.ok, true);
  assert.equal(s.matrix.rows[0].row.id, 1);          // worst status first
});
test("top resources: grouped per resource, critical first, health score joined from fleet-detail", () => {
  const t = s.topResources;
  assert.equal(t[0].resource, "i-aaa"); assert.equal(t[0].crit, 2); assert.equal(t[0].score, 31);
  assert.deepEqual(t.map(x => x.resource), ["i-aaa", "i-ccc", "db-1"]);   // equal severity: longest-standing first
  assert.equal(t[0].age, 300 * 60000);               // oldest firing alert on the resource
});
test("anomalies = firing multivariate_anomaly rows only", () => { assert.equal(s.kpi.anomalies, 1); assert.equal(s.anomalies[0].id, 2); });
test("freshness levels (15/60 min) and unknown for never-synced", () => {
  const by = Object.fromEntries(s.freshness.map(f => [f.row.id, f.level]));
  assert.deepEqual(by, { 1: "fresh", 2: "stale", 3: "unknown" });
  assert.equal(s.kpi.fresh, 1); assert.equal(s.kpi.stale, 1); assert.equal(s.kpi.regions, 3);
});
test("missing endpoints yield null, never a guess", () => {
  const n = summarize({ rows, firing, resolved, incidents: null, events: null, fleet: null, now: NOW });
  assert.equal(n.kpi.incidents, null); assert.equal(n.kpi.attention, null); assert.equal(n.incidents, null);
});
test("incidents: active only, critical first; verdict degrades/escalates on them", () => {
  const inc = [{ id: 1, account_row_id: 2, title: "Disk saturation", severity: "warning", status: "active", alert_count: 3, started_at: iso(50), last_seen_at: iso(5) },
    { id: 2, account_row_id: 2, title: "DB down", severity: "critical", status: "active", alert_count: 5, started_at: iso(40), last_seen_at: iso(2) },
    { id: 3, account_row_id: 2, title: "old", severity: "critical", status: "resolved", alert_count: 1, started_at: iso(900), last_seen_at: iso(800) }];
  const r = summarize({ rows: [rows[1]], firing: [], resolved: [], incidents: inc, events: [], fleet: null, now: NOW });
  assert.deepEqual(r.incidents.map(i => i.id), [2, 1]);
  assert.equal(r.verdict.level, "critical");
});
test("healthy fleet reads healthy; no rows reads unknown", () => {
  const ok = [row(2, "U4RAD", "ap-south-1", "healthy", 0, 0, "2026-10-01T12:28:00")];
  assert.equal(summarize({ rows: ok, firing: [], resolved: [], incidents: [], events: [], fleet: null, now: NOW }).verdict.level, "healthy");
  assert.equal(summarize({ rows: [], firing: [], resolved: [], incidents: [], events: [], fleet: null, now: NOW }).verdict.level, "unknown");
});
test("system ERROR events in the last hour degrade a clean fleet; INFO never appears in the feed", () => {
  const ok = [row(2, "U4RAD", "ap-south-1", "healthy", 0, 0, "2026-10-01T12:28:00")];
  const ev = [{ event_type: "collector_cycle_failed", severity: "ERROR", message: "boom", created_at: "2026-10-01T12:10:00", aws_account_id: 2 },
    { event_type: "discovery_ok", severity: "INFO", message: "fine", created_at: "2026-10-01T12:11:00", aws_account_id: 2 }];
  const r = summarize({ rows: ok, firing: [], resolved: [], incidents: [], events: ev, fleet: null, now: NOW });
  assert.equal(r.verdict.level, "degraded"); assert.equal(r.feed.length, 1); assert.equal(r.feed[0].kind, "System");
});
test("scope filter limits every section to the chosen account rows", () => {
  const r = summarize({ rows, firing, resolved, incidents: [], events: [], fleet: null, now: NOW, scopeIds: new Set([3]) });
  assert.equal(r.kpi.regions, 1); assert.equal(r.kpi.critical, 0); assert.equal(r.topResources.length, 1); assert.equal(r.topResources[0].resource, "i-ccc");
});
test("feed: newest first, resolved included, 24h only", () => {
  const f = s.feed; assert.ok(f.every((x, i) => i === 0 || f[i - 1].t >= x.t));
  assert.ok(f.some(x => x.kind === "Resolved")); assert.ok(!f.some(x => x.text === "i-ccc"));   // i-ccc triggered 33h ago
});
test("capped flags fire only at the fetch limits", () => {
  assert.equal(s.activity.capped.firing, false);
  const big = Array.from({ length: 1000 }, (_, i) => alert(100 + i, "WARNING", 1, `i-${i}`, "ec2", iso(10)));
  assert.equal(summarize({ rows, firing: big, resolved: [], incidents: [], events: [], fleet: null, now: NOW }).activity.capped.firing, true);
});
