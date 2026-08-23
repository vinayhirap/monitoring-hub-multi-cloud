// src/api/api.js
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
export const acknowledgeAlert = (id) => apiFetch(`/api/alerts/${id}/ack`,     { method: "PATCH" });
export const resolveAlert     = (id) => apiFetch(`/api/alerts/${id}/resolve`,  { method: "PATCH" });
export const muteAlert        = (id) => apiFetch(`/api/alerts/${id}/mute`,     { method: "PATCH" });

// ── Audit logs ────────────────────────────────────────────────────
export const getAuditLogs     = (limit=100) => apiFetch(`/api/audit-logs?limit=${limit}`);

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
export const discoverNamespaceMetrics = (accountId, namespace, region) =>
  apiFetch(`/api/account-metrics/${accountId}/discover?namespace=${encodeURIComponent(namespace)}${region ? `&region=${region}` : ""}`, { method: "POST" });
export const downloadYaceConfig = (accountId, tier) =>
  `/api/account-metrics/${accountId}/yace-config${tier ? `?tier=${tier}` : ""}`;
