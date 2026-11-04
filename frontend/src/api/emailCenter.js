// src/api/emailCenter.js
//
// One API layer for the Email & Reports page (app/api/email_center.py). Every call goes through apiFetch, so a 401
// sends the person to sign-in the same way as the rest of the app. Errors carry the backend's `detail` (see errMsg).
import { apiFetch } from "./api";

const j = (method, body) => ({ method, body: body === undefined ? undefined : JSON.stringify(body) });
const BASE = "/api/email-center";

export { errMsg } from "./access";

export const getStatus      = () => apiFetch(`${BASE}/status`);
export const getOptions     = () => apiFetch(`${BASE}/options`);
export const sendTestEmail  = (to) => apiFetch(`${BASE}/test`, j("POST", { to }));

export const listLists      = () => apiFetch(`${BASE}/lists`);
export const createList     = (d) => apiFetch(`${BASE}/lists`, j("POST", d));
export const updateList     = (id, d) => apiFetch(`${BASE}/lists/${id}`, j("PUT", d));
export const deleteList     = (id) => apiFetch(`${BASE}/lists/${id}`, j("DELETE"));
export const previewList    = (id, accountId) =>
  apiFetch(`${BASE}/lists/${id}/preview${accountId ? `?account_id=${encodeURIComponent(accountId)}` : ""}`);

export const listRules      = () => apiFetch(`${BASE}/rules`);
export const createRule     = (d) => apiFetch(`${BASE}/rules`, j("POST", d));
export const updateRule     = (id, d) => apiFetch(`${BASE}/rules/${id}`, j("PUT", d));
export const deleteRule     = (id) => apiFetch(`${BASE}/rules/${id}`, j("DELETE"));

export const listSchedules  = () => apiFetch(`${BASE}/schedules`);
export const createSchedule = (d) => apiFetch(`${BASE}/schedules`, j("POST", d));
export const updateSchedule = (id, d) => apiFetch(`${BASE}/schedules/${id}`, j("PUT", d));
export const deleteSchedule = (id) => apiFetch(`${BASE}/schedules/${id}`, j("DELETE"));
export const runSchedule    = (id, dryRun = false) => apiFetch(`${BASE}/schedules/${id}/run`, j("POST", { dry_run: !!dryRun }));

export const getDeliveryLog = ({ status, category, beforeId, limit = 50 } = {}) => {
  const q = new URLSearchParams();
  if (status) q.set("status", status);
  if (category) q.set("category", category);
  if (beforeId) q.set("before_id", String(beforeId));
  q.set("limit", String(limit));
  return apiFetch(`${BASE}/log?${q.toString()}`);
};
