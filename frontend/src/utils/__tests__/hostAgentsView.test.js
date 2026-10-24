import test from "node:test";
import assert from "node:assert/strict";
import {
  stateOf, stateCounts, accountOptions, filterHosts, relativeTime, utcStamp, parseServiceList, parseInterval,
  buildConfigPatch, buildKeyRequest, buildInstallCommand, DOWNLOAD_PATH, firingAlertsForHost, mergeAlertLists,
} from "../hostAgentsView.js";

const rows = [
  { id: 1, aws_account_id: 10, account: "Prod", hostname: "web-1", instance_id: "i-0abc", os: "linux", arch: "amd64", agent_version: "0.1.0", state: "online" },
  { id: 2, aws_account_id: 10, account: "Prod", hostname: "db-1", instance_id: null, os: "linux", arch: "arm64", agent_version: "0.1.0", state: "stale" },
  { id: 3, aws_account_id: 20, account: "Dev", hostname: "build-1", instance_id: "i-0def", os: "linux", arch: "amd64", agent_version: "0.1.1", state: "revoked" },
];

test("state helpers", () => {
  assert.equal(stateOf({ state: "ONLINE" }), "online");
  assert.equal(stateOf({ state: "weird" }), "offline");
  assert.deepEqual(stateCounts(rows), { all: 3, online: 1, stale: 1, offline: 0, revoked: 1 });
});

test("accountOptions and filterHosts", () => {
  assert.deepEqual(accountOptions(rows), [{ id: 20, name: "Dev" }, { id: 10, name: "Prod" }]);
  assert.equal(filterHosts(rows, { accountId: "10" }).length, 2);
  assert.equal(filterHosts(rows, { state: "stale" })[0].hostname, "db-1");
  assert.equal(filterHosts(rows, { query: "I-0DEF" })[0].hostname, "build-1");
  assert.equal(filterHosts(rows, { query: "arm64" }).length, 1);
  assert.equal(filterHosts(rows, { accountId: "10", state: "revoked" }).length, 0);
  assert.equal(filterHosts(null).length, 0);
});

test("relativeTime and utcStamp", () => {
  const now = Date.parse("2026-10-23T12:00:00Z");
  assert.equal(relativeTime("2026-10-23T11:59:55Z", now), "just now");
  assert.equal(relativeTime("2026-10-23T11:59:15Z", now), "45s ago");
  assert.equal(relativeTime("2026-10-23T11:55:00Z", now), "5m ago");
  assert.equal(relativeTime("2026-10-23T09:00:00Z", now), "3h ago");
  assert.equal(relativeTime("2026-10-20T12:00:00Z", now), "3d ago");
  assert.equal(relativeTime(null, now), "Never");
  assert.equal(utcStamp("2026-10-03T21:38:48Z"), "03 Oct 2026, 21:38:48 UTC");
  assert.equal(utcStamp(null), "—");
});

test("parseServiceList mirrors the server rules", () => {
  assert.deepEqual(parseServiceList("nginx, sshd\nnginx  cron.service").services, ["nginx", "sshd", "cron.service"]);
  assert.deepEqual(parseServiceList("ok bad/name -x").invalid, ["bad/name", "-x"]);
  assert.equal(parseServiceList(Array.from({ length: 101 }, (_, i) => `s${i}`).join(",")).tooMany, true);
  assert.deepEqual(parseServiceList("").services, []);
});

test("parseInterval", () => {
  assert.equal(parseInterval("15"), 15);
  assert.equal(parseInterval("300"), 300);
  assert.equal(parseInterval("14"), null);
  assert.equal(parseInterval("301"), null);
  assert.equal(parseInterval("1.5"), null);
  assert.equal(parseInterval(""), null);
});

test("buildConfigPatch only sends what was chosen", () => {
  assert.deepEqual(buildConfigPatch({ intervalText: "30", servicesText: "", clearServices: false }), { body: { interval_seconds: 30 } });
  assert.deepEqual(buildConfigPatch({ intervalText: "", servicesText: "nginx sshd", clearServices: false }), { body: { watch_services: ["nginx", "sshd"] } });
  assert.deepEqual(buildConfigPatch({ intervalText: "", servicesText: "nginx", clearServices: true }), { body: { watch_services: [] } });
  assert.ok(buildConfigPatch({ intervalText: "", servicesText: "", clearServices: false }).error);
  assert.ok(buildConfigPatch({ intervalText: "5", servicesText: "", clearServices: false }).error);
  assert.ok(buildConfigPatch({ intervalText: "", servicesText: "a/b", clearServices: false }).error);
});

test("buildKeyRequest validates like the server", () => {
  assert.deepEqual(buildKeyRequest({ accountId: "10", label: " web fleet ", maxUses: "5", expiresHours: "24" }),
    { body: { aws_account_id: 10, max_uses: 5, expires_in_hours: 24, label: "web fleet" } });
  assert.deepEqual(buildKeyRequest({ accountId: "10", label: "", maxUses: 1, expiresHours: 1 }),
    { body: { aws_account_id: 10, max_uses: 1, expires_in_hours: 1 } });
  assert.ok(buildKeyRequest({ accountId: "", maxUses: 1, expiresHours: 1 }).error);
  assert.ok(buildKeyRequest({ accountId: "1", maxUses: 0, expiresHours: 1 }).error);
  assert.ok(buildKeyRequest({ accountId: "1", maxUses: 1001, expiresHours: 1 }).error);
  assert.ok(buildKeyRequest({ accountId: "1", maxUses: 1, expiresHours: 8761 }).error);
  assert.ok(buildKeyRequest({ accountId: "1", label: "x".repeat(101), maxUses: 1, expiresHours: 1 }).error);
});

test("buildInstallCommand: matches agent/install.sh (env var name, --base-url, --url, no pipe to sh)", () => {
  const r = buildInstallCommand({ hubUrl: "https://hub.example/", key: "cak_a'b" });
  assert.equal(r.ready, true);
  assert.ok(r.command.includes(`BASE='https://hub.example${DOWNLOAD_PATH}'`));
  assert.ok(r.command.includes("/manifest.json"));
  assert.ok(r.command.includes(`CLOUDOPS_AGENT_ENROLL_KEY='cak_a'\\''b'`), "key is shell-quoted and uses the installer's variable");
  assert.ok(r.command.includes('bash install.sh --base-url "$BASE/$V" --url \'https://hub.example\''));
  assert.ok(!r.command.includes("| sudo") && !/\|\s*(sudo\s+)?(ba)?sh\b/.test(r.command), "installer is downloaded, never piped to a shell");
  assert.ok(!r.command.includes("CLOUDOPS_HUB_URL") && !r.command.includes("CLOUDOPS_ENROLLMENT_KEY"));
  assert.ok(!r.command.includes("--insecure-dev"));
});

test("buildInstallCommand: plain-http hub (DEV) adds --insecure-dev", () => {
  assert.ok(buildInstallCommand({ hubUrl: "http://10.0.0.1:8000", key: "cak_x" }).command.endsWith("--insecure-dev"));
});

test("firingAlertsForHost matches exact resource only and only firing", () => {
  const alerts = [
    { id: 1, resource: "web-1", resource_name: "web-1", state: "firing", severity: "WARNING", triggered_at: "2026-10-23T10:00:00Z" },
    { id: 2, resource: "i-0abc", resource_name: "web-1", state: "firing", severity: "CRITICAL", triggered_at: "2026-10-23T09:00:00Z" },
    { id: 3, resource: "web-10", resource_name: "web-10", state: "firing", severity: "CRITICAL" },
    { id: 4, resource: "web-1", resource_name: "web-1", state: "stale", severity: "CRITICAL" },
  ];
  assert.deepEqual(firingAlertsForHost(alerts, rows[0]).map(a => a.id), [2, 1]);
  assert.deepEqual(firingAlertsForHost(alerts, {}).map(a => a.id), []);
  assert.deepEqual(mergeAlertLists([[{ id: 1 }, { id: 2 }], [{ id: 2 }, { id: 3 }]]).map(a => a.id), [1, 2, 3]);
});
