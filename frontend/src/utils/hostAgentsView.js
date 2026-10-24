// src/utils/hostAgentsView.js
// Pure helpers for the Host Agents page: no fetching, no React, nothing invented. Everything is derived from
// the rows GET /api/host-agents and GET /api/alerts already return.
import { alertsForResource, tsMs } from "./evidence.js";

export const STATES = ["online", "stale", "offline", "revoked"];
export const STATE_LABEL = { online: "Online", stale: "Stale", offline: "Offline", revoked: "Revoked" };
export const STATE_TONE = { online: "ok", stale: "warn", offline: "crit", revoked: "mute" };

export const KEY_STATUS_TONE = { active: "ok", exhausted: "warn", expired: "mute", revoked: "mute" };

export const INTERVAL_MIN = 15;     // server: HostConfigPatch interval_seconds ge/le
export const INTERVAL_MAX = 300;
export const MAX_WATCH_SERVICES = 100;
const SERVICE_RE = /^[A-Za-z0-9][A-Za-z0-9_.@:-]*$/;   // server: _SERVICE_RE

/** Where the hub serves agent releases (deploy/nginx.conf `location ^~ /dl/host-agent/`). */
export const DOWNLOAD_PATH = "/dl/host-agent";

export function stateOf(row) {
  const s = String(row?.state || "").toLowerCase();
  return STATES.includes(s) ? s : "offline";
}

export function stateCounts(rows) {
  const out = { all: 0, online: 0, stale: 0, offline: 0, revoked: 0 };
  for (const r of rows || []) { out.all += 1; out[stateOf(r)] += 1; }
  return out;
}

/** Distinct accounts present in the host list: [{id, name}] sorted by name. */
export function accountOptions(rows) {
  const m = new Map();
  for (const r of rows || []) {
    if (r?.aws_account_id != null && !m.has(r.aws_account_id)) m.set(r.aws_account_id, r.account || `Account ${r.aws_account_id}`);
  }
  return [...m].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name));
}

export function filterHosts(rows, { accountId = "all", state = "all", query = "" } = {}) {
  const q = String(query || "").trim().toLowerCase();
  return (rows || []).filter(r => {
    if (accountId !== "all" && String(r.aws_account_id) !== String(accountId)) return false;
    if (state !== "all" && stateOf(r) !== state) return false;
    if (!q) return true;
    return [r.hostname, r.instance_id, r.account, r.os, r.arch, r.agent_version]
      .some(v => v != null && String(v).toLowerCase().includes(q));
  });
}

/** "just now" / "45s ago" / "5m ago" / "3h ago" / "2d ago"; "Never" when the host has not reported. */
export function relativeTime(iso, nowMs = Date.now()) {
  const t = tsMs(iso);
  if (t == null) return "Never";
  const s = Math.max(0, Math.round((nowMs - t) / 1000));
  if (s < 10) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/** Absolute UTC stamp, e.g. "03 Oct 2026, 21:38:48 UTC". Times from this API are UTC by contract. */
export function utcStamp(iso) {
  const t = tsMs(iso);
  if (t == null) return "—";
  const p = new Intl.DateTimeFormat("en-GB", {
    day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
    hourCycle: "h23", timeZone: "UTC",
  }).formatToParts(new Date(t));
  const g = k => p.find(x => x.type === k)?.value ?? "";
  return `${g("day")} ${g("month")} ${g("year")}, ${g("hour")}:${g("minute")}:${g("second")} UTC`;
}

/**
 * Watched services from free text (commas / whitespace / newlines).
 * Returns { services, invalid, tooMany }. Mirrors the server's rules so the user sees the problem before sending.
 */
export function parseServiceList(text) {
  const seen = new Set();
  const services = [];
  const invalid = [];
  for (const raw of String(text || "").split(/[\s,]+/)) {
    const name = raw.trim();
    if (!name) continue;
    if (name.length > 100 || !SERVICE_RE.test(name)) { if (!invalid.includes(name)) invalid.push(name); continue; }
    if (!seen.has(name)) { seen.add(name); services.push(name); }
  }
  return { services, invalid, tooMany: services.length > MAX_WATCH_SERVICES };
}

/** Whole number within the server's 15..300 range, else null. Blank means "not changing" and also returns null. */
export function parseInterval(value) {
  const s = String(value ?? "").trim();
  if (!/^\d+$/.test(s)) return null;
  const n = Number(s);
  return n >= INTERVAL_MIN && n <= INTERVAL_MAX ? n : null;
}

/**
 * PATCH body from the edit form. Only fields the person actually chose to change are sent.
 * `current` ({ interval_seconds, watch_services }, from GET /api/host-agents/{id}/services) is optional: when given, the
 * form is pre-filled with it, so a field is sent only when it DIFFERS from the current value (an untouched pre-filled
 * form must not bump the config version or restart the agent's reporting for nothing). Without it, anything filled in is sent.
 * Returns { body } or { error }.
 */
export function buildConfigPatch({ intervalText, servicesText, clearServices }, current = null) {
  const body = {};
  if (String(intervalText ?? "").trim() !== "") {
    const n = parseInterval(intervalText);
    if (n == null) return { error: `Interval must be a whole number from ${INTERVAL_MIN} to ${INTERVAL_MAX} seconds.` };
    if (!current || n !== current.interval_seconds) body.interval_seconds = n;
  }
  const have = Array.isArray(current?.watch_services) ? current.watch_services : null;
  const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
  if (clearServices) {
    if (!have || have.length > 0) body.watch_services = [];
  } else if (String(servicesText ?? "").trim() !== "") {
    const { services, invalid, tooMany } = parseServiceList(servicesText);
    if (invalid.length) return { error: `Invalid service name: ${invalid[0].slice(0, 40)}` };
    if (tooMany) return { error: `At most ${MAX_WATCH_SERVICES} services can be watched.` };
    if (!have || !same(services, have)) body.watch_services = services;
  } else if (have && have.length > 0) {
    // A pre-filled list that was emptied by hand: say so instead of silently keeping it ("blank = unchanged" is only
    // true when the current list is not known).
    return { error: "The service list is empty. Tick “Stop watching all services” to clear it, or restore the names." };
  }
  if (!Object.keys(body).length) return { error: current ? "Nothing changed." : "Nothing to change: set an interval and/or a list of services." };
  return { body };
}

/** Enrollment-key form -> POST body, validated like the server (max_uses 1..1000, expires 1..8760 h). */
export function buildKeyRequest({ accountId, label, maxUses, expiresHours }) {
  const aws_account_id = Number(accountId);
  if (!Number.isInteger(aws_account_id) || aws_account_id < 1) return { error: "Choose an account." };
  const uses = Number(maxUses), hours = Number(expiresHours);
  if (!Number.isInteger(uses) || uses < 1 || uses > 1000) return { error: "Max uses must be a whole number from 1 to 1000." };
  if (!Number.isInteger(hours) || hours < 1 || hours > 8760) return { error: "Expiry must be a whole number of hours from 1 to 8760." };
  const body = { aws_account_id, max_uses: uses, expires_in_hours: hours };
  const l = String(label || "").trim();
  if (l) {
    if (l.length > 100) return { error: "Label must be 100 characters or fewer." };
    body.label = l;
  }
  return { body };
}

/**
 * Linux install command, matching agent/install.sh exactly:
 *   - the enrollment key goes in CLOUDOPS_AGENT_ENROLL_KEY (kept out of `ps`), the hub in --url;
 *   - install.sh needs --base-url pointing at the version folder that holds the binary and SHA256SUMS;
 *   - the version folder is read from the hub's manifest.json ("latest"), so the command never goes stale;
 *   - the script is downloaded first and run second (not piped to a shell), so it can be read before it runs.
 * A plain-http hub (DEV only) adds --insecure-dev, which the installer and agent otherwise refuse.
 */
export function buildInstallCommand({ hubUrl, key }) {
  const q = s => `'${String(s).replace(/'/g, `'\\''`)}'`;
  const hub = String(hubUrl || "").replace(/\/+$/, "");
  const insecure = /^http:\/\//i.test(hub) ? " --insecure-dev" : "";
  const lines = [
    `BASE=${q(hub + DOWNLOAD_PATH)}`,
    `V=$(curl -fsSL "$BASE/manifest.json" | sed -n 's/.*"latest": *"\\([^"]*\\)".*/\\1/p') && \\`,
    `curl -fsSLO "$BASE/$V/install.sh" && \\`,
    `sudo CLOUDOPS_AGENT_ENROLL_KEY=${q(key)} bash install.sh --base-url "$BASE/$V" --url ${q(hub)}${insecure}`,
  ];
  return { ready: true, command: lines.join("\n") };
}

/** Firing alerts that name this host (hostname or instance id), most severe first. Exact match only, never by account. */
export function firingAlertsForHost(alertRows, host) {
  const ids = [host?.hostname, host?.instance_id].filter(Boolean);
  return alertsForResource(alertRows, ids).filter(a => a.state === "firing");
}

/** Merge several alert lists (one per search term) by alert id. */
export function mergeAlertLists(lists) {
  const m = new Map();
  for (const rows of lists) for (const a of rows || []) if (!m.has(a.id)) m.set(a.id, a);
  return [...m.values()];
}

// ── Current metrics (GET /api/host-agents/{id}/metrics) ───────────────────────────────────────────────────────────
// Display formatting only: the server returns the stored reading and its unit; nothing is converted, averaged or estimated.

const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];

function trimmed(n, maxFraction) {
  return Number(n).toLocaleString("en-US", { maximumFractionDigits: maxFraction });
}

function formatBytes(n) {
  let v = Number(n);
  let i = 0;
  while (Math.abs(v) >= 1024 && i < BYTE_UNITS.length - 1) { v /= 1024; i += 1; }
  return `${trimmed(v, i === 0 ? 0 : 1)} ${BYTE_UNITS[i]}`;
}

function formatDuration(seconds) {
  let s = Math.floor(Number(seconds));
  if (!Number.isFinite(s) || s < 0) return "—";
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return m > 0 ? `${m}m ${s - m * 60}s` : `${s}s`;
}

/** One stored reading -> the text shown in the table. A non-finite value shows an em dash, never a made-up number. */
export function formatHostMetricValue(m) {
  const v = m?.value;
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  switch (m.unit) {
    case "%": return `${trimmed(v, 1)}%`;
    case "bytes": return formatBytes(v);
    case "bytes/s": return `${formatBytes(v)}/s`;
    case "seconds": return formatDuration(v);
    default: return trimmed(v, 2);
  }
}

/** "Disk used" + label -> "Disk used · /" ; metrics without a label keep their plain title. */
export function hostMetricTitle(m) {
  const base = m?.title || m?.name || "Metric";
  return m?.label ? `${base} · ${m.label}` : base;
}

/** Rows for the table, in the order the server sent them, with display strings. Anything that is not an array -> []. */
export function hostMetricRows(payload) {
  const list = Array.isArray(payload?.metrics) ? payload.metrics : [];
  return list.filter(m => m && typeof m.name === "string").map(m => ({
    key: m.name,
    title: hostMetricTitle(m),
    value: formatHostMetricValue(m),
    timestamp: m.timestamp || null,
    ageSeconds: Number.isFinite(m.age_seconds) ? m.age_seconds : null,
  }));
}

/** Uptime of the host for the details list: "39d 8h 47m", suffixed when the host is not online (then it is the value at the
 *  last report, not a live one). null when the payload carries no uptime reading. Only reads what the server stored. */
export function hostUptimeText(payload, host) {
  const list = Array.isArray(payload?.metrics) ? payload.metrics : [];
  const m = list.find(x => x && x.metric === "uptime_seconds" && Number.isFinite(x.value));
  if (!m) return null;
  const text = formatHostMetricValue(m);
  const st = stateOf(host || {});
  return st === "online" ? text : `${text} at last report`;
}

/** Short caption under the table, or null. Only states facts the response carries. */
export function hostMetricsNote(payload, host) {
  if (!payload) return null;
  const parts = [];
  const state = stateOf(host);
  if (state !== "online") parts.push(`This host is ${STATE_LABEL[state].toLowerCase()}, so these are the last readings received, not live values.`);
  if (payload.shared_series) parts.push("This host is attached to an EC2 resource: memory and disk readings use the same stored names as the CloudWatch agent, so the stored value can come from either source.");
  if (payload.source === "metric_history") parts.push("Showing the newest history point from the last 24 hours.");
  return parts.length ? parts.join(" ") : null;
}

// ── Watched services (GET /api/host-agents/{id}/services) ─────────────────────────────────────────────────────────
// The server returns the stored state of every name in the host's watch list; nothing here is polled, guessed or averaged.

export const SERVICE_STATE_LABEL = { running: "Running", stopped: "Stopped", failed: "Failed", unknown: "Unknown" };
export const SERVICE_STATE_TONE = { running: "ok", stopped: "warn", failed: "crit", unknown: "mute" };

/** 7260 -> "2h 1m", 90 -> "1m 30s", 5 -> "5s"; non-finite or negative -> "—". */
export function durationText(seconds) {
  return formatDuration(seconds);
}

/** Table rows in the order the administrator listed the services. A state the UI does not know is shown as unknown. */
export function serviceRows(payload) {
  const list = Array.isArray(payload?.services) ? payload.services : [];
  return list.filter(x => x && typeof x.name === "string").map(x => {
    const state = Object.prototype.hasOwnProperty.call(SERVICE_STATE_LABEL, x.state) ? x.state : "unknown";
    return {
      key: x.name, name: x.name, state, label: SERVICE_STATE_LABEL[state], tone: SERVICE_STATE_TONE[state],
      since: x.since || null,
      sinceText: Number.isFinite(x.since_seconds) ? durationText(x.since_seconds) : "—",
      observedAt: x.observed_at || null,
      ageSeconds: Number.isFinite(x.age_seconds) ? x.age_seconds : null,
    };
  });
}

/** Names that are configured but have not reported yet (never shown with a state). */
export function pendingServices(payload) {
  return Array.isArray(payload?.pending) ? payload.pending.filter(n => typeof n === "string") : [];
}

/** "3 running · 1 failed" — only the non-zero parts, worst first; null when nothing is watched. */
export function serviceSummaryText(payload) {
  const m = payload?.summary;
  if (!m || !m.total) return null;
  const parts = [];
  for (const k of ["failed", "stopped", "unknown", "running", "pending"]) {
    if (m[k] > 0) parts.push(`${m[k]} ${k === "pending" ? "waiting for first report" : k}`);
  }
  return parts.join(" · ");
}

/** The worst tone among the services (crit > warn > mute > ok), for the panel badge; null when none reported. */
export function serviceWorstTone(payload) {
  const rows = serviceRows(payload);
  if (!rows.length) return null;
  for (const t of ["crit", "warn", "mute"]) if (rows.some(r => r.tone === t)) return t;
  return "ok";
}

/** Caption under the services table. States facts only. */
export function servicesNote(payload, host) {
  if (!payload) return null;
  const parts = [];
  const state = stateOf(host);
  if (state !== "online" && serviceRows(payload).length) {
    parts.push(`This host is ${STATE_LABEL[state].toLowerCase()}, so these are the states from its last report, not live values.`);
  }
  if (pendingServices(payload).length) {
    parts.push(`Waiting for the first report of: ${pendingServices(payload).join(", ")}. The agent picks up a changed list on its next check-in.`);
  }
  return parts.length ? parts.join(" ") : null;
}

/** Values for the Configure form, from the services payload; null when the payload does not carry them. */
export function configFormDefaults(payload) {
  if (!payload || !Array.isArray(payload.watch_services) || !Number.isFinite(payload.interval_seconds)) return null;
  return { intervalText: String(payload.interval_seconds), servicesText: payload.watch_services.join("\n") };
}

/** { interval_seconds, watch_services } of the services payload, for buildConfigPatch's `current`; null when unknown. */
export function currentConfigOf(payload) {
  if (!configFormDefaults(payload)) return null;
  return { interval_seconds: payload.interval_seconds, watch_services: payload.watch_services.slice() };
}

// ── charts (GET /api/host-agents/{id}/history) ───────────────────────────────────────────────────────────────────────
// The same look-back presets as the EC2 / RDS pages (history is kept 30 days, so 1M is the longest).
export const CHART_RANGES = [
  { label: "1H", hours: 1 }, { label: "3H", hours: 3 }, { label: "6H", hours: 6 },
  { label: "1D", hours: 24 }, { label: "1W", hours: 168 }, { label: "1M", hours: 720 },
];
export const DEFAULT_CHART_HOURS = 6;
// Host metrics are gauges stored as averages, so Sum is never valid (the server does not offer it either).
export const CHART_STAT_OPTIONS = ["auto", "Average", "Minimum", "Maximum", "SampleCount"];
// Charts re-read stored data once a minute (the default agent report interval); a faster refresh only re-reads the same rows.
export const CHART_REFRESH_MS = 60000;
export const CHART_REFRESH_TIP = "Reload the charts from CloudOps. This reads stored data only; nothing is requested from the host. "
  + "New points arrive each time the agent reports (default every 60 s), and the charts reload automatically every minute while this tab is visible.";

// Display groups, in the order they appear. Each contract metric belongs to exactly one group; a metric the UI does not know
// yet (a newer agent) lands in "Other" instead of being dropped.
const GROUPS = [
  { key: "compute", title: "CPU and load", contracts: ["cpu_percent", "load1", "load5", "load15"] },
  { key: "memory", title: "Memory", contracts: ["mem_used_percent", "mem_available_bytes", "swap_used_percent"] },
  { key: "disk", title: "Disk", contracts: ["disk_used_percent", "disk_inodes_used_percent", "disk_read_bytes_per_s", "disk_write_bytes_per_s"] },
  { key: "network", title: "Network", contracts: ["net_rx_bytes_per_s", "net_tx_bytes_per_s"] },
  { key: "system", title: "System", contracts: ["proc_count", "uptime_seconds"] },
];
const COLOR = {
  cpu_percent: "#2bb3ac", load1: "#38bdf8", load5: "#60a5fa", load15: "#818cf8",
  mem_used_percent: "#7c6ee0", mem_available_bytes: "#a78bfa", swap_used_percent: "#f472b6",
  disk_used_percent: "#fbbf24", disk_inodes_used_percent: "#f59e0b", disk_read_bytes_per_s: "#34d399", disk_write_bytes_per_s: "#22c55e",
  net_rx_bytes_per_s: "#22c55e", net_tx_bytes_per_s: "#7c6ee0", proc_count: "#38bdf8", uptime_seconds: "#94a3b8",
};
export const chartColor = (contract) => COLOR[contract] || "#2bb3ac";

/** Series -> [{key, title, items}] in display order; empty groups are omitted. `items` keep the server's order (contract,
 *  then mount / interface), and the first series of a group may be drawn full width (`wide`). Pure. */
export function chartGroups(series) {
  const list = Array.isArray(series) ? series.filter(s => s && s.name) : [];
  const used = new Set();
  const out = [];
  for (const g of GROUPS) {
    const items = [];
    for (const c of g.contracts) for (const s of list) if (s.contract === c) { items.push(s); used.add(s); }
    if (items.length) out.push({ key: g.key, title: g.title, items });
  }
  const rest = list.filter(s => !used.has(s));
  if (rest.length) out.push({ key: "other", title: "Other", items: rest });
  return out;
}

/** One line of context under the charts, or "". Never claims a value; only says where the data comes from and what is missing. */
export function chartsNote(payload, host) {
  if (!payload || typeof payload !== "object") return "";
  const notes = [];
  const st = stateOf(host || {});
  if (st === "offline" || st === "stale") {
    notes.push(`This host is ${st === "offline" ? "offline" : "not reporting on time"}: the charts end at the last data it delivered and are not extended.`);
  } else if (st === "revoked") {
    notes.push("This host is revoked: the charts show the history it delivered before that.");
  }
  if (payload.shared_series) {
    notes.push("This host is attached to an EC2 resource: memory and disk charts use the same stored names as the CloudWatch agent, so a series can combine both sources.");
  }
  if (Array.isArray(payload.series) && payload.series.length === 0) {
    notes.push("No metric history has been stored for this host yet.");
  }
  return notes.join(" ");
}
