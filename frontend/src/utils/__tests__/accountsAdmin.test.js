import test from "node:test";
import assert from "node:assert/strict";
import { removalPhrase, groupAccounts, removalError, regionStatusTone } from "../accountsAdmin.js";

test("removalPhrase is account/region, so two regions of one account need different phrases", () => {
  assert.equal(removalPhrase({ account_name: "U4RAD", region: "ap-south-1" }), "U4RAD/ap-south-1");
  assert.notEqual(removalPhrase({ account_name: "Prod", region: "ap-south-1" }), removalPhrase({ account_name: "Prod", region: "us-east-1" }));
  assert.equal(removalPhrase(null), "/");
});
test("groupAccounts groups regions under one cloud account and sorts both levels", () => {
  const g = groupAccounts([
    { id: 3, account_id: "2", account_name: "Zed", region: "us-east-1" },
    { id: 1, account_id: "1", account_name: "Alpha", region: "us-east-1" },
    { id: 2, account_id: "1", account_name: "Alpha", region: "ap-south-1" }]);
  assert.deepEqual(g.map(x => x.account_name), ["Alpha", "Zed"]);
  assert.deepEqual(g[0].regions.map(r => r.region), ["ap-south-1", "us-east-1"]);
  assert.deepEqual(groupAccounts(null), []);
});
test("removalError maps HTTP status to an honest message", () => {
  assert.equal(removalError(new Error("403 Forbidden")).kind, "denied");
  assert.equal(removalError(new Error("404 Not Found")).gone, true);
  assert.equal(removalError(new Error("401")).kind, "auth");
  assert.equal(removalError(new Error("boom")).kind, "error");
  assert.equal(removalError(undefined).gone, false);
});
test("regionStatusTone", () => { assert.equal(regionStatusTone("critical"), "crit"); assert.equal(regionStatusTone("x"), "mute"); });
