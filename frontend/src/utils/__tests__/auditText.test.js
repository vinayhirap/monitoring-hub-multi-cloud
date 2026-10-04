import test from "node:test";
import assert from "node:assert/strict";
import { humanizeAuditText, humanizePayload } from "../auditText.js";

const names = { 10: "U4RAD", 7: "AuroGov Mumbai" };

test("account ids become account names, in every form the log has used", () => {
  assert.equal(humanizeAuditText("account 10: 1 metric threshold(s) set from defaults", names),
    "U4RAD: recommended defaults applied to 1 metric threshold");
  assert.equal(humanizeAuditText("account 7: 6 metric threshold(s) set from defaults", names),
    "AuroGov Mumbai: recommended defaults applied to 6 metric thresholds");
  assert.equal(humanizeAuditText("account=10 metric_id=312 warn=70 crit=90", names),
    "U4RAD metric #312 set to warning 70, critical 90");
  assert.equal(humanizeAuditText("Account #7 removed", names), "AuroGov Mumbai removed");
});

test("an unknown account id is left alone rather than invented", () => {
  assert.equal(humanizeAuditText("account 99: ok", names), "account 99: ok");
  assert.equal(humanizeAuditText("account 10: ok", {}), "account 10: ok");
});

test("the (s) plural is fixed and ordinary text is untouched", () => {
  assert.equal(humanizeAuditText("3 alert(s) acknowledged", names), "3 alerts acknowledged");
  assert.equal(humanizeAuditText("1 alert(s) acknowledged", names), "1 alert acknowledged");
  assert.equal(humanizeAuditText("Login successful", names), "Login successful");
  assert.equal(humanizeAuditText("U4RAD (aws) id=10", names), "U4RAD (aws) id=10");       // not an "account N" phrase
});

test("non-strings pass through and payload keeps every other field", () => {
  assert.equal(humanizeAuditText(null, names), null);
  assert.equal(humanizeAuditText(undefined, names), undefined);
  const p = { role: "ADMIN", detail: "account 10: 2 metric threshold(s) set from defaults", request_id: "abc" };
  const out = humanizePayload(p, names);
  assert.deepEqual(out, { role: "ADMIN", detail: "U4RAD: recommended defaults applied to 2 metric thresholds", request_id: "abc" });
  assert.equal(p.detail.startsWith("account 10"), true);                                  // original untouched
  assert.equal(humanizePayload(null, names), null);
});
