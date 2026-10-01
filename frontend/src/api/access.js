// src/api/access.js
//
// One API layer for the unified Access Control page (users, groups, roles,
// scopes, bindings, overrides, reviews). Replaces the second, drifted
// `apiFetch` that used to live inside pages/UserManagement.jsx and the
// scattered wrappers for the same endpoints in api.js.
import { apiFetch } from "./api";

const j = (method, body) => ({ method, body: body === undefined ? undefined : JSON.stringify(body) });

/** Human-readable message for any error thrown by apiFetch. */
export const errMsg = (e) => e?.detail || e?.message || "Something went wrong";

// Users
export const listUsers        = () => apiFetch("/api/users");
export const createUser       = (d) => apiFetch("/api/users", j("POST", d));
export const getUserDetail    = (id) => apiFetch(`/api/users/${id}/detail`);
export const updateUser       = (id, d) => apiFetch(`/api/users/${id}`, j("PATCH", d));
export const setUserRole      = (id, role) => apiFetch(`/api/users/${id}/role`, j("PATCH", { role }));
export const deactivateUser   = (id) => apiFetch(`/api/users/${id}/deactivate`, j("POST"));
export const activateUser     = (id) => apiFetch(`/api/users/${id}/activate`, j("POST"));
export const resetUserPassword= (id) => apiFetch(`/api/users/${id}/reset-password`, j("POST"));
export const deleteUser       = (id) => apiFetch(`/api/users/${id}`, j("DELETE"));
export const grantUserAccess  = (id, scopes) => apiFetch(`/api/users/${id}/access`, j("POST", { scopes }));
export const revokeUserAccess = (scopeId) => apiFetch(`/api/users/access/${scopeId}`, j("DELETE"));
export const listAccounts     = () => apiFetch("/api/live/accounts");

// Groups
export const listGroups       = () => apiFetch("/api/groups");
export const getGroup         = (id) => apiFetch(`/api/groups/${id}`);
export const createGroup      = (d) => apiFetch("/api/groups", j("POST", d));
export const deleteGroup      = (id) => apiFetch(`/api/groups/${id}`, j("DELETE"));
export const addGroupMembers  = (id, userIds) => apiFetch(`/api/groups/${id}/members`, j("POST", { user_ids: userIds }));
export const removeGroupMember= (id, userId) => apiFetch(`/api/groups/${id}/members/${userId}`, j("DELETE"));
export const addGroupPolicy   = (id, scopes) => apiFetch(`/api/groups/${id}/policies`, j("POST", { scopes }));
export const removeGroupPolicy= (policyId) => apiFetch(`/api/groups/policies/${policyId}`, j("DELETE"));

// Roles & permission catalog
export const listRoles        = () => apiFetch("/api/rbac/roles");
export const createRole       = (d) => apiFetch("/api/rbac/roles", j("POST", d));
export const cloneRole        = (id, d) => apiFetch(`/api/rbac/roles/${id}/clone`, j("POST", d));
export const updateRole       = (id, d) => apiFetch(`/api/rbac/roles/${id}`, j("PATCH", d));
export const setRolePerms     = (id, permissions) => apiFetch(`/api/rbac/roles/${id}/permissions`, j("PUT", { permissions }));
export const deleteRole       = (id) => apiFetch(`/api/rbac/roles/${id}`, j("DELETE"));
export const permissionCatalog= () => apiFetch("/api/permissions");

// Scopes / bindings / overrides / reviews
export const listScopes       = () => apiFetch("/api/rbac/scopes");
export const createScope      = (d) => apiFetch("/api/rbac/scopes", j("POST", d));
export const deleteScope      = (id) => apiFetch(`/api/rbac/scopes/${id}`, j("DELETE"));
export const listBindings     = () => apiFetch("/api/rbac/bindings");
export const createBinding    = (d) => apiFetch("/api/rbac/bindings", j("POST", d));
export const updateBinding    = (id, d) => apiFetch(`/api/rbac/bindings/${id}`, j("PATCH", d));
export const deleteBinding    = (id) => apiFetch(`/api/rbac/bindings/${id}`, j("DELETE"));
export const listOverrides    = () => apiFetch("/api/rbac/overrides");
export const createOverride   = (d) => apiFetch("/api/rbac/overrides", j("POST", d));
export const deleteOverride   = (id) => apiFetch(`/api/rbac/overrides/${id}`, j("DELETE"));
export const listReviews      = () => apiFetch("/api/rbac/reviews");
export const createReview     = (d) => apiFetch("/api/rbac/reviews", j("POST", d));
