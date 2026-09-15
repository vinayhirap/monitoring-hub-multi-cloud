// src/api/api.js
import { clearAllCached } from "../utils/dataCache";

const BASE = "";

async function apiFetch(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  if (res.status === 401) {
    // Session expired (or never existed) — bounce to login rather
    // than leaving the caller to interpret a raw fetch failure.
    // SECURITY: also clear the localStorage data cache here, not just
    // in AuthContext's logout() — a 401 can happen without the user
    // ever clicking "log out" (server-side session expiry, a
    // restarted backend, etc.), and without this, whatever this
    // browser cached under the just-ended session stays sitting
    // around for the next login on the same device to render before
    // its own scoped fetch resolves. See dataCache.js's
    // clearAllCached() docstring for the full shared-device scenario.
    clearAllCached();
    if (window.location.pathname !== "/login") {
      window.location.href = "/login";
    }
    throw new Error(`API ${path} \u2192 401 (session expired)`);
  }
  if (!res.ok) throw new Error(`API ${path} \u2192 ${res.status}`);
  return res.json();
}

// ── Live real AWS data ──────────────────────────────────────────────────────
export const getLiveAccounts  = ()   => apiFetch("/api/live/accounts");
export const getResourceCounts = (id) => apiFetch(`/api/live/resource-counts/${id}`);
export const getResourcesList  = (id, service) => apiFetch(`/api/live/resources-list/${id}/${service}`);
export const getLiveEC2       = (id) => apiFetch(`/api/live/ec2/${id}`);
export const getLiveRDS       = (id) => apiFetch(`/api/live/rds/${id}`);
export const getLiveLambda    = (id) => apiFetch(`/api/live/lambda/${id}`);
export const getLiveEC2Metrics= (instanceId, region) =>
  apiFetch(`/api/live/metrics/ec2/${instanceId}${region ? `?region=${region}` : ""}`);

// ── Admin ──────────────────────────────────────────────────────────
export const getAccounts      = ()   => apiFetch("/api/admin/accounts");
export const addAccount       = (data) => apiFetch("/api/admin/accounts", { method:"POST", body: JSON.stringify(data) });
export const discoverAccount  = (id)   => apiFetch(`/api/admin/accounts/${id}/discover`, { method:"POST" });
export const testRole         = (data) => apiFetch("/api/admin/accounts/test-role", { method:"POST", body: JSON.stringify(data) });
export const testAzureCredentials = (data) => apiFetch("/api/admin/accounts/test-azure-credentials", { method:"POST", body: JSON.stringify(data) });
export const testGcpCredentials   = (data) => apiFetch("/api/admin/accounts/test-gcp-credentials",   { method:"POST", body: JSON.stringify(data) });

// ── Alerts ──────────────────────────────────────────────────────────
export const getAlerts = () => apiFetch("/api/alerts/open");
// Uncapped, authoritative tab-badge counts -- see app/api/alerts.py's
// /counts endpoint docstring for why this must never be derived from
// a paginated row list on the client.
export const getAlertCounts = () => apiFetch("/api/alerts/counts");
export const acknowledgeAlert = (id) => apiFetch(`/api/alerts/${id}/ack`,     { method: "PATCH" });
export const resolveAlert     = (id) => apiFetch(`/api/alerts/${id}/resolve`,  { method: "PATCH" });
export const muteAlert        = (id) => apiFetch(`/api/alerts/${id}/mute`,     { method: "PATCH" });
// Deep RCA (2026-09-14) -- plain-English probable-root-cause
// explanation for a single alert, fetched lazily (only when the user
// expands "Why did this happen?" on that row), not preloaded for
// every alert in the list.
export const explainAlert     = (id) => apiFetch(`/api/alerts/${id}/explain`);

// ── Audit logs ────────────────────────────────────────────────────
export const getAuditLogs     = (limit=100) => apiFetch(`/api/audit-logs?limit=${limit}`);

// ── Topology / dependency graph (roadmap phase 4/7) ──────────────────────
export const getTopology      = (accountId) => apiFetch(`/api/topology/${accountId}`);
export const addManualEdge    = (accountId, sourceId, targetId, relationshipType="depends_on") =>
  apiFetch(`/api/topology/${accountId}/manual-edge`, {
    method: "POST",
    body: JSON.stringify({ source_resource_id: sourceId, target_resource_id: targetId, relationship_type: relationshipType }),
  });
export const deleteManualEdge = (accountId, edgeId) =>
  apiFetch(`/api/topology/${accountId}/manual-edge/${edgeId}`, { method: "DELETE" });

// ── Auth ──────────────────────────────────────────────────────────
export const login = (username, password) =>
  apiFetch("/api/auth/login", { method:"POST", body: JSON.stringify({ username, password }) });

// ── Metric catalog ────────────────────────────────────────────────────
export const getMetricCatalog        = (params = {}) => {
  const qs = new URLSearchParams(params).toString();
  return apiFetch(`/api/metric-catalog${qs ? `?${qs}` : ""}`);
};
export const getMetricCatalogServices = () => apiFetch("/api/metric-catalog/services");
export const getDefaultTemplate       = () => apiFetch("/api/metric-catalog/default-template");
export const getAccountMetrics        = (accountId) => apiFetch(`/api/account-metrics/${accountId}`);
export const saveAccountMetrics       = (accountId, enabledIds) =>
  apiFetch(`/api/account-metrics/${accountId}`, { method: "PUT", body: JSON.stringify({ enabled_metric_ids: enabledIds }) });
export const applyDefaultTemplate     = (accountId) =>
  apiFetch(`/api/account-metrics/${accountId}/apply-default`, { method: "POST" });

// ── Federated AWS Console deep link (same endpoint the Alerts page uses) ──
// POST, not GET: this endpoint writes an audit-log entry as a side
// effect (see app/api/admin/accounts.py's docstring for the CSRF
// reasoning) -- the query-param-only signature is unchanged, just the
// HTTP method.
export const getConsoleUrl = (accountId, service) =>
  apiFetch(`/api/admin/accounts/${accountId}/console-url?service=${encodeURIComponent(service)}`, { method: "POST" });
export const discoverNamespaceMetrics = (accountId, namespace, region) =>
  apiFetch(`/api/account-metrics/${accountId}/discover?namespace=${encodeURIComponent(namespace)}${region ? `&region=${region}` : ""}`, { method: "POST" });
export const downloadYaceConfig = (accountId, tier) =>
  `/api/account-metrics/${accountId}/yace-config${tier ? `?tier=${tier}` : ""}`;

// ── Operational events (roadmap phase 5) ─────────────────────────────────
export const getOpEvents = (params = {}) => {
  const qs = new URLSearchParams(params).toString();
  return apiFetch(`/api/op-events${qs ? `?${qs}` : ""}`);
};

// ── Incidents / health / RCA (AIOps roadmap Phase 1, 2026-09-14) ─────────
export const getIncidents = (accountId, params = {}) => {
  const qs = new URLSearchParams(params).toString();
  return apiFetch(`/api/incidents/${accountId}${qs ? `?${qs}` : ""}`);
};
export const getIncidentDetail = (accountId, incidentId) =>
  apiFetch(`/api/incidents/${accountId}/${incidentId}/detail`);
export const getResourceHealth = (accountId) =>
  apiFetch(`/api/incidents/${accountId}/health`);
export const getCapacityForecast = (accountId, resourceId) =>
  apiFetch(`/api/incidents/${accountId}/forecast/${encodeURIComponent(resourceId)}`);

// ── Escalation policies (roadmap phase 9) ─────────────────────────────────
// NOTE: EscalationPolicies.jsx previously rolled its own local fetch
// wrapper instead of using apiFetch here, because these five helpers
// simply didn't exist yet. Added now so it's consistent with every
// other page's API-call pattern.
export const getEscalationPolicies = () => apiFetch("/api/escalation-policies");
export const getEscalationGroups   = () => apiFetch("/api/escalation-policies/groups");
export const createEscalationPolicy = (data) =>
  apiFetch("/api/escalation-policies", { method: "POST", body: JSON.stringify(data) });
export const updateEscalationPolicy = (id, data) =>
  apiFetch(`/api/escalation-policies/${id}`, { method: "PATCH", body: JSON.stringify(data) });
export const deleteEscalationPolicy = (id) =>
  apiFetch(`/api/escalation-policies/${id}`, { method: "DELETE" });

// ── Synthetic / uptime monitoring ────────────────────────────────────
export const getSyntheticChecks   = () => apiFetch("/api/synthetic-checks");
export const getSyntheticResults  = (id, hours = 24) => apiFetch(`/api/synthetic-checks/${id}/results?hours=${hours}`);
export const createSyntheticCheck = (data) => apiFetch("/api/synthetic-checks", { method: "POST", body: JSON.stringify(data) });
export const updateSyntheticCheck = (id, data) => apiFetch(`/api/synthetic-checks/${id}`, { method: "PATCH", body: JSON.stringify(data) });
export const deleteSyntheticCheck = (id) => apiFetch(`/api/synthetic-checks/${id}`, { method: "DELETE" });

// ── SLO / error-budget tracking ──────────────────────────────────────
export const getSlos      = () => apiFetch("/api/slo");
export const createSlo    = (data) => apiFetch("/api/slo", { method: "POST", body: JSON.stringify(data) });
export const updateSlo    = (id, data) => apiFetch(`/api/slo/${id}`, { method: "PATCH", body: JSON.stringify(data) });
export const deleteSlo    = (id) => apiFetch(`/api/slo/${id}`, { method: "DELETE" });

// ── Lite CSPM security findings ──────────────────────────────────────
export const getSecurityFindings = (status = "open", severity = null) =>
  apiFetch(`/api/security-findings?status=${status}${severity ? `&severity=${severity}` : ""}`);
export const getSecurityFindingsSummary = () => apiFetch("/api/security-findings/summary");

// ── Maintenance windows ──────────────────────────────────────────────
export const getMaintenanceWindows   = () => apiFetch("/api/maintenance-windows");
export const createMaintenanceWindow = (data) => apiFetch("/api/maintenance-windows", { method: "POST", body: JSON.stringify(data) });
export const updateMaintenanceWindow = (id, data) => apiFetch(`/api/maintenance-windows/${id}`, { method: "PATCH", body: JSON.stringify(data) });
export const deleteMaintenanceWindow = (id) => apiFetch(`/api/maintenance-windows/${id}`, { method: "DELETE" });

// ── Deploy-risk correlation ───────────────────────────────────────────
export const getDeployRisk = (days = 7) => apiFetch(`/api/deploy-risk?days=${days}`);

// ── Public status page (admin curation -- authenticated) ─────────────
export const getStatusPageComponents   = () => apiFetch("/api/status-page/components");
export const createStatusPageComponent = (data) => apiFetch("/api/status-page/components", { method: "POST", body: JSON.stringify(data) });
export const updateStatusPageComponent = (id, data) => apiFetch(`/api/status-page/components/${id}`, { method: "PATCH", body: JSON.stringify(data) });
export const deleteStatusPageComponent = (id) => apiFetch(`/api/status-page/components/${id}`, { method: "DELETE" });
// The PUBLIC status page itself (no auth) is fetched directly with
// plain fetch() in pages/StatusPagePublic.jsx, not via apiFetch --
// apiFetch redirects to /login on a 401, which would be wrong for an
// endpoint that's supposed to work for a logged-out visitor.

// ── Natural-language alert search ────────────────────────────────────
export const searchAlerts = (q) => apiFetch(`/api/search?q=${encodeURIComponent(q)}`);

// ── Downloadable postmortems (LLM-polished, alerts.js's explain) ─────
// Not fetched via apiFetch -- this triggers a file download, see
// pages/Alerts.jsx's handlePostmortemDownload for why it opens the URL
// directly instead of parsing a JSON response.
export const postmortemUrl = (alertId, format = "pdf") => `/api/alerts/${alertId}/postmortem?format=${format}`;
