import test from "node:test";
import assert from "node:assert/strict";
import { isChunkError, loadWithRetry } from "../lazyWithRetry.js";

const store = () => { const m = new Map(); return { getItem: k => m.get(k) ?? null, setItem: (k, v) => m.set(k, v), removeItem: k => m.delete(k), m }; };
const chunkErr = () => new TypeError("Failed to fetch dynamically imported module: https://x/assets/Alerts-abc.js");

test("recognises stale-chunk errors across browsers, and ignores real bugs", () => {
  assert.ok(isChunkError(chunkErr()));
  assert.ok(isChunkError(new Error("error loading dynamically imported module")));
  assert.ok(isChunkError(new Error("Importing a module script failed.")));
  assert.ok(isChunkError({ name: "ChunkLoadError", message: "Loading chunk 12 failed." }));
  assert.ok(!isChunkError(new Error("x is not a function")));
});

test("success clears the retry flag and returns the module", async () => {
  const s = store(); s.setItem("mh:chunk-reload", "1");
  const mod = await loadWithRetry(() => Promise.resolve({ default: 1 }), s, () => assert.fail("no reload"));
  assert.deepEqual(mod, { default: 1 });
  assert.equal(s.getItem("mh:chunk-reload"), null);
});

test("first stale-chunk failure reloads once and never resolves", async () => {
  const s = store(); let reloads = 0;
  const p = loadWithRetry(() => Promise.reject(chunkErr()), s, () => reloads++);
  const raced = await Promise.race([p.then(() => "resolved", () => "rejected"), new Promise(r => setTimeout(() => r("pending"), 20))]);
  assert.equal(raced, "pending"); assert.equal(reloads, 1); assert.equal(s.getItem("mh:chunk-reload"), "1");
});

test("a second failure in the same session is surfaced, not looped", async () => {
  const s = store(); s.setItem("mh:chunk-reload", "1"); let reloads = 0;
  await assert.rejects(loadWithRetry(() => Promise.reject(chunkErr()), s, () => reloads++), /dynamically imported/);
  assert.equal(reloads, 0);
});

test("non-chunk errors and blocked storage never trigger a reload", async () => {
  let reloads = 0;
  await assert.rejects(loadWithRetry(() => Promise.reject(new Error("boom")), store(), () => reloads++), /boom/);
  const blocked = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() {} };
  await assert.rejects(loadWithRetry(() => Promise.reject(chunkErr()), blocked, () => reloads++), /dynamically imported/);
  assert.equal(reloads, 0);
});
