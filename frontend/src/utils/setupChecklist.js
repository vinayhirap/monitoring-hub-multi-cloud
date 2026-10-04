// First-run checklist model (audit B9): which steps to show, where each one goes, and who may see it.
// Pure, so it is unit-tested. The page each link opens enforces its own permission; this only avoids showing a
// link to someone who would be bounced.
export const STEP_META = {
  accounts:      { title: "Connect a cloud account",         to: "/onboarding",          perm: "accounts.onboard" },
  notifications: { title: "Send alerts to Slack, Teams or email", to: "/settings#notifications", perm: "notifications.manage" },
  synthetic:     { title: "Add an uptime check",             to: "/synthetic-checks",    perm: null },
  slo:           { title: "Define a service level objective", to: "/slos",               perm: null },
  status_page:   { title: "Publish a status page",           to: "/status-page-admin",   perm: null },
  escalation:    { title: "Set an escalation policy",        to: "/escalation-policies", perm: null },
};

/**
 * @param {{steps: {key:string, done:boolean, hint:string}[]}} status  from GET /api/setup/status
 * @param {(perm:string)=>boolean} hasPermission
 * @param {string} role
 * @returns {{show:boolean, remaining:object[], done:number, total:number}}
 */
export function buildChecklist(status, hasPermission, role) {
  const empty = { show: false, remaining: [], done: 0, total: 0 };
  if (!status || !Array.isArray(status.steps)) return empty;
  // Only people who administer the product see setup nudges; a read-only viewer cannot act on them.
  const isAdmin = String(role || "").toLowerCase() === "admin";
  if (!isAdmin) return empty;
  const steps = status.steps.filter(s => STEP_META[s.key]);
  const remaining = steps
    .filter(s => !s.done)
    .map(s => ({ ...STEP_META[s.key], key: s.key, hint: s.hint }))
    .filter(s => !s.perm || hasPermission(s.perm));
  return {
    show: remaining.length > 0,
    remaining,
    done: steps.filter(s => s.done).length,
    total: steps.length,
  };
}
