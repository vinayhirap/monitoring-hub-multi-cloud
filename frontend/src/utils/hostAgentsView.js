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

/** Placeholder shown while no installer URL is configured anywhere the UI can read (see gaps). Never a real URL. */
export const INSTALLER_PLACEHOLDER = "<INSTALLER_URL_NOT_CONFIGURED>";

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
 * PATCH body from the edit form. Only fields the person actually chose to change are sent (the list endpoint does
 * not return the current config, so nothing may be assumed or overwritten by default).
 * Returns { body } or { error }.
 */
export function buildConfigPatch({ intervalText, servicesText, clearServices }) {
  const body = {};
  if (String(intervalText ?? "").trim() !== "") {
    const n = parseInterval(intervalText);
    if (n == null) return { error: `Interval must be a whole number from ${INTERVAL_MIN} to ${INTERVAL_MAX} seconds.` };
    body.interval_seconds = n;
  }
  if (clearServices) {
    body.watch_services = [];
  } else if (String(servicesText ?? "").trim() !== "") {
    const { services, invalid, tooMany } = parseServiceList(servicesText);
    if (invalid.length) return { error: `Invalid service name: ${invalid[0].slice(0, 40)}` };
    if (tooMany) return { error: `At most ${MAX_WATCH_SERVICES} services can be watched.` };
    body.watch_services = services;
  }
  if (!Object.keys(body).length) return { error: "Nothing to change: set an interval and/or a list of services." };
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
 * Install one-liner. The installer's real CLI is NOT defined in the contract: the shape below (environment variables
 * CLOUDOPS_HUB_URL / CLOUDOPS_ENROLLMENT_KEY piped to sh) is an assumption to align with the installer (listed as a
 * gap). With no installerUrl the placeholder is rendered and `ready` is false so the UI can mark it clearly.
 */
export function buildInstallCommand({ hubUrl, key, installerUrl }) {
  const ready = !!installerUrl;
  const url = ready ? installerUrl : INSTALLER_PLACEHOLDER;
  const q = s => `'${String(s).replace(/'/g, `'\\''`)}'`;
  return {
    ready,
    command: `curl -fsSL ${q(url)} | sudo CLOUDOPS_HUB_URL=${q(hubUrl)} CLOUDOPS_ENROLLMENT_KEY=${q(key)} sh`,
  };
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
