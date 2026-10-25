// src/api/hostAgents.js
//
// Host agent (NOT the AI agent in app/agent/*) -- /api/host-agents/*, see app/api/host_agents.py.
// Every route answers 404 while HOST_AGENT_ENABLED is false, so callers treat a 404 as "feature off".
import { apiFetch } from "./api";

const j = (method, body) => ({ method, body: body === undefined ? undefined : JSON.stringify(body) });

export const listHostAgents      = () => apiFetch("/api/host-agents");
export const listEnrollmentKeys  = () => apiFetch("/api/host-agents/enrollment-keys");
// body {aws_account_id, label, max_uses, expires_in_hours} -> {key, id}. The key is returned exactly once.
export const createEnrollmentKey = (d) => apiFetch("/api/host-agents/enrollment-keys", j("POST", d));
export const revokeEnrollmentKey = (id) => apiFetch(`/api/host-agents/enrollment-keys/${encodeURIComponent(id)}`, j("DELETE"));
export const revokeHostAgent     = (id) => apiFetch(`/api/host-agents/${encodeURIComponent(id)}/revoke`, j("POST"));
// body {watch_services?: string[], interval_seconds?: 15..300}
export const patchHostAgentConfig = (id, d) => apiFetch(`/api/host-agents/${encodeURIComponent(id)}/config`, j("PATCH", d));
// -> {host_agent_id, hostname, resource_type, shared_series, source, metrics: [{name, metric, title, label, label_key, unit, value, timestamp, age_seconds}]}
// Latest stored reading per agent metric; a DB read on the server (no cloud call). 404 = unknown host, out of scope, or feature off.
export const getHostAgentMetrics = (id) => apiFetch(`/api/host-agents/${encodeURIComponent(id)}/metrics`);
// -> {host_agent_id, hostname, status, config_version, interval_seconds, watch_services: string[],
//     services: [{name, state: running|stopped|failed|unknown, since, observed_at, since_seconds, age_seconds}],
//     pending: string[], summary: {total, running, stopped, failed, unknown, pending}}
// The host's current config (what Configure edits) and the latest stored state of each watched service. DB read only.
export const getHostAgentServices = (id) => apiFetch(`/api/host-agents/${encodeURIComponent(id)}/services`);
// -> {host_agent_id, hostname, status, resource_type, shared_series, requested_hours, effective_hours, bucket_secs, retention_days,
//     interval_seconds, series: [{name, contract, label_key, label, title, unit, data: [{t, v, a, mn, mx, s, n}]}],
//     meta: {<stored metric name>: the chart-card metadata (title, unit, stats, cadence, threshold lines, open alert)}}
// Bucketed history for the charts (same card as the EC2 page). DB read only; hours is capped at the 30-day retention.
export const getHostAgentHistory = (id, hours) =>
  apiFetch(`/api/host-agents/${encodeURIComponent(id)}/history?hours=${encodeURIComponent(hours)}`);
// -> {host_agent_id, hostname, status, os, agent_version, watch_services: string[], total, scanned_at, scan_age_seconds,
//     items: [{name, state, startup, description, major, watched}], missing_watched: string[]}
// Every service that EXISTS on the host at the agent's last scan (first report after start, then ~every 30 min). DB read only.
// Empty `items` = no scan has arrived yet (agent older than 0.2.2, or just enrolled), not "the host has no services".
export const getHostAgentServiceInventory = (id) => apiFetch(`/api/host-agents/${encodeURIComponent(id)}/service-inventory`);
