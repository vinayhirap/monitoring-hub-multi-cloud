import test from "node:test";
import assert from "node:assert/strict";
import { plural, pluralWord } from "../plural.js";
test("plurals", () => {
  assert.equal(plural(1, "alert"), "1 alert");
  assert.equal(plural(0, "alert"), "0 alerts");
  assert.equal(plural(3, "alert"), "3 alerts");
  assert.equal(plural("2", "binding"), "2 bindings");
  assert.equal(plural(2, "entry", "entries"), "2 entries");
  assert.equal(pluralWord(1, "alert"), "alert");
  assert.equal(pluralWord(5, "alert"), "alerts");
});
