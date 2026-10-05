import test from "node:test";
import assert from "node:assert/strict";
import { describeLimits, formatSpread, fixedValuesCaption, mergeLiveThresholds, markSaved, holdAfterSave, SAVE_HOLD_MS } from "../thresholdLive.js";

const eff = (over = {}) => ({ mode: "dynamic", resources_total: 6, with_limit: 6, learning: 0,
  warning: { min: 1116, median: 2000, max: 4200 }, critical: { min: 1500, median: 2800, max: 5000 }, baseline_updated_at: "2026-10-05T05:00:00Z", ...over });

test("a plain fixed-limit row has nothing extra to say", () => {
  assert.equal(describeLimits({ mode: "static" }, "cpuutilization"), null);
  assert.equal(describeLimits(null, "cpuutilization"), null);
});

test("dynamic rows show the learned spread and how many resources it covers", () => {
  const d = describeLimits(eff(), "volumereadops");
  assert.equal(d.headline, "Learned now: warn 1116 to 4200, crit 1500 to 5000");
  assert.match(d.detail, /6 of 6 resources have a learned limit/);
  assert.equal(describeLimits(eff({ with_limit: 4, learning: 2 }), "volumereadops").detail.includes("2 still learning"), true);
});

test("anomaly-only rows say it is a learned line, and never quote the placeholders", () => {
  const d = describeLimits(eff({ mode: "anomaly", warning: { min: 3300, median: 3300, max: 3300 }, critical: { min: 3300, median: 3300, max: 3300 } }), "volumereadops");
  assert.match(d.headline, /^Learned alert line now: 3\.3K|3300$/);
  assert.ok(!/1000000|5000000/.test(d.headline + d.detail));
});

test("no learned limit yet is explained instead of showing blanks", () => {
  const d = describeLimits(eff({ with_limit: 0, warning: null, critical: null }), "networkin");
  assert.equal(d.headline, "Using the fixed values for now");
  assert.equal(describeLimits(eff({ mode: "anomaly", with_limit: 0, warning: null }), "x").headline, "No learned line yet");
  assert.match(describeLimits(eff({ resources_total: 0, with_limit: 0 }), "x").detail, /No resources/);
});

test("captions relabel the typed inputs honestly", () => {
  assert.match(fixedValuesCaption("dynamic"), /only until/);
  assert.match(fixedValuesCaption("anomaly"), /not enforced/);
  assert.equal(fixedValuesCaption("static"), "");
  assert.equal(formatSpread(null, "x"), "n/a");
  assert.equal(formatSpread({ min: 5, median: 5, max: 5 }, "x"), "5");
});

const rows = () => [{ id: 1, warning_value: 70, critical_value: 90, use_dynamic: 0, enabled: 1 }, { id: 2, warning_value: 1, critical_value: 5, use_dynamic: 0, enabled: 1 }];
const srv = (o = {}) => ({ 1: { mode: "static", use_dynamic: false, dynamic_k: 3, enabled: true, warning_value: 70, critical_value: 90 },
                           2: { mode: "dynamic", use_dynamic: true, dynamic_k: 3, enabled: true, warning_value: 1, critical_value: 5, ...o } });

test("the auto-tuner flipping a row to dynamic shows up without a reload", () => {
  const { rows: out } = mergeLiveThresholds(rows(), srv(), {});
  assert.equal(out[1].use_dynamic, 1); assert.equal(out[1].mode, "dynamic"); assert.equal(out[0].use_dynamic, 0);
});

test("a value somebody is typing is never overwritten by a poll", () => {
  const first = mergeLiveThresholds(rows(), srv(), {});
  const edited = first.rows.map(t => (t.id === 2 ? { ...t, warning_value: "7" } : t));          // person types 7
  const polled = mergeLiveThresholds(edited, srv({ warning_value: 2, critical_value: 8 }), first.lastServer);
  assert.equal(polled.rows[1].warning_value, "7");                                                // kept
  assert.equal(polled.rows[1].critical_value, 5);                                                 // card is "dirty": nothing changes
  assert.equal(polled.rows[0].warning_value, 70);
});

test("an untouched card follows a change made elsewhere (another admin, the tuner)", () => {
  const first = mergeLiveThresholds(rows(), srv(), {});
  const polled = mergeLiveThresholds(first.rows, srv({ warning_value: 2, critical_value: 8 }), first.lastServer);
  assert.equal(polled.rows[1].warning_value, 2); assert.equal(polled.rows[1].critical_value, 8);
});

test("after Save the saved values stay on screen even if another worker answers from its old cache", () => {
  const first = mergeLiveThresholds(rows(), srv(), {});
  const saved = first.rows.map(t => (t.id === 2 ? { ...t, warning_value: "7", critical_value: "9" } : t));
  const last = markSaved(first.lastServer, saved[1]);
  const holds = holdAfterSave({}, saved[1], 1000);
  const stale = mergeLiveThresholds(saved, srv(), last, holds, 5000);                              // stale cache: still 1 / 5
  assert.equal(saved[1].warning_value, "7");
  assert.equal(stale.rows[1].warning_value, "7"); assert.equal(stale.rows[1].critical_value, "9");
  const fresh = mergeLiveThresholds(saved, srv({ warning_value: 7, critical_value: 9 }), last, holds, 6000);
  assert.equal(Number(fresh.rows[1].warning_value), 7);                                            // server caught up
  const later = mergeLiveThresholds(saved, srv({ warning_value: 3, critical_value: 4 }), last, holds, 1000 + SAVE_HOLD_MS + 1);
  assert.equal(Number(later.rows[1].warning_value), 3);                                             // hold over: a real change elsewhere is adopted
});

test("rows the server does not know about are left alone", () => {
  const { rows: out } = mergeLiveThresholds([{ id: 99, warning_value: 1, critical_value: 2 }], srv(), {});
  assert.deepEqual(out, [{ id: 99, warning_value: 1, critical_value: 2 }]);
  assert.deepEqual(mergeLiveThresholds(null, null).rows, []);
});

import { ageText } from "../thresholdLive.js";
test("age text", () => {
  const now = Date.parse("2026-10-05T06:00:00Z");
  assert.equal(ageText("2026-10-05T06:00:00Z", now), "just now");
  assert.equal(ageText("2026-10-05T05:48:00Z", now), "12 min ago");
  assert.equal(ageText("2026-10-05T03:00:00Z", now), "3 h ago");
  assert.equal(ageText("2026-10-03T06:00:00Z", now), "2 d ago");
  assert.equal(ageText(null, now), ""); assert.equal(ageText("junk", now), "");
});
