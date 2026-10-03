import test from "node:test";
import assert from "node:assert/strict";
const store = new Map();
globalThis.localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k), get length() { return store.size; }, key: i => [...store.keys()][i] };
const { getCached, setCached, clearAllCached, blockCacheWrites, allowCacheWrites } = await import("../dataCache.js");

test("cache round trip", () => { setCached("k", { a: 1 }); assert.deepEqual(getCached("k").data, { a: 1 }); });
test("after logout, a late write from an in-flight request is IGNORED (no cross-user leftovers)", () => {
  blockCacheWrites(); clearAllCached();
  setCached("overview:accounts", { accounts: [{ account_name: "previous user's account" }] });   // arrives after logout
  assert.equal(getCached("overview:accounts"), null);
  assert.equal([...store.keys()].filter(k => k.startsWith("mh_cache:")).length, 0);
});
test("the next sign-in re-enables caching", () => { allowCacheWrites(); setCached("x", 1); assert.equal(getCached("x").data, 1); });
test("clearAllCached removes every cached view, including per-scope dashboard views", () => {
  setCached("dash:model:all", { v: 1 }); setCached("dash:model:123", { v: 1 }); setCached("overview:accounts", {});
  clearAllCached(); assert.equal([...store.keys()].filter(k => k.startsWith("mh_cache:")).length, 0);
});
