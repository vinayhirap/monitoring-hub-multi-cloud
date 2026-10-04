import test from "node:test";
import assert from "node:assert/strict";
import { backoffSkips, visibleInterval } from "../poll.js";

test("backoff grows 0,1,3,7 and is capped at 7", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 10].map(backoffSkips), [0, 1, 3, 7, 7, 7]);
  assert.equal(backoffSkips(-2), 0);
});

function fakeEnv(hidden = false) {
  const listeners = {};
  const doc = {
    hidden,
    addEventListener: (n, f) => { listeners[n] = f; },
    removeEventListener: (n) => { delete listeners[n]; },
  };
  let tick = null; let cleared = false;
  return {
    doc, listeners,
    setI: (f) => { tick = f; return 7; },
    clearI: () => { cleared = true; },
    fire: () => tick(),
    get cleared() { return cleared; },
  };
}

test("runs on every tick while visible", () => {
  const env = fakeEnv(false); let n = 0;
  visibleInterval(() => n++, 1000, env.doc, env.setI, env.clearI);
  env.fire(); env.fire();
  assert.equal(n, 2);
});

test("silent while hidden, then refreshes once on return", () => {
  const env = fakeEnv(true); let n = 0;
  visibleInterval(() => n++, 1000, env.doc, env.setI, env.clearI);
  env.fire(); env.fire();
  assert.equal(n, 0);
  env.doc.hidden = false;
  env.listeners.visibilitychange();
  assert.equal(n, 1);
});

test("cleanup stops the timer and removes the listener", () => {
  const env = fakeEnv(false);
  const stop = visibleInterval(() => {}, 1000, env.doc, env.setI, env.clearI);
  stop();
  assert.equal(env.cleared, true);
  assert.equal(env.listeners.visibilitychange, undefined);
});
