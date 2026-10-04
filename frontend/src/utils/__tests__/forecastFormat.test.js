import test from "node:test";
import assert from "node:assert/strict";
import { roundDays, formatDaysLeft, formatDaysLeftShort } from "../forecastFormat.js";
import { shouldLoadBrandVideo, readEnvironment } from "../loginMedia.js";

test("near-term forecasts are whole days, far ones are rounded and honest", () => {
  assert.equal(formatDaysLeft(11.2), "11 days");
  assert.equal(formatDaysLeft(1), "1 day");
  assert.equal(formatDaysLeft(0.4), "under a day");
  assert.equal(formatDaysLeft(283.7), "about 280 days");      // was "~283.7 days"
  assert.equal(formatDaysLeft(47), "about 45 days");
  assert.equal(formatDaysLeft(400), "over a year");
});

test("short badge form", () => {
  assert.equal(formatDaysLeftShort(11.4), "11d left");
  assert.equal(formatDaysLeftShort(283.7), "~280d left");
  assert.equal(formatDaysLeftShort(0.2), "<1d left");
  assert.equal(formatDaysLeftShort(500), "1y+ left");
});

test("bad input never prints NaN", () => {
  for (const bad of [null, undefined, "x", -3, NaN]) {
    assert.equal(roundDays(bad), null);
    assert.equal(formatDaysLeft(bad), "unknown");
    assert.equal(formatDaysLeftShort(bad), "n/a");
  }
});

test("the brand video loads only on wide screens without reduced-motion or data-saver", () => {
  assert.equal(shouldLoadBrandVideo({ wide: true, reducedMotion: false, saveData: false }), true);
  assert.equal(shouldLoadBrandVideo({ wide: false, reducedMotion: false, saveData: false }), false);
  assert.equal(shouldLoadBrandVideo({ wide: true, reducedMotion: true, saveData: false }), false);
  assert.equal(shouldLoadBrandVideo({ wide: true, reducedMotion: false, saveData: true }), false);
});

test("environment detection reads media queries and falls back safely", () => {
  const win = { matchMedia: q => ({ matches: q.includes("min-width") }), navigator: { connection: { saveData: false } } };
  assert.deepEqual(readEnvironment(win), { wide: true, reducedMotion: false, saveData: false });
  assert.deepEqual(readEnvironment({}), { wide: false, reducedMotion: false, saveData: false });
  assert.equal(shouldLoadBrandVideo(readEnvironment(undefined)), false);
});

import { serviceLabelFor } from "../dashboardModel.js";
test("one name per load balancer kind, taken from the ARN", () => {
  const arn = kind => `arn:aws:elasticloadbalancing:ap-south-1:1:loadbalancer/${kind}/xrai/abc`;
  assert.equal(serviceLabelFor("elb", arn("app")), "ALB");
  assert.equal(serviceLabelFor("elb", arn("net")), "NLB");
  assert.equal(serviceLabelFor("elb", arn("gwy")), "GWLB");
  assert.equal(serviceLabelFor("elb", "classic-lb-name"), "ELB");      // nothing to go on: unchanged
  assert.equal(serviceLabelFor("ec2", "i-1"), "EC2");
  assert.equal(serviceLabelFor(undefined, undefined), "OTHER");
});
