import test from "node:test";
import assert from "node:assert/strict";
import {
  parseTypedEmails, emptyListForm, listToForm, listFormProblem, listFormToPayload, toggleId, emptyScheduleForm,
  scheduleToForm, formScopeId, scheduleFormProblem, scheduleFormToPayload, scopeLabel, statusTone, deliveryTone,
  nextBeforeId, smtpProblems, splitScope,
  RULE_EVENTS, emptyRuleForm, ruleToForm, toggleRuleEvent, ruleFormProblem, ruleFormToPayload, ruleAccountLabel, ruleEventLabel,
  canOpenEmailCenter, emailCenterTabs, EMAIL_CENTER_TABS,
} from "../emailCenterView.js";

test("parseTypedEmails de-duplicates case-insensitively and reports the bad ones", () => {
  const r = parseTypedEmails("A@b.co, a@B.co; c@d.co\nnot-an-email");
  assert.deepEqual(r.emails, ["A@b.co", "c@d.co"]);
  assert.deepEqual(r.bad, ["not-an-email"]);
  assert.deepEqual(parseTypedEmails("").emails, []);
});

test("list form: needs a name and at least one member, round-trips a stored list", () => {
  assert.match(listFormProblem(emptyListForm()), /name/);
  assert.match(listFormProblem({ ...emptyListForm(), name: "Ops" }), /at least one/);
  assert.match(listFormProblem({ ...emptyListForm(), name: "Ops", emails: "bad" }), /valid email/);
  assert.equal(listFormProblem({ ...emptyListForm(), name: "Ops", user_ids: [1] }), "");
  const stored = { name: "Ops", description: "d", user_ids: [1, 2], group_ids: [3], emails: ["x@y.co"] };
  const f = listToForm(stored);
  assert.equal(f.emails, "x@y.co");
  assert.deepEqual(listFormToPayload(f), { name: "Ops", description: "d", user_ids: [1, 2], group_ids: [3], emails: ["x@y.co"] });
  assert.deepEqual(toggleId([1, 2], 2), [1]);
  assert.deepEqual(toggleId([1], 5), [1, 5]);
});

test("scope ids use the same shapes as the Reports page", () => {
  const f = { ...emptyScheduleForm(), account_id: "7" };
  assert.equal(formScopeId({ ...f, scope_type: "ACCOUNT" }), "7");
  assert.equal(formScopeId({ ...f, scope_type: "SLO", item_id: "" }), "7");
  assert.equal(formScopeId({ ...f, scope_type: "SLO", item_id: "12" }), "7:12");
  assert.equal(formScopeId({ ...f, scope_type: "HOST", item_id: "3" }), "7:3");
  assert.equal(formScopeId({ ...f, scope_type: "ENVIRONMENT" }), "7");
  assert.equal(formScopeId({ ...f, scope_type: "COST", cost_provider: "aws" }), "aws");
  assert.equal(formScopeId({ ...f, scope_type: "CLIENT", client_name: " Acme " }), "Acme");
  assert.equal(formScopeId({ ...f, scope_type: "RESOURCE", resource_id: " i-0abc " }), "i-0abc");
  assert.deepEqual(splitScope("SYNTHETIC", "7:4"), { account: "7", item: "4" });
});

test("schedule form: validation messages", () => {
  const base = { ...emptyScheduleForm(), name: "Weekly prod", account_id: "7", recipient_list_id: "3" };
  assert.equal(scheduleFormProblem(base), "");
  assert.match(scheduleFormProblem({ ...base, name: " " }), /name/);
  assert.match(scheduleFormProblem({ ...base, account_id: "" }), /account/i);
  assert.equal(scheduleFormProblem({ ...base, scope_type: "COST", account_id: "" }), "");
  assert.match(scheduleFormProblem({ ...base, report_type: "MONTHLY", run_day: 31 }), /1 to 28/);
  assert.equal(scheduleFormProblem({ ...base, report_type: "WEEKLY", run_day: 31 }), "");
  assert.match(scheduleFormProblem({ ...base, recipient_list_id: "", extra_emails: "" }), /recipient list/);
  assert.equal(scheduleFormProblem({ ...base, recipient_list_id: "", extra_emails: "me@x.co" }), "");
  assert.match(scheduleFormProblem({ ...base, scope_type: "RESOURCE", resource_id: "" }), /resource id/);
  assert.match(scheduleFormProblem({ ...base, extra_emails: "nope" }), /valid email/);
});

test("schedule payload: only the fields that apply to the report type are sent", () => {
  const base = { ...emptyScheduleForm("Asia/Kolkata"), name: " W ", account_id: "7", recipient_list_id: "3", extra_emails: "a@b.co" };
  const weekly = scheduleFormToPayload({ ...base, report_type: "WEEKLY", run_weekday: "4" });
  assert.equal(weekly.run_weekday, 4); assert.ok(!("run_day" in weekly)); assert.ok(!("run_month" in weekly));
  assert.equal(weekly.name, "W"); assert.equal(weekly.account_id, 7); assert.equal(weekly.recipient_list_id, 3);
  assert.deepEqual(weekly.extra_emails, ["a@b.co"]);
  const monthly = scheduleFormToPayload({ ...base, report_type: "MONTHLY", run_day: "2" });
  assert.equal(monthly.run_day, 2); assert.ok(!("run_weekday" in monthly)); assert.ok(!("run_month" in monthly));
  const yearly = scheduleFormToPayload({ ...base, report_type: "YEARLY", run_day: "1", run_month: "4" });
  assert.equal(yearly.run_month, 4);
  const cost = scheduleFormToPayload({ ...base, scope_type: "COST", account_id: "7", cost_provider: "gcp" });
  assert.equal(cost.account_id, null); assert.equal(cost.scope_id, "gcp");
  const noList = scheduleFormToPayload({ ...base, recipient_list_id: "" });
  assert.equal(noList.recipient_list_id, null);
});

test("a stored schedule round-trips through the form", () => {
  const row = { name: "Prod SLOs", enabled: 1, report_type: "YEARLY", scope_type: "SLO", scope_id: "7:12", account_id: 7,
    run_weekday: null, run_month: 4, run_day: 2, run_hour: 9, run_minute: 30, timezone: "UTC", recipient_list_id: 3,
    extra_emails: ["a@b.co"] };
  const f = scheduleToForm(row);
  assert.equal(f.item_id, "12"); assert.equal(f.account_id, "7"); assert.equal(f.enabled, true); assert.equal(f.extra_emails, "a@b.co");
  const p = scheduleFormToPayload(f);
  assert.equal(p.scope_id, "7:12"); assert.equal(p.run_month, 4); assert.equal(p.timezone, "UTC");
  const cost = scheduleToForm({ ...row, scope_type: "COST", scope_id: "azure", account_id: null, report_type: "MONTHLY" });
  assert.equal(cost.cost_provider, "azure"); assert.equal(cost.account_id, "");
});

test("scopeLabel names the account and never prints 'Slo'", () => {
  const accounts = [{ id: 7, name: "U4RAD" }];
  assert.equal(scopeLabel({ scope_type: "ACCOUNT", scope_id: "7", account_id: 7 }, accounts), "Account U4RAD");
  assert.equal(scopeLabel({ scope_type: "SLO", scope_id: "7:12", account_id: 7 }, accounts), "SLOs: #12 in U4RAD");
  assert.equal(scopeLabel({ scope_type: "SLO", scope_id: "7", account_id: 7 }, accounts), "SLOs: all in U4RAD");
  assert.equal(scopeLabel({ scope_type: "COST", scope_id: "all", account_id: null }, accounts), "Cloud cost: all clouds");
  assert.equal(scopeLabel({ scope_type: "COST", scope_id: "aws", account_id: null }, accounts), "Cloud cost: AWS");
  assert.equal(scopeLabel({ scope_type: "ACCOUNT", scope_id: "9", account_id: 9 }, accounts), "Account account 9");
});

test("tones, paging and the SMTP problem list", () => {
  assert.equal(statusTone("SENT"), "ok"); assert.equal(statusTone("FAILED"), "crit");
  assert.equal(statusTone("PARTIAL"), "warn"); assert.equal(statusTone(null), "mute");
  assert.equal(deliveryTone("sent"), "ok"); assert.equal(deliveryTone("skipped"), "warn");
  assert.equal(nextBeforeId([{ id: 9 }, { id: 8 }], 2), 8);
  assert.equal(nextBeforeId([{ id: 9 }], 2), null);
  assert.equal(nextBeforeId([], 2), null);
  const s = smtpProblems({ public_url_warning: "http!", problems: ["http!", "port bad"] });
  assert.deepEqual(s, { problems: ["port bad"], urlWarning: "http!" });
  assert.deepEqual(smtpProblems(null), { problems: [], urlWarning: "" });
});

test("rule form: needs a name, an event and a recipient; payload matches the API", () => {
  assert.deepEqual(RULE_EVENTS.map(e => e.key), ["alert_opened", "alert_escalated", "alert_resolved", "incident_opened", "incident_resolved"]);
  const f = { ...emptyRuleForm(), name: "Ops", recipient_list_id: "3" };
  assert.equal(ruleFormProblem(f), "");
  assert.match(ruleFormProblem({ ...f, name: "  " }), /name/);
  assert.match(ruleFormProblem({ ...f, events: [] }), /event/);
  assert.match(ruleFormProblem({ ...f, recipient_list_id: "" }), /recipient list or type/);
  assert.match(ruleFormProblem({ ...f, extra_emails: "nope" }), /valid email/);
  assert.equal(ruleFormProblem({ ...f, recipient_list_id: "", extra_emails: "a@b.co" }), "");
  assert.deepEqual(ruleFormToPayload({ ...f, name: " Ops ", account_id: "7", extra_emails: "A@b.co, a@B.co" }), {
    name: "Ops", enabled: true, events: ["alert_opened", "alert_resolved"], min_severity: "WARNING",
    account_id: 7, recipient_list_id: 3, extra_emails: ["A@b.co"],
  });
  assert.equal(ruleFormToPayload(emptyRuleForm()).account_id, null);
});

test("rule form: events keep canonical order and a stored rule round-trips", () => {
  assert.deepEqual(toggleRuleEvent(["alert_resolved"], "alert_opened"), ["alert_opened", "alert_resolved"]);
  assert.deepEqual(toggleRuleEvent(["alert_opened", "alert_resolved"], "alert_opened"), ["alert_resolved"]);
  const stored = { name: "R", enabled: 0, events: ["incident_opened", "alert_opened"], min_severity: "CRITICAL", account_id: 4, recipient_list_id: null, extra_emails: ["x@y.co"] };
  const f = ruleToForm(stored);
  assert.deepEqual(f.events, ["alert_opened", "incident_opened"]);
  assert.equal(f.enabled, false);
  assert.equal(f.account_id, "4");
  assert.equal(f.recipient_list_id, "");
  assert.equal(f.extra_emails, "x@y.co");
  assert.equal(ruleToForm(null).enabled, true);
});

test("rule labels: account name, all accounts, unknown account, event label", () => {
  const accounts = [{ id: 4, name: "AuroGov" }];
  assert.equal(ruleAccountLabel({ account_id: null }, accounts), "All accounts");
  assert.equal(ruleAccountLabel({ account_id: 4 }, accounts), "AuroGov");
  assert.equal(ruleAccountLabel({ account_id: 9 }, accounts), "Account 9");
  assert.equal(ruleEventLabel("alert_escalated"), "Alert escalated");
  assert.equal(ruleEventLabel("weird"), "weird");
});

test("the Reports page links to Email & Reports exactly when the sidebar entry would show", () => {
  const perms = new Set(["emailcenter.view"]), feats = new Set(["email_center"]);
  const has = c => perms.has(c), feat = f => feats.has(f);
  assert.equal(canOpenEmailCenter("admin", has, feat), true);
  assert.equal(canOpenEmailCenter("Editor", has, feat), true);
  assert.equal(canOpenEmailCenter("viewer", has, feat), false);           // roles: admin, editor only
  assert.equal(canOpenEmailCenter(undefined, has, feat), false);
  assert.equal(canOpenEmailCenter("admin", () => false, feat), false);    // no emailcenter.view
  assert.equal(canOpenEmailCenter("admin", has, () => false), false);     // EMAIL_CENTER_ENABLED off on this box
});

const tabKeys = perms => emailCenterTabs(c => perms.includes(c)).map(t => t.key);

test("emailCenterTabs: every tab needs its own permission, and a read-only viewer never gets recipients or the log", () => {
  assert.deepEqual(tabKeys(["emailcenter.view"]), ["status", "schedules", "rules"]);
  assert.deepEqual(tabKeys(["emailcenter.view", "emailcenter.manage"]), ["status", "schedules", "rules"]);   // manage alone adds no tab
  assert.deepEqual(tabKeys(["emailcenter.view", "emailcenter.recipients"]), ["status", "recipients", "schedules", "rules"]);
  assert.deepEqual(tabKeys(["emailcenter.view", "emailcenter.log"]), ["status", "schedules", "rules", "log"]);
  assert.deepEqual(tabKeys(["emailcenter.view", "emailcenter.manage", "emailcenter.recipients", "emailcenter.log", "emailcenter.test"]),
    ["status", "recipients", "schedules", "rules", "log"]);
  assert.deepEqual(tabKeys([]), []);
});

test("the status tab is first and the old smtp key is gone, so /email-center/smtp bookmarks fall back to it", () => {
  assert.equal(EMAIL_CENTER_TABS[0].key, "status");
  assert.ok(!EMAIL_CENTER_TABS.some(t => t.key === "smtp"));
});
