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
