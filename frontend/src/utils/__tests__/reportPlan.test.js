import test from "node:test";
import assert from "node:assert/strict";
import { templatesFor, quickRanges, periodWindow, validateRequest, trackJob, updateJob, describeRequest, filterLibrary, expiryState, fmtSize, jobIsTerminal } from "../reportPlan.js";

test("client template is admin-only", () => {
  assert.ok(!templatesFor(false).some(t => t.id === "client"));
  assert.ok(templatesFor(true).some(t => t.id === "client"));
});
test("quickRanges are valid ISO with offset and ordered", () => {
  const now = new Date(Date.UTC(2026, 9, 3, 10, 30, 0));
  const r = Object.fromEntries(quickRanges(now).map(x => [x.key, x]));
  assert.equal(r["24h"].start, "2026-10-02T10:30:00+00:00"); assert.equal(r["24h"].end, "2026-10-03T10:30:00+00:00");
  assert.equal(r.mtd.start, "2026-10-01T00:00:00+00:00");
  assert.equal(r.prev.start, "2026-09-01T00:00:00+00:00"); assert.equal(r.prev.end, "2026-10-01T00:00:00+00:00");
  Object.values(r).forEach(x => assert.ok(Date.parse(x.end) > Date.parse(x.start)));
});
test("previous month wraps across a year boundary", () => {
  const r = quickRanges(new Date(Date.UTC(2026, 0, 15))).find(x => x.key === "prev");
  assert.equal(r.start, "2025-12-01T00:00:00+00:00"); assert.equal(r.end, "2026-01-01T00:00:00+00:00");
});
test("periodWindow for presets and custom", () => {
  const now = new Date(Date.UTC(2026, 9, 3));
  assert.equal(periodWindow({ reportType: "WEEKLY" }, now).start.toISOString(), "2026-09-26T00:00:00.000Z");
  assert.equal(periodWindow({ reportType: "CUSTOM", start: "bad", end: "x" }, now), null);
  assert.ok(periodWindow({ reportType: "CUSTOM", start: "2026-09-01T00:00:00+00:00", end: "2026-09-02T00:00:00+00:00" }, now));
});
test("validateRequest mirrors backend rules", () => {
  const ok = { scopeType: "ACCOUNT", reportType: "WEEKLY", accountId: "1", isAdmin: false };
  assert.equal(validateRequest(ok), null);
  assert.match(validateRequest({ ...ok, accountId: "" }), /account/i);
  assert.match(validateRequest({ ...ok, scopeType: "RESOURCE" }), /resource/i);
  assert.match(validateRequest({ ...ok, scopeType: "INCIDENT", scopeId: " " }), /incident/i);
  assert.match(validateRequest({ scopeType: "CLIENT", reportType: "MONTHLY", isAdmin: false, scopeId: "x" }), /admin/i);
  assert.match(validateRequest({ scopeType: "CLIENT", reportType: "MONTHLY", isAdmin: true, scopeId: "" }), /name/i);
  assert.equal(validateRequest({ scopeType: "CLIENT", reportType: "MONTHLY", isAdmin: true, scopeId: "Acme" }), null);
  const c = { ...ok, reportType: "CUSTOM" };
  assert.match(validateRequest(c), /start and an end/);
  assert.match(validateRequest({ ...c, start: "2026-09-02T00:00:00Z", end: "2026-09-01T00:00:00Z" }), /after the start/);
  assert.match(validateRequest({ ...c, start: "2025-01-01T00:00:00Z", end: "2026-09-01T00:00:00Z" }), /400/);
  assert.equal(validateRequest({ ...c, start: "2026-09-01T00:00:00Z", end: "2026-09-10T00:00:00Z" }), null);
});
test("job tracking: newest first, dedupe, cap, update", () => {
  let l = [];
  for (let i = 1; i <= 15; i++) l = trackJob(l, { job_id: i, status: "QUEUED" });
  assert.equal(l.length, 12); assert.equal(l[0].job_id, 15);
  l = trackJob(l, { job_id: 10, status: "QUEUED" }); assert.equal(l[0].job_id, 10); assert.equal(l.filter(j => j.job_id === 10).length, 1);
  l = updateJob(l, 10, { status: "COMPLETE" }); assert.equal(l[0].status, "COMPLETE");
  assert.ok(jobIsTerminal("FAILED") && jobIsTerminal("COMPLETE") && !jobIsTerminal("PROCESSING"));
});
test("describeRequest", () => {
  const s = describeRequest({ template: { label: "Monthly review" }, scopeLabel: "Account Prod", window: { start: new Date(Date.UTC(2026, 8, 3)), end: new Date(Date.UTC(2026, 9, 3)) } });
  assert.match(s, /Monthly review . Account Prod . 03 Sep 2026/);
});
test("filterLibrary and expiryState and fmtSize", () => {
  const rows = [{ report_type: "WEEKLY", scope_type: "ACCOUNT", scope_label: "Prod", account_id: 1, generated_by: "amy" }, { report_type: "MONTHLY", scope_type: "RESOURCE", scope_id: "i-1", account_id: 2 }];
  assert.equal(filterLibrary(rows, { type: "WEEKLY" }).length, 1); assert.equal(filterLibrary(rows, { account: "2" }).length, 1);
  assert.equal(filterLibrary(rows, { q: "AMY" }).length, 1); assert.equal(filterLibrary(rows, { q: "zzz" }).length, 0);
  const now = Date.UTC(2026, 9, 3);
  assert.equal(expiryState("2026-10-01T00:00:00Z", now).state, "expired"); assert.equal(expiryState("2026-10-10T00:00:00Z", now).state, "soon");
  assert.equal(expiryState("2027-10-10T00:00:00Z", now).state, "ok"); assert.equal(expiryState(null, now).state, "unknown");
  assert.equal(fmtSize(500), "500 B"); assert.equal(fmtSize(2048), "2 KB"); assert.equal(fmtSize(3 * 1048576), "3.0 MB");
});

import { TEMPLATES, contentFor, periodMode, reportScopeId, describeRequest as describe2, COST_CONTENT, HOST_CONTENT, ENVIRONMENT_CONTENT, SECURITY_CONTENT, SLO_CONTENT } from "../reportPlan.js";

test("cost template is admin-only; section templates need their section's permission", () => {
  assert.ok(!templatesFor(false).some(t => t.id === "cost"));
  assert.ok(templatesFor(true).some(t => t.id === "cost"));
  const only = new Set(["security.view"]);
  const ids = templatesFor(false, c => only.has(c)).map(t => t.id);
  assert.ok(ids.includes("security") && !ids.includes("host") && !ids.includes("slo") && !ids.includes("environment") && !ids.includes("synthetic"));
  assert.ok(ids.includes("weekly") && ids.includes("incident"));
});
test("every template has its own content list", () => {
  const by = Object.fromEntries(TEMPLATES.map(t => [t.id, contentFor(t)]));
  assert.equal(by.cost, COST_CONTENT); assert.equal(by.host, HOST_CONTENT); assert.equal(by.environment, ENVIRONMENT_CONTENT);
  assert.equal(by.security, SECURITY_CONTENT); assert.equal(by.slo, SLO_CONTENT);
  assert.notEqual(by.weekly, by.cost);
});
test("scope ids are built the way the server parses them", () => {
  assert.equal(reportScopeId({ scopeType: "COST" }), "all");
  assert.equal(reportScopeId({ scopeType: "COST", scopeId: "azure" }), "azure");
  assert.equal(reportScopeId({ scopeType: "HOST", accountId: "3" }), "3");
  assert.equal(reportScopeId({ scopeType: "HOST", accountId: "3", itemId: "7" }), "3:7");
  assert.equal(reportScopeId({ scopeType: "SLO", accountId: "3", itemId: "9" }), "3:9");
  assert.equal(reportScopeId({ scopeType: "ENVIRONMENT", accountId: "3", itemId: "9" }), "3");   // whole account only
  assert.equal(reportScopeId({ scopeType: "SECURITY", accountId: "3" }), "3");
});
test("period modes", () => {
  assert.equal(periodMode("SLO"), "none"); assert.equal(periodMode("ENVIRONMENT"), "trend");
  for (const t of ["COST", "HOST", "SECURITY", "ACCOUNT", "SYNTHETIC"]) assert.equal(periodMode(t), "period");
});
test("validateRequest for the section reports", () => {
  assert.match(validateRequest({ scopeType: "COST", reportType: "MONTHLY", isAdmin: false }), /admin/i);
  assert.equal(validateRequest({ scopeType: "COST", reportType: "MONTHLY", isAdmin: true }), null);
  assert.equal(validateRequest({ scopeType: "COST", reportType: "MONTHLY", isAdmin: true, scopeId: "gcp" }), null);
  assert.match(validateRequest({ scopeType: "COST", reportType: "MONTHLY", isAdmin: true, scopeId: "oracle" }), /AWS/);
  for (const t of ["HOST", "ENVIRONMENT", "SECURITY", "SLO"]) assert.match(validateRequest({ scopeType: t, reportType: "MONTHLY", accountId: "" }), /account/i);
  assert.equal(validateRequest({ scopeType: "HOST", reportType: "MONTHLY", accountId: "2" }), null);
  // an as-of-now report ignores a bad custom range
  assert.equal(validateRequest({ scopeType: "SLO", reportType: "CUSTOM", accountId: "2", start: "", end: "" }), null);
});
test("as-of-now requests are described as such", () => {
  assert.match(describe2({ template: { label: "SLO report" }, reportType: "MONTHLY", scopeLabel: "SLOs in Prod", window: null, asOf: true }), /as of now/);
});
