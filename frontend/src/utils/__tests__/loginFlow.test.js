import test from "node:test";
import assert from "node:assert/strict";
import { sanitizeNext, parseRetryAfter, formatWait, loginFailure, FALLBACK_AFTER_LOGIN, markSessionExpired, takeSessionExpired, EXPIRED_KEY } from "../loginFlow.js";

test("sanitizeNext keeps ordinary in-app paths (with query and hash)", () => {
  for (const ok of ["/overview", "/alerts?tab=attention", "/accounts/1/ec2?resource=i-0abc#x", "/settings#accounts", "/reports"]) assert.equal(sanitizeNext(ok), ok);
});
test("sanitizeNext rejects every way of leaving the site (open-redirect attempts)", () => {
  const attacks = ["//evil.com", "///evil.com", "https://evil.com", "http://evil.com/x", "javascript:alert(1)", "/\\evil.com", "\\\\evil.com", "/%2Fevil.com", "/%2f%2fevil.com", "/%5Cevil.com",
    "/\t/evil.com", "/\n/evil.com", "/javascript:alert(1)", "/http://evil.com", "evil.com", "  //evil.com", "/%E0%A4%A", "data:text/html,x", "/ok\u0000"];
  for (const a of attacks) assert.equal(sanitizeNext(a), null, JSON.stringify(a));
});
test("sanitizeNext never returns the login page (no loop) and handles junk input", () => {
  for (const a of ["/login", "/login?next=/x", "/login/", "/logout", null, undefined, 42, {}, "", "   ", "x".repeat(2100)]) assert.equal(sanitizeNext(a), null);
});
test("parseRetryAfter: seconds, HTTP date, junk, clamping", () => {
  assert.equal(parseRetryAfter("30"), 30); assert.equal(parseRetryAfter("0"), 1); assert.equal(parseRetryAfter("999999"), 3600);
  assert.equal(parseRetryAfter(null), null); assert.equal(parseRetryAfter("soon"), null); assert.equal(parseRetryAfter(""), null);
  const now = Date.UTC(2026, 9, 3, 12, 0, 0);
  assert.equal(parseRetryAfter(new Date(now + 90000).toUTCString(), now), 90);
  assert.equal(parseRetryAfter(new Date(now - 90000).toUTCString(), now), 1);          // a date in the past -> minimum 1 s, never negative
});
test("formatWait", () => { assert.equal(formatWait(1), "1 second"); assert.equal(formatWait(45), "45 seconds"); assert.equal(formatWait(60), "1 minute"); assert.equal(formatWait(61), "2 minutes"); });
test("loginFailure tells wrong password, lockout, server and network apart", () => {
  assert.equal(loginFailure({ status: 401 }).kind, "credentials"); assert.equal(loginFailure({ status: 401 }).fieldsInvalid, true);
  const rl = loginFailure({ status: 429, retryAfter: "120" });
  assert.equal(rl.kind, "rate-limited"); assert.equal(rl.waitSec, 120); assert.match(rl.message, /2 minutes/); assert.equal(rl.fieldsInvalid, false);
  assert.equal(loginFailure({ status: 429 }).waitSec, 60);                                // no header: assume a minute rather than "0"
  assert.equal(loginFailure({ status: 500 }).kind, "server"); assert.equal(loginFailure({ status: 503 }).kind, "server");
  assert.equal(loginFailure({ network: true }).kind, "network"); assert.equal(loginFailure({ status: 403 }).kind, "denied");
  assert.equal(loginFailure().kind, "unknown");
});
test("a wrong password and an unknown user get the SAME message (no username enumeration)", () => {
  assert.equal(loginFailure({ status: 401 }).message, "Invalid username or password.");
});
test("FALLBACK_AFTER_LOGIN", () => { assert.equal(FALLBACK_AFTER_LOGIN, "/overview"); });

const fakeStore = () => { const m = new Map(); return { getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k), m }; };
test("session-expired note: set by the API handler, shown once, ignored when old", () => {
  const st = fakeStore(), t0 = 1_000_000;
  assert.equal(takeSessionExpired("", st, t0), false);                              // nothing recorded
  markSessionExpired(st, t0); assert.equal(takeSessionExpired("", st, t0 + 5000), true);
  assert.equal(takeSessionExpired("", st, t0 + 6000), false);                       // consumed: not shown twice
  markSessionExpired(st, t0); assert.equal(takeSessionExpired("", st, t0 + 10 * 60 * 1000), false);   // stale note from an earlier session
  assert.equal(st.m.has(EXPIRED_KEY), false);
});
test("the ?reason=expired link still works, and broken storage never throws", () => {
  assert.equal(takeSessionExpired("?reason=expired&next=%2Falerts", fakeStore()), true);
  assert.equal(takeSessionExpired("?reason=other", fakeStore()), false);
  const broken = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() { throw new Error("denied"); } };
  assert.doesNotThrow(() => markSessionExpired(broken)); assert.equal(takeSessionExpired("", broken), false); assert.equal(takeSessionExpired("?reason=expired", broken), true);
});
