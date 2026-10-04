import test from "node:test";
import assert from "node:assert/strict";
import { filterFindings, paginate, pageCount, checkTypes, PAGE_SIZE } from "../findingsView.js";

const label = id => ({ sg_open_to_world: "Security group open to internet", ebs_unattached: "Unattached EBS volume" }[id] || id);
const rows = [
  { id: 1, check_id: "sg_open_to_world", description: "TCP port 22 open to 0.0.0.0/0", resource_id: "sg-0aa", account_name: "U4RAD" },
  { id: 2, check_id: "sg_open_to_world", description: "All traffic, all ports open", resource_id: "sg-0bb", account_name: "AuroGov" },
  { id: 3, check_id: "ebs_unattached", description: "500 GiB gp3 volume", resource_id: "vol-1", account_name: "U4RAD", region: "ap-south-1" },
];

test("search matches label, description, resource and account, case-insensitively", () => {
  const f = q => filterFindings(rows, { q, label }).map(r => r.id);
  assert.deepEqual(f("tcp port 22"), [1]);
  assert.deepEqual(f("SG-0BB"), [2]);
  assert.deepEqual(f("aurogov"), [2]);
  assert.deepEqual(f("unattached"), [3]);          // from the label, not stored text
  assert.deepEqual(f("ap-south-1"), [3]);
  assert.deepEqual(f("nothing like this"), []);
  assert.deepEqual(f("  "), [1, 2, 3]);
});

test("type filter and search combine", () => {
  assert.deepEqual(filterFindings(rows, { checkId: "sg_open_to_world", q: "u4rad", label }).map(r => r.id), [1]);
});

test("pagination slices, reports the range, and clamps the page", () => {
  const many = Array.from({ length: 120 }, (_, i) => ({ id: i }));
  const p1 = paginate(many, 1);
  assert.equal(p1.rows.length, PAGE_SIZE); assert.equal(p1.from, 1); assert.equal(p1.to, 50); assert.equal(p1.pages, 3);
  const p3 = paginate(many, 3);
  assert.equal(p3.rows.length, 20); assert.equal(p3.from, 101); assert.equal(p3.to, 120);
  assert.equal(paginate(many, 99).page, 3);       // list shrank while on a late page
  assert.equal(paginate(many, 0).page, 1);
  const none = paginate([], 1);
  assert.deepEqual([none.from, none.to, none.pages, none.rows.length], [0, 0, 1, 0]);
  assert.equal(pageCount(50), 1); assert.equal(pageCount(51), 2);
});

test("check types are distinct and sorted by label", () => {
  assert.deepEqual(checkTypes(rows, label).map(t => t.id), ["sg_open_to_world", "ebs_unattached"]);   // by label: Security… < Unattached…
});
