import test from "node:test";
import assert from "node:assert/strict";
import { buildChecklist } from "../setupChecklist.js";

const status = (done) => ({
  steps: ["accounts", "notifications", "synthetic", "slo", "status_page", "escalation"]
    .map(key => ({ key, done: done.includes(key), hint: `hint ${key}` })),
});
const all = () => true;

test("lists only the steps that are not done, in order, with links", () => {
  const c = buildChecklist(status(["accounts"]), all, "admin");
  assert.equal(c.show, true);
  assert.deepEqual(c.remaining.map(r => r.key), ["notifications", "synthetic", "slo", "status_page", "escalation"]);
  assert.equal(c.done, 1);
  assert.equal(c.total, 6);
  assert.equal(c.remaining[0].to, "/settings#notifications");
});

test("hidden once everything is set up", () => {
  const c = buildChecklist(status(["accounts", "notifications", "synthetic", "slo", "status_page", "escalation"]), all, "admin");
  assert.equal(c.show, false);
  assert.equal(c.done, 6);
});

test("only administrators see it", () => {
  for (const role of ["viewer", "editor", "", undefined]) {
    assert.equal(buildChecklist(status([]), all, role).show, false, String(role));
  }
});

test("a step the admin cannot act on is not offered", () => {
  const noNotify = p => p !== "notifications.manage";
  const c = buildChecklist(status(["accounts"]), noNotify, "admin");
  assert.ok(!c.remaining.some(r => r.key === "notifications"));
  assert.ok(c.remaining.some(r => r.key === "slo"));
});

test("bad or missing status never throws and shows nothing", () => {
  for (const bad of [null, undefined, {}, { steps: "x" }]) {
    assert.equal(buildChecklist(bad, all, "admin").show, false);
  }
  const c = buildChecklist({ steps: [{ key: "unknown_future_step", done: false, hint: "" }] }, all, "admin");
  assert.equal(c.show, false);
});
