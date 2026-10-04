import test from "node:test";
import assert from "node:assert/strict";
import { formatStamp, formatShort, formatDay } from "../timeFormat.js";

const T = "2026-10-04T09:48:58Z";

test("same instant, both zones, always labelled", () => {
  assert.equal(formatStamp(T, "Asia/Kolkata", "IST"), "04 Oct 2026, 15:18:58 IST");
  assert.equal(formatStamp(T, "UTC", "UTC"), "04 Oct 2026, 09:48:58 UTC");
});

test("day-first with a month name: never ambiguous", () => {
  assert.equal(formatDay("2026-03-10T05:00:00Z", "UTC"), "10 Mar 2026");
  assert.equal(formatDay("2026-10-03T05:00:00Z", "UTC"), "03 Oct 2026");
});

test("midnight is 00:xx, not 24:xx", () => {
  assert.equal(formatStamp("2026-10-04T00:05:09Z", "UTC", "UTC"), "04 Oct 2026, 00:05:09 UTC");
  assert.equal(formatShort("2026-10-04T18:30:00Z", "Asia/Kolkata", "IST"), "05 Oct, 00:00 IST");
});

test("MySQL-style naive timestamps are read as UTC, ISO with Z or offset is respected", () => {
  assert.equal(formatStamp("2026-10-04 09:48:58", "UTC", "UTC"), "04 Oct 2026, 09:48:58 UTC");
  assert.equal(formatStamp("2026-10-04T15:18:58+05:30", "UTC", "UTC"), "04 Oct 2026, 09:48:58 UTC");
});

test("garbage, empty and null fall back instead of 'Invalid Date'", () => {
  for (const bad of [null, undefined, "", "not a date", NaN]) {
    assert.equal(formatStamp(bad, "UTC", "UTC"), "—");
    assert.equal(formatDay(bad, "UTC"), "—");
  }
  assert.equal(formatStamp("x", "UTC", "UTC", "n/a"), "n/a");
});

test("accepts a Date and a label-less call", () => {
  assert.equal(formatStamp(new Date(T), "UTC"), "04 Oct 2026, 09:48:58");
});

import { zoneLabel } from "../timeFormat.js";
test("zoneLabel maps the two supported zones and passes others through", () => {
  assert.equal(zoneLabel("Asia/Kolkata"), "IST");
  assert.equal(zoneLabel("UTC"), "UTC");
  assert.equal(zoneLabel("Europe/Paris"), "Europe/Paris");
  assert.equal(zoneLabel(undefined), "");
});
