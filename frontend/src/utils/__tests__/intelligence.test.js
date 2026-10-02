import test from "node:test";
import assert from "node:assert/strict";
import { detectAnomaly, coMovement, projectCrossing, actionFor, buildInsights, median, mad } from "../intelligence.js";

const T0 = Date.UTC(2026, 9, 1, 0, 0, 0), STEP = 300e3;
const mk = (n, f) => Array.from({ length: n }, (_, i) => ({ t: T0 + i * STEP, v: f(i) }));
const noise = i => ((i * 7919) % 13) / 13;            // deterministic pseudo-noise in [0,1)

test("median/mad", () => { assert.equal(median([1, 9, 3]), 3); assert.equal(mad([1, 2, 3, 4, 100]), 1); });
test("insufficient history is reported, not guessed", () => assert.equal(detectAnomaly(mk(10, () => 5)).status, "insufficient"));
test("flat noisy series is normal", () => assert.equal(detectAnomaly(mk(60, i => 50 + noise(i) * 4)).status, "normal"));
test("sustained step is anomalous with onset at the step", () => {
  const a = detectAnomaly(mk(60, i => (i >= 54 ? 95 : 50 + noise(i) * 4)));
  assert.equal(a.status, "anomalous"); assert.equal(a.dir, "up");
  assert.equal(a.onset, T0 + 54 * STEP); assert.equal(a.runLength, 6);
});
test("single-point spike is not flagged", () => assert.equal(detectAnomaly(mk(60, i => (i === 59 ? 95 : 50 + noise(i) * 4))).status, "normal"));
test("downward anomaly detected", () => assert.equal(detectAnomaly(mk(60, i => (i >= 55 ? 5 : 50 + noise(i) * 4))).dir, "down"));
test("tiny but statistically tight wobble is ignored", () => assert.equal(detectAnomaly(mk(60, i => (i >= 55 ? 100.5 : 100 + (i % 2) * 0.001))).status, "normal"));
test("constant-zero baseline that suddenly moves is flagged", () => assert.equal(detectAnomaly(mk(60, i => (i >= 55 ? 3 : 0))).status, "anomalous"));
test("gaps (null) are ignored", () => {
  const s = mk(60, i => (i >= 54 ? 95 : 50 + noise(i) * 4)); s[10].v = null; s[20].v = null;
  assert.equal(detectAnomaly(s).status, "anomalous");
});

// Deterministic PRNG so the false-positive-rate tests are reproducible
const prng = seed => { let s = seed; return () => (s = (s * 1664525 + 1013904223) % 4294967296) / 4294967296; };
const sparseSeries = (rnd, n, p, max) => mk(n, () => (rnd() < p ? 1 + Math.floor(rnd() * max) : 0));
test("sparse/bursty stationary metrics (MAD = 0) do not raise false anomalies (< 1% of series)", () => {
  const rnd = prng(12345); let flagged = 0; const N = 1500;
  for (let k = 0; k < N; k++) if (detectAnomaly(sparseSeries(rnd, 72, 0.3, 10)).status === "anomalous") flagged++;
  assert.ok(flagged / N < 0.01, `false-positive rate ${(100 * flagged / N).toFixed(1)}%`);
});
test("heavy-tailed stationary traffic stays under 1% false positives", () => {
  const rnd = prng(777); let flagged = 0; const N = 1500;
  for (let k = 0; k < N; k++) if (detectAnomaly(mk(72, () => 1000 * Math.exp(1.2 * Math.sqrt(-2 * Math.log(rnd() + 1e-9)) * Math.cos(2 * Math.PI * rnd())))).status === "anomalous") flagged++;
  assert.ok(flagged / N < 0.01, `false-positive rate ${(100 * flagged / N).toFixed(1)}%`);
});
test("a genuinely new level on a sparse metric is still detected (above everything seen before)", () => {
  const rnd = prng(99); const base = sparseSeries(rnd, 60, 0.3, 5);
  const s = base.map((p, i) => (i >= 54 ? { ...p, v: 40 } : p));
  assert.equal(detectAnomaly(s).status, "anomalous");
});
test("a sustained level inside the metric's own historical range is not an anomaly", () => {
  const s = mk(60, i => (i >= 54 ? 53.9 : 50 + noise(i) * 4));        // baseline spans 50..54
  assert.equal(detectAnomaly(s).status, "normal");
});
test("buildInsights over 40 series x 500 points is fast enough to run on every chart refresh", () => {
  const series = Array.from({ length: 40 }, (_, k) => ({ key: "m" + k, title: "M" + k, pts: mk(500, i => 50 + noise(i + k) * 5 + (k % 5 === 0 && i >= 494 ? 60 : 0)) }));
  const t0 = Date.now(); const r = buildInsights({ series }); const ms = Date.now() - t0;
  assert.ok(ms < 600, `took ${ms} ms`); assert.ok(r.insights.length >= 1);
});
test("coMovement finds a lead/lag relationship and ignores unrelated series", () => {
  const a = mk(60, i => (i % 9 === 0 ? 10 : 1) + noise(i) * 0.2);
  const b = mk(60, i => (i % 9 === 1 ? 10 : 1) + noise(i + 3) * 0.2);       // b follows a by one step
  const c = coMovement(a, b);
  assert.ok(c.r > 0.8, `r=${c.r}`); assert.equal(c.lag, 1);
  const u = mk(60, i => noise(i * 31 + 5));
  assert.ok(Math.abs(coMovement(a, u).r) < 0.5);
  assert.equal(coMovement(mk(5, () => 1), a), null);
});
test("a shared slow trend is not reported as co-movement", () => {
  const a = mk(60, i => i + noise(i) * 0.5), b = mk(60, i => 2 * i + noise(i + 9) * 0.5);
  const c = coMovement(a, b); assert.ok(!c || Math.abs(c.r) < 0.7, JSON.stringify(c));
});

test("projectCrossing: rising series reaches threshold", () => {
  const p = projectCrossing(mk(36, i => 50 + i * 0.5), 90, ">");      // +6/h, last ~67.5 -> ~3.75h
  assert.ok(p && Math.abs(p.hours - 3.75) < 0.2 && p.r2 > 0.99, JSON.stringify(p));
});
test("projectCrossing: already past, flat, receding, or far-off give null", () => {
  assert.equal(projectCrossing(mk(36, i => 95 + i * 0.1), 90, ">"), null);
  assert.equal(projectCrossing(mk(36, () => 50), 90, ">"), null);
  assert.equal(projectCrossing(mk(36, i => 50 - i * 0.5), 90, ">"), null);
  assert.equal(projectCrossing(mk(36, i => 10 + i * 0.001 + noise(i)), 90, ">"), null);
});
test("projectCrossing supports '<' thresholds (free space)", () => {
  const p = projectCrossing(mk(36, i => 60 - i * 0.5), 20, "<"); assert.ok(p && p.hours > 0);
});

test("actionFor maps by metric family with a safe default", () => {
  assert.match(actionFor("CPUUtilization"), /CPU/); assert.match(actionFor("disk_used_percent"), /clean up/);
  assert.match(actionFor("NetworkOut"), /traffic/); assert.match(actionFor("Zzz"), /evidence timeline/);
});

test("buildInsights chains anomaly + co-moving metric + nearby event + firing alert", () => {
  const cpu = { key: "CPUUtilization", title: "CPU utilization", unit: "Percent", crit: 90, warn: 80, cmp: ">", pts: mk(60, i => (i >= 54 ? 95 : 50 + noise(i) * 4)) };
  const net = { key: "NetworkOut", title: "Network out", unit: "Bytes", pts: mk(60, i => (i >= 53 ? 5000 : 1000 + noise(i) * 100)) };
  const calm = { key: "Mem", title: "Memory", unit: "Percent", pts: mk(60, i => 40 + noise(i)) };
  const ev = [{ event_type: "instance_restart", created_at: new Date(T0 + 50 * STEP).toISOString() }];
  const { insights, coverage } = buildInsights({ series: [cpu, net, calm], alerts: [{ state: "firing", metric_name: "CPUUtilization" }], events: ev, fmt: (s, v) => String(Math.round(v)), tz: "UTC" });
  assert.equal(coverage.analysed, 3);
  const a = insights.find(i => i.id === "an:CPUUtilization");
  assert.equal(a.level, "high"); assert.ok(a.metrics.includes("NetworkOut")); assert.equal(a.signals.events, 1);
  assert.equal(a.leader, "Network out"); assert.equal(a.confidence, "high");
  assert.match(a.text, /Network out moved first/); assert.match(a.text, /instance_restart/);
  assert.ok(!insights.some(i => i.id === "an:Mem"));
  assert.ok(!insights.some(i => i.id === "an:NetworkOut"), "co-moving metric is folded into one cluster card");
  assert.match(a.action, /Start with Network out, which moved first/);
});
test("buildInsights is quiet when nothing is wrong and reports coverage", () => {
  const r = buildInsights({ series: [{ key: "a", title: "A", pts: mk(60, i => 5 + noise(i)) }, { key: "b", title: "B", pts: mk(5, () => 1) }] });
  assert.equal(r.insights.length, 0); assert.deepEqual(r.coverage, { analysed: 1, total: 2 });
});
test("forecast on a '<' metric (free space) picks the nearer threshold too", () => {
  const s = { key: "Free", title: "Free space", unit: "Percent", warn: 30, crit: 15, cmp: "<", pts: mk(36, i => 60 - i * 0.5) };   // last 42.5, slope -6/h: warn 2.1 h, crit 4.6 h
  const f = buildInsights({ series: [s], fmt: (x, v) => `${v}%` }).insights.find(i => i.kind === "forecast");
  assert.ok(f && /warning/.test(f.title) && f.hours > 1.5 && f.hours < 3, JSON.stringify(f));
});
test("buildInsights forecast for a rising metric", () => {
  const s = { key: "Disk", title: "Disk used", unit: "Percent", warn: 80, crit: 90, cmp: ">", pts: mk(36, i => 50 + i * 0.5) };
  const r = buildInsights({ series: [s], fmt: (x, v) => `${v}%` });
  const f = r.insights.find(i => i.kind === "forecast");
  assert.ok(f && /warning/.test(f.title), "leads with the earlier (warning) threshold");           // warning 80 is reached in ~2.1 h, critical 90 in ~3.75 h
  assert.ok(f.hours > 1.5 && f.hours < 3, String(f.hours));
  assert.match(f.text, /warning threshold \(80%\) in about 2\.\d h, and its critical threshold \(90%\) in about 3\.\d h/);
});
