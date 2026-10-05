import test from "node:test";
import assert from "node:assert/strict";
import { filenameFromDisposition, safeFileName, describeApiError } from "../download.js";

test("file name is read from every Content-Disposition form", () => {
  assert.equal(filenameFromDisposition('attachment; filename="CloudOps-Weekly-Report-U4RAD-2026-09-26_to_2026-10-03.pdf"'),
    "CloudOps-Weekly-Report-U4RAD-2026-09-26_to_2026-10-03.pdf");
  assert.equal(filenameFromDisposition("attachment; filename=report.pdf"), "report.pdf");
  assert.equal(filenameFromDisposition("attachment; filename*=UTF-8''Caf%C3%A9%20report.pdf"), "Café report.pdf");
  assert.equal(filenameFromDisposition('attachment; filename="a b.pdf"; size=10'), "a b.pdf");
  assert.equal(filenameFromDisposition("attachment"), null);
  assert.equal(filenameFromDisposition(null), null);
});

test("unsafe names are neutralised and an empty one falls back", () => {
  assert.equal(safeFileName("../../etc/passwd"), "passwd");
  assert.equal(safeFileName("C:\\Users\\x\\report.pdf"), "report.pdf");
  assert.equal(safeFileName('a:b*c?"d<e>f|g.pdf'), "a-b-c-d-e-f-g.pdf");
  assert.equal(safeFileName("...hidden.pdf"), "hidden.pdf");
  assert.equal(safeFileName("", "fallback.pdf"), "fallback.pdf");
  assert.equal(safeFileName(null), "download");
});

test("errors are described for people, not as 'API /path -> 500'", () => {
  const e500 = Object.assign(new Error("API /api/reports/generate?x=1 \u2192 500"), { status: 500, requestId: "abc123" });
  const msg = describeApiError(e500, "queuing the report");
  assert.match(msg, /server hit a problem while queuing the report/);
  assert.match(msg, /reference abc123/);
  assert.ok(!msg.includes("/api/"));
  assert.equal(describeApiError(Object.assign(new Error("x"), { status: 500 }), "saving"), "The server hit a problem while saving. Please try again.");
  assert.equal(describeApiError(Object.assign(new Error("x"), { status: 409, detail: "Cannot delete the last admin" })), "Cannot delete the last admin");
  assert.match(describeApiError(Object.assign(new Error("x"), { status: 403 })), /permission/);
  assert.match(describeApiError(Object.assign(new Error("x"), { status: 429 })), /Too many/);
  assert.match(describeApiError(new TypeError("Failed to fetch")), /Cannot reach the server/);
  assert.match(describeApiError(null, "loading"), /while loading/);
});
