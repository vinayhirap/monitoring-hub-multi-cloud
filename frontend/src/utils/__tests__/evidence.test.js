import test from "node:test";
import assert from "node:assert/strict";
import { seriesStats, freshness, displayAge, breachState, alertsForResource, eventsForResource, buildTimeline, ageText, healthTone, tsMs } from "../evidence.js";

test("seriesStats ignores gaps and reports trend", () => {
  const pts = [10,10,10,10,null,20,20,20,20,20].map((v, i) => ({ t: i, v }));
  const s = seriesStats(pts);
  assert.equal(s.n, 9); assert.equal(s.max, 20); assert.equal(s.min, 10); assert.equal(s.latest, 20);
  assert.equal(s.trend.dir, "up");
});
test("seriesStats: short series has no trend, empty is null", () => {
  assert.equal(seriesStats([{ t: 1, v: 5 }, { t: 2, v: 6 }]).trend, null);
  assert.equal(seriesStats([{ t: 1, v: null }]), null);
  assert.equal(seriesStats(null), null);
});
test("flat series is flat", () => {
  const s = seriesStats(Array.from({ length: 12 }, (_, i) => ({ t: i, v: 50 + (i % 2) * 0.5 })));
  assert.equal(s.trend.dir, "flat");
});
test("freshness follows how often the metric is collected, not the CloudWatch period alone", () => {
  const now = 1e9, min = 60e3;
  // 5-minute metric collected every 5 min: up to ~13 min old is NORMAL. It used to flip to "late" at exactly 15 min.
  assert.equal(freshness(now - 13 * min, now, 300, 300, 1200).state, "fresh");
  assert.equal(freshness(now - 15 * min, now, 300, 300, 1200).state, "fresh");
  assert.equal(freshness(now - 19 * min, now, 300, 300, 1200).state, "fresh");
  assert.equal(freshness(now - 25 * min, now, 300, 300, 1200).state, "late");       // a missed collection
  assert.equal(freshness(now - 50 * min, now, 300, 300, 1200).state, "stale");
  // HOURLY metric (polled every 1 hr): 44 minutes old is perfectly normal, it was shown as "stale"
  assert.equal(freshness(now - 44 * min, now, 300, 3600, 10800).state, "fresh");
  assert.equal(freshness(now - 4 * 60 * min, now, 300, 3600, 10800).state, "late");
  assert.equal(freshness(now - 7 * 60 * min, now, 300, 3600, 10800).state, "stale");
  // without the server value the allowance is derived from the collection interval, then the period
  assert.equal(freshness(now - 44 * min, now, 300, 3600).state, "fresh");
  assert.equal(freshness(now - 19 * min, now, 300).state, "fresh");
  assert.equal(freshness(null, now, 300).state, "none");
  assert.equal(freshness(now - 600e3, now, 0).state, "unknown");                    // cadence unknown: age only, no verdict
  assert.equal(freshness(now - 99999e3, now, null).state, "unknown");
});
test("the age shown is measured from the END of the datapoint's period", () => {
  assert.equal(displayAge(15 * 60e3, 300), 10 * 60e3);        // stamped 11:45, covers 11:45-11:50, now 12:00 -> 10 min, not 15
  assert.equal(displayAge(2 * 60e3, 300), 0);                 // period still open: never negative
  assert.equal(displayAge(null, 300), null);
  assert.equal(displayAge(5 * 60e3, 0), 5 * 60e3);
});
test("breachState handles both comparison directions", () => {
  assert.equal(breachState(95, 80, 90), "critical");
  assert.equal(breachState(85, 80, 90), "warning");
  assert.equal(breachState(10, 80, 90), "ok");
  assert.equal(breachState(5, 20, 10, "<"), "critical");
  assert.equal(breachState(null, 1, 2), null);
});
test("alertsForResource: exact match only, firing then severity", () => {
  const rows = [
    { id: 1, resource: "i-aaa", severity: "WARNING", state: "firing", triggered_at: "2026-10-01T10:00:00Z" },
    { id: 2, resource: "i-aaa", severity: "CRITICAL", state: "resolved", triggered_at: "2026-10-01T09:00:00Z" },
    { id: 3, resource: "i-aaa", severity: "CRITICAL", state: "firing", triggered_at: "2026-10-01T08:00:00Z" },
    { id: 4, resource: "i-aaab", severity: "CRITICAL", state: "firing" },
  ];
  assert.deepEqual(alertsForResource(rows, ["i-aaa"]).map(a => a.id), [3, 1, 2]);
});
test("eventsForResource requires a real mention", () => {
  const ev = [{ message: "restarted i-0abc123", detail: {} }, { message: "other", detail: { id: "i-0abc123" } }, { message: "nothing" }];
  assert.equal(eventsForResource(ev, ["i-0abc123"]).length, 2);
  assert.equal(eventsForResource(ev, ["ab"]).length, 0);
});
test("buildTimeline orders newest first and emits resolve points", () => {
  const t = buildTimeline([{ id: 1, severity: "critical", triggered_at: "2026-10-01T10:00:00Z", resolved_at: "2026-10-01T11:00:00Z" }],
    [{ id: 9, severity: "ERROR", created_at: "2026-10-01T10:30:00Z" }]);
  assert.deepEqual(t.map(x => x.key), ["a1r", "e9", "a1t"]);
});
test("ageText / healthTone / tsMs", () => {
  assert.equal(ageText(30e3), "30s"); assert.equal(ageText(90e3), "2m"); assert.equal(ageText(3 * 3600e3), "3h");
  assert.equal(healthTone(95), "ok"); assert.equal(healthTone(75), "warn"); assert.equal(healthTone(40), "crit"); assert.equal(healthTone(null), "mute");
  assert.equal(tsMs("2026-10-01 10:00:00"), Date.parse("2026-10-01T10:00:00Z"));
});

import { lifecycle } from "../evidence.js";
test("lifecycle reflects only real alert fields", () => {
  const open = lifecycle({ triggered_at: "2026-10-01T10:00:00Z", status: "active" });
  assert.deepEqual(open.map(s => s.done), [true, false, false]);
  const acked = lifecycle({ triggered_at: "x", acked: 1, acked_by: "amy", acked_at: "2026-10-01T10:05:00Z", status: "acknowledged" });
  assert.equal(acked[1].done, true); assert.equal(acked[1].who, "amy"); assert.equal(acked[2].done, false);
  const res = lifecycle({ triggered_at: "x", resolved_at: "2026-10-01T11:00:00Z", resolution_reason: "auto", status: "resolved" });
  assert.equal(res[2].done, true); assert.equal(res[2].why, "auto");
});
