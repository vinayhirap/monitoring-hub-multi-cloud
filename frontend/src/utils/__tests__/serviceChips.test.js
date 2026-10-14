// node --test src/utils/__tests__/serviceChips.test.js
import test from "node:test";
import assert from "node:assert/strict";
import { buildServiceChips, serviceChipLabel, syncFreshness } from "../serviceChips.js";

const ring = (crit = [], warn = []) => ({ critical_services: crit, warning_services: warn });
const row = (o = {}) => ({
  id: 1, active_services: ["ec2", "ebs", "s3", "alb", "lambda"],
  service_counts: { ec2: 15, ebs: 28, s3: 13, alb: 3, lambda: 1 },
  ec2_total: 15, ec2_running: 10, ec2_stopped: 5,
  health_ring: ring(), ...o,
});

test("one chip per active service, not a fixed four", () => {
  const chips = buildServiceChips([row()]);
  assert.equal(chips.length, 5);
  assert.deepEqual(chips.map(c => c.key).sort(), ["alb", "ebs", "ec2", "lambda", "s3"]);
});

test("alerting services come first, critical before warning, then by count", () => {
  const chips = buildServiceChips([row({ health_ring: ring(["lambda"], ["s3"]) })]);
  // critical lambda, then warning s3, then the healthy ones by count: ebs 28, ec2 15, alb 3
  assert.deepEqual(chips.map(c => c.key), ["lambda", "s3", "ebs", "ec2", "alb"]);
  assert.equal(chips[0].sev, "critical");
  assert.equal(chips[1].sev, "warning");
});

test("a service that only appears because it is alerting is still shown, without a count", () => {
  const chips = buildServiceChips([row({ active_services: ["ec2"], health_ring: ring([], ["waf"]) })]);
  const waf = chips.find(c => c.key === "waf");
  assert.ok(waf);
  assert.equal(waf.count, null);
  assert.equal(waf.sev, "warning");
});

test("falls back to the live per-type total when service_counts has no entry", () => {
  const chips = buildServiceChips([row({ service_counts: undefined, ebs_total: 28 })]);
  assert.equal(chips.find(c => c.key === "ebs").count, 28);
  assert.equal(chips.find(c => c.key === "alb").count, null);   // no live total exists for ALB
});

test("counts add up across the regions of one account, and the chip links to the first region that has it", () => {
  const chips = buildServiceChips([
    row({ id: 10, active_services: ["ec2"], service_counts: { ec2: 4 }, ec2_total: 4, ec2_running: 4, ec2_stopped: 0 }),
    row({ id: 11, active_services: ["ec2", "rds"], service_counts: { ec2: 6, rds: 2 }, ec2_total: 6, ec2_running: 5, ec2_stopped: 1 }),
  ]);
  const ec2 = chips.find(c => c.key === "ec2");
  assert.equal(ec2.count, 10);
  assert.equal(ec2.rowId, 10);
  assert.equal(chips.find(c => c.key === "rds").rowId, 11);
  assert.equal(ec2.sub, "9 running · 1 stopped");
});

test("EC2 running/stopped detail is only shown when it adds up to the chip number", () => {
  assert.equal(buildServiceChips([row()]).find(c => c.key === "ec2").sub, "10 running · 5 stopped");
  const lagging = buildServiceChips([row({ service_counts: { ec2: 14 } })]).find(c => c.key === "ec2");
  assert.equal(lagging.sub, null);
});

test("a worse severity in any region wins for the account chip", () => {
  const chips = buildServiceChips([
    row({ id: 1, active_services: ["ec2"], health_ring: ring([], ["ec2"]) }),
    row({ id: 2, active_services: ["ec2"], health_ring: ring(["ec2"], []) }),
  ]);
  assert.equal(chips[0].sev, "critical");
});

test("no rows or no services gives no chips instead of throwing", () => {
  assert.deepEqual(buildServiceChips([]), []);
  assert.deepEqual(buildServiceChips(undefined), []);
  assert.deepEqual(buildServiceChips([{ id: 1 }]), []);
});

test("labels: known services keep their name, catalog label next, tidy key last", () => {
  assert.equal(serviceChipLabel("ec2"), "EC2");
  assert.equal(serviceChipLabel("alb"), "ALB");
  assert.equal(serviceChipLabel("dynamodb", { dynamodb: "DynamoDB" }), "DynamoDB");
  assert.equal(serviceChipLabel("cloud_run_service"), "Cloud run service");
});

test("syncFreshness uses the least recently synced region", () => {
  const now = Date.parse("2026-10-14T10:00:00Z");
  const min = m => new Date(now - m * 60000).toISOString();
  assert.equal(syncFreshness([{ last_synced_at: min(2) }], now).level, "fresh");
  assert.equal(syncFreshness([{ last_synced_at: min(2) }, { last_synced_at: min(30) }], now).level, "aging");
  assert.equal(syncFreshness([{ last_synced_at: min(2) }, { last_synced_at: min(90) }], now).level, "stale");
  assert.equal(syncFreshness([{ last_synced_at: min(2) }, { last_synced_at: null }], now).level, "unknown");
  assert.equal(syncFreshness([], now).level, "unknown");
});
