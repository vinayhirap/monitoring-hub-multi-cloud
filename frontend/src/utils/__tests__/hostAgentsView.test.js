import test from "node:test";
import assert from "node:assert/strict";
import {
  stateOf, stateCounts, accountOptions, filterHosts, relativeTime, utcStamp, parseServiceList, parseInterval,
  buildConfigPatch, buildKeyRequest, buildInstallCommand, DOWNLOAD_PATH, firingAlertsForHost, mergeAlertLists,
  formatHostMetricValue, hostMetricTitle, hostMetricRows, hostMetricsNote,
  CHART_RANGES, DEFAULT_CHART_HOURS, CHART_REFRESH_MS, CHART_REFRESH_TIP, CHART_STAT_OPTIONS, hostUptimeText, chartGroups, chartColor, chartsNote,
  agentSupportsInventory, inventoryRows, inventoryEmptyText, inventoryScanText, mergeWatchList, checkManualService, INVENTORY_MIN_AGENT,
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

test("formatHostMetricValue formats by the unit the server sent and never invents a number", () => {
  assert.equal(formatHostMetricValue({ unit: "%", value: 41.46 }), "41.5%");
  assert.equal(formatHostMetricValue({ unit: "%", value: 0 }), "0%");
  assert.equal(formatHostMetricValue({ unit: "bytes", value: 512 }), "512 B");
  assert.equal(formatHostMetricValue({ unit: "bytes", value: 1536 }), "1.5 KiB");
  assert.equal(formatHostMetricValue({ unit: "bytes", value: 3 * 1024 ** 3 }), "3 GiB");
  assert.equal(formatHostMetricValue({ unit: "bytes/s", value: 2048 }), "2 KiB/s");
  assert.equal(formatHostMetricValue({ unit: "seconds", value: 59 }), "59s");
  assert.equal(formatHostMetricValue({ unit: "seconds", value: 3661 }), "1h 1m");
  assert.equal(formatHostMetricValue({ unit: "seconds", value: 90061 }), "1d 1h 1m");
  assert.equal(formatHostMetricValue({ unit: "", value: 0.256 }), "0.26");
  assert.equal(formatHostMetricValue({ unit: "", value: 312 }), "312");
  for (const bad of [null, undefined, {}, { unit: "%", value: null }, { unit: "%", value: "12" }, { unit: "%", value: NaN }, { unit: "seconds", value: -5 }]) {
    assert.equal(formatHostMetricValue(bad), "—");
  }
});

test("hostMetricTitle and hostMetricRows", () => {
  assert.equal(hostMetricTitle({ title: "Disk used", label: "/" }), "Disk used · /");
  assert.equal(hostMetricTitle({ title: "Memory used", label: null }), "Memory used");
  assert.equal(hostMetricTitle({ name: "x" }), "x");
  assert.deepEqual(hostMetricRows(null), []);
  assert.deepEqual(hostMetricRows({ metrics: "nope" }), []);
  const rows2 = hostMetricRows({ metrics: [
    { name: "host_cpu_percent", title: "CPU utilization (guest OS)", label: null, unit: "%", value: 12.25, timestamp: "2026-10-24T10:00:00Z", age_seconds: 5 },
    null,
    { title: "no name" },
  ] });
  assert.equal(rows2.length, 1);
  assert.deepEqual(rows2[0], { key: "host_cpu_percent", title: "CPU utilization (guest OS)", value: "12.3%", timestamp: "2026-10-24T10:00:00Z", ageSeconds: 5 });
});

test("hostMetricsNote only states what the response and host carry", () => {
  assert.equal(hostMetricsNote(null, rows[0]), null);
  assert.equal(hostMetricsNote({ metrics: [], shared_series: false, source: "metrics" }, rows[0]), null);
  assert.match(hostMetricsNote({ metrics: [], source: "metrics" }, rows[1]), /stale, so these are the last readings/);
  assert.match(hostMetricsNote({ metrics: [], shared_series: true, source: "metrics" }, rows[0]), /CloudWatch agent/);
  assert.match(hostMetricsNote({ metrics: [], source: "metric_history" }, rows[0]), /last 24 hours/);
});

// ── watched services (patch 0009) ─────────────────────────────────────────────────────────────────────────────────

import {
  SERVICE_STATE_LABEL, SERVICE_STATE_TONE, durationText, serviceRows, pendingServices, serviceSummaryText, serviceWorstTone,
  servicesNote, configFormDefaults, currentConfigOf,
} from "../hostAgentsView.js";

const svcPayload = {
  watch_services: ["nginx", "sshd", "cron", "mysql"], interval_seconds: 30, config_version: 3,
  services: [
    { name: "nginx", state: "running", since: "2026-10-24T08:00:00Z", observed_at: "2026-10-24T10:00:00Z", since_seconds: 7260, age_seconds: 20 },
    { name: "sshd", state: "failed", since: "2026-10-24T09:58:30Z", observed_at: "2026-10-24T10:00:00Z", since_seconds: 90, age_seconds: 20 },
    { name: "cron", state: "weird", since: null, observed_at: null, since_seconds: null, age_seconds: null },
  ],
  pending: ["mysql"], summary: { total: 4, running: 1, stopped: 0, failed: 1, unknown: 1, pending: 1 },
};

test("service states map to labels and tones, and an unknown value never shows as running", () => {
  assert.deepEqual(Object.keys(SERVICE_STATE_LABEL), ["running", "stopped", "failed", "unknown"]);
  assert.deepEqual(Object.keys(SERVICE_STATE_TONE), Object.keys(SERVICE_STATE_LABEL));
  const rows = serviceRows(svcPayload);
  assert.deepEqual(rows.map(r => [r.name, r.state, r.tone]), [["nginx", "running", "ok"], ["sshd", "failed", "crit"], ["cron", "unknown", "mute"]]);
  assert.equal(rows[0].sinceText, "2h 1m");
  assert.equal(rows[1].sinceText, "1m 30s");
  assert.equal(rows[2].sinceText, "—");
  assert.deepEqual(serviceRows(null), []);
  assert.deepEqual(serviceRows({ services: "x" }), []);
  assert.equal(durationText(-1), "—");
});

test("summary text lists the worst first and only non-zero parts", () => {
  assert.equal(serviceSummaryText(svcPayload), "1 failed · 1 unknown · 1 running · 1 waiting for first report");
  assert.equal(serviceSummaryText({ summary: { total: 0 } }), null);
  assert.equal(serviceSummaryText(null), null);
  assert.deepEqual(pendingServices(svcPayload), ["mysql"]);
});

test("worst tone", () => {
  assert.equal(serviceWorstTone(svcPayload), "crit");
  assert.equal(serviceWorstTone({ services: [{ name: "a", state: "running" }] }), "ok");
  assert.equal(serviceWorstTone({ services: [{ name: "a", state: "running" }, { name: "b", state: "stopped" }] }), "warn");
  assert.equal(serviceWorstTone({ services: [] }), null);
});

test("servicesNote says when the states are from a last report and what is still pending", () => {
  assert.equal(servicesNote(null, { state: "online" }), null);
  assert.equal(servicesNote({ services: [{ name: "a", state: "running" }], pending: [] }, { state: "online" }), null);
  assert.match(servicesNote(svcPayload, { state: "offline" }), /last report, not live/);
  assert.match(servicesNote(svcPayload, { state: "online" }), /Waiting for the first report of: mysql/);
  assert.doesNotMatch(servicesNote({ services: [], pending: ["x"] }, { state: "offline" }), /last report/);   // nothing to call "last report"
});

test("Configure is pre-filled from the services payload only when it carries both values", () => {
  assert.deepEqual(configFormDefaults(svcPayload), { intervalText: "30", servicesText: "nginx\nsshd\ncron\nmysql" });
  assert.equal(configFormDefaults({ watch_services: [] }), null);
  assert.equal(configFormDefaults(null), null);
  assert.deepEqual(currentConfigOf(svcPayload), { interval_seconds: 30, watch_services: ["nginx", "sshd", "cron", "mysql"] });
});

test("buildConfigPatch with a known current config sends only real changes", () => {
  const cur = currentConfigOf(svcPayload);
  const form = d => ({ intervalText: "30", servicesText: "nginx\nsshd\ncron\nmysql", clearServices: false, ...d });
  assert.ok(buildConfigPatch(form({}), cur).error);                                                        // untouched form: nothing to save
  assert.deepEqual(buildConfigPatch(form({ intervalText: "60" }), cur), { body: { interval_seconds: 60 } });
  assert.deepEqual(buildConfigPatch(form({ servicesText: "nginx\nsshd" }), cur), { body: { watch_services: ["nginx", "sshd"] } });
  assert.deepEqual(buildConfigPatch(form({ servicesText: "mysql nginx sshd cron" }), cur), { body: { watch_services: ["mysql", "nginx", "sshd", "cron"] } });   // order is the admin's
  assert.deepEqual(buildConfigPatch(form({ clearServices: true }), cur), { body: { watch_services: [] } });
  assert.match(buildConfigPatch(form({ servicesText: "" }), cur).error, /Stop watching all/);                // emptied by hand is not "unchanged"
  assert.ok(buildConfigPatch(form({ servicesText: "a/b" }), cur).error);
  assert.deepEqual(buildConfigPatch(form({ servicesText: "x", clearServices: true }), { interval_seconds: 30, watch_services: [] }).error, "Nothing changed.");
});

// ── charts ──────────────────────────────────────────────────────────────────────────────────────────────────────────
const S = (name, contract) => ({ name, contract, title: name, unit: "Percent", data: [] });

test("chart ranges are the EC2 presets, capped at the 30-day retention", () => {
  assert.deepEqual(CHART_RANGES.map(r => r.label), ["1H", "3H", "6H", "1D", "1W", "1M"]);
  assert.equal(CHART_RANGES.at(-1).hours, 720);
  assert.ok(CHART_RANGES.some(r => r.hours === DEFAULT_CHART_HOURS));
  assert.equal(CHART_REFRESH_MS, 60000);
});

test("the refresh tooltip never claims AWS or a 5 minute cadence (that text belongs to CloudWatch pages)", () => {
  assert.ok(!/AWS|5 min/i.test(CHART_REFRESH_TIP));
  assert.match(CHART_REFRESH_TIP, /agent reports/);
});

test("chartGroups orders groups and keeps per-mount / per-interface series together", () => {
  const series = [
    S("host_net_tx_bytes_per_s__ens5", "net_tx_bytes_per_s"), S("host_proc_count", "proc_count"),
    S("disk_used_percent__boot", "disk_used_percent"), S("host_load1", "load1"), S("host_cpu_percent", "cpu_percent"),
    S("disk_used_percent", "disk_used_percent"), S("mem_used_percent", "mem_used_percent"),
    S("host_net_rx_bytes_per_s__ens5", "net_rx_bytes_per_s"),
  ];
  const g = chartGroups(series);
  assert.deepEqual(g.map(x => x.key), ["compute", "memory", "disk", "network", "system"]);
  assert.deepEqual(g[0].items.map(s => s.name), ["host_cpu_percent", "host_load1"]);                      // contract order inside a group
  assert.deepEqual(g[2].items.map(s => s.name), ["disk_used_percent__boot", "disk_used_percent"]);        // server order kept per contract
  assert.deepEqual(g[3].items.map(s => s.name), ["host_net_rx_bytes_per_s__ens5", "host_net_tx_bytes_per_s__ens5"]);
});

test("chartGroups never drops an unknown metric and omits empty groups", () => {
  const g = chartGroups([S("host_future_thing", "future_thing"), null, { contract: "cpu_percent" }, S("host_cpu_percent", "cpu_percent")]);
  assert.deepEqual(g.map(x => x.key), ["compute", "other"]);
  assert.equal(g[1].items[0].name, "host_future_thing");
  assert.deepEqual(chartGroups(null), []);
  assert.deepEqual(chartGroups([]), []);
});

test("every contract metric has a colour and an unknown one falls back", () => {
  for (const c of ["cpu_percent", "load1", "mem_used_percent", "disk_used_percent", "net_rx_bytes_per_s", "uptime_seconds"]) {
    assert.match(chartColor(c), /^#[0-9a-f]{6}$/i);
  }
  assert.match(chartColor("nope"), /^#[0-9a-f]{6}$/i);
});

test("chartsNote states facts only", () => {
  const ok = { series: [S("a", "cpu_percent")], shared_series: false };
  assert.equal(chartsNote(ok, { state: "online" }), "");
  assert.equal(chartsNote(null, { state: "online" }), "");
  assert.match(chartsNote(ok, { state: "offline" }), /offline.*last data it delivered.*not extended/);
  assert.match(chartsNote(ok, { state: "stale" }), /not reporting on time/);
  assert.match(chartsNote(ok, { state: "revoked" }), /revoked/);
  assert.match(chartsNote({ ...ok, shared_series: true }, { state: "online" }), /CloudWatch agent/);
  assert.match(chartsNote({ series: [] }, { state: "online" }), /No metric history has been stored/);
});

test("the statistic list offers no Sum for gauge metrics", () => {
  assert.deepEqual(CHART_STAT_OPTIONS, ["auto", "Average", "Minimum", "Maximum", "SampleCount"]);
});

test("hostUptimeText reads only the stored uptime reading and labels it when the host is not online", () => {
  const payload = { metrics: [
    { name: "host_cpu_percent", metric: "cpu_percent", unit: "%", value: 20 },
    { name: "host_uptime_seconds", metric: "uptime_seconds", unit: "seconds", value: 3 * 86400 + 4 * 3600 + 5 * 60 },
  ] };
  assert.equal(hostUptimeText(payload, { state: "online" }), "3d 4h 5m");
  assert.equal(hostUptimeText(payload, { state: "offline" }), "3d 4h 5m at last report");
  assert.equal(hostUptimeText({ metrics: [{ name: "host_cpu_percent", metric: "cpu_percent", unit: "%", value: 1 }] }, { state: "online" }), null);
  assert.equal(hostUptimeText({ metrics: [{ metric: "uptime_seconds", unit: "seconds", value: null }] }, { state: "online" }), null);
  assert.equal(hostUptimeText(null, { state: "online" }), null);
});

// ── Add service picker ──────────────────────────────────────────────────────────────────────────────────────────────
const inv = {
  agent_version: "0.2.2", scan_age_seconds: 240,
  items: [
    { name: "nginx", state: "running", startup: "enabled", description: "Web server", major: true, watched: true },
    { name: "mysql", state: "stopped", startup: "disabled", description: "Database", major: true, watched: false },
    { name: "cron", state: "running", startup: "enabled", description: "Scheduler", major: true, watched: false },
    { name: "systemd-journald", state: "running", startup: "static", description: "Journal", major: false, watched: false },
    { name: "dbus", state: "weird", startup: "static", description: "", major: false, watched: false },
  ],
};

test("agentSupportsInventory compares versions numerically", () => {
  assert.equal(INVENTORY_MIN_AGENT, "0.2.2");
  assert.equal(agentSupportsInventory("0.2.2"), true);
  assert.equal(agentSupportsInventory("0.2.10"), true);
  assert.equal(agentSupportsInventory("0.3.0"), true);
  assert.equal(agentSupportsInventory("v1.0.0"), true);
  assert.equal(agentSupportsInventory("0.2.1"), false);
  assert.equal(agentSupportsInventory("0.1.9"), false);
  assert.equal(agentSupportsInventory(null), false);
  assert.equal(agentSupportsInventory("dev"), false);
});

test("inventoryRows hides OS-internal services by default, counts them, and keeps watched/selected ones visible", () => {
  let r = inventoryRows(inv);
  assert.deepEqual(r.rows.map(x => x.name), ["cron", "mysql", "nginx"]);      // unwatched first, alphabetical, watched last
  assert.equal(r.hiddenInternal, 2);
  r = inventoryRows(inv, { showAll: true });
  assert.deepEqual(r.rows.map(x => x.name), ["cron", "mysql", "dbus", "systemd-journald", "nginx"]);   // major, then internal, then watched
  assert.equal(r.hiddenInternal, 0);
  r = inventoryRows(inv, { selected: ["systemd-journald"] });
  assert.ok(r.rows.some(x => x.name === "systemd-journald"), "a selected internal service must stay visible");
  assert.equal(r.hiddenInternal, 1);
});

test("inventoryRows searches name and description and tolerates unknown states and bad payloads", () => {
  assert.deepEqual(inventoryRows(inv, { query: "DATA" }).rows.map(x => x.name), ["mysql"]);   // description match, case-insensitive
  assert.deepEqual(inventoryRows(inv, { query: "journ" }).rows, []);
  assert.equal(inventoryRows(inv, { query: "journ" }).hiddenInternal, 1);                     // "tick show all" hint source
  assert.equal(inventoryRows(inv, { showAll: true }).rows.find(x => x.name === "dbus").state, "unknown");
  assert.deepEqual(inventoryRows(null), { rows: [], hiddenInternal: 0 });
  assert.deepEqual(inventoryRows({ items: [null, { state: "running" }] }), { rows: [], hiddenInternal: 0 });
});

test("inventoryEmptyText distinguishes an old agent from a scan that has not arrived", () => {
  assert.equal(inventoryEmptyText(inv, {}), null);
  assert.match(inventoryEmptyText({ items: [], agent_version: "0.2.1" }, {}), /0\.2\.1.*Update the agent to 0\.2\.2/);
  assert.match(inventoryEmptyText({ items: [] }, { agent_version: "0.1.0" }), /Update the agent/);
  assert.match(inventoryEmptyText({ items: [], agent_version: "0.2.2" }, {}), /No service scan has arrived/);
  assert.match(inventoryEmptyText(null, {}), /No service scan has arrived/);
});

test("inventoryScanText", () => {
  assert.equal(inventoryScanText(inv), "Scanned 4m ago");
  assert.equal(inventoryScanText({ scan_age_seconds: 20 }), "Scanned just now");
  assert.equal(inventoryScanText({ scan_age_seconds: 7300 }), "Scanned 2h ago");
  assert.equal(inventoryScanText({ scan_age_seconds: 200000 }), "Scanned 2d ago");
  assert.equal(inventoryScanText({ scan_age_seconds: null }), "");
  assert.equal(inventoryScanText(null), "");
});

test("mergeWatchList appends new names, never duplicates, keeps order and flags the 100 limit", () => {
  assert.deepEqual(mergeWatchList(["nginx"], ["mysql", "nginx", "cron", "mysql"]),
    { services: ["nginx", "mysql", "cron"], added: ["mysql", "cron"], already: ["nginx"], tooMany: false });
  assert.deepEqual(mergeWatchList(null, ["a"]).services, ["a"]);
  const full = Array.from({ length: 100 }, (_, i) => `s${i}`);
  assert.equal(mergeWatchList(full, ["extra"]).tooMany, true);
  assert.equal(mergeWatchList(full, ["s1"]).tooMany, false);
});

test("checkManualService validates like the server and warns only when a scan exists and lacks the name", () => {
  assert.deepEqual(checkManualService("nginx", inv), { name: "nginx" });
  assert.deepEqual(checkManualService("NGINX", inv), { name: "NGINX" });              // Windows names are case-insensitive
  assert.deepEqual(checkManualService("nginxS", inv), { name: "nginxS", notFound: true });
  assert.deepEqual(checkManualService("nginxS", { items: [] }), { name: "nginxS" });  // no scan: no false warning
  assert.match(checkManualService("bad name;", inv).error, /Invalid|one name/);
  assert.match(checkManualService("-rf", inv).error, /Invalid service name/);
  assert.match(checkManualService("a b", inv).error, /one name/);
  assert.match(checkManualService("  ", inv).error, /Type a service name/);
});
