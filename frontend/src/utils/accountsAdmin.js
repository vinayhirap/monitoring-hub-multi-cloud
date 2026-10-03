// utils/accountsAdmin.js -- pure helpers for the Settings > Accounts & Regions section.
// A monitored "region" is one aws_accounts row (account x region). Removing it is IRREVERSIBLE on the server
// (alerts, metrics, resources, incidents, SLOs, synthetic checks, findings, maintenance windows,
// escalation policies and stored credentials are deleted), so the UI asks for the exact phrase below.

/** The phrase a person must type to confirm removing one account/region row. */
export const removalPhrase = row => `${row?.account_name ?? ""}/${row?.region ?? ""}`;

/** Group account x region rows by cloud account, regions sorted, accounts sorted by name. */
export function groupAccounts(rows) {
  const m = new Map();
  for (const r of rows || []) {
    const k = String(r.account_id ?? r.id);
    const g = m.get(k) || { key: k, account_id: r.account_id, account_name: r.account_name, regions: [] };
    g.regions.push(r);
    m.set(k, g);
  }
  return [...m.values()].map(g => ({ ...g, regions: g.regions.sort((a, b) => String(a.region).localeCompare(String(b.region))) }))
    .sort((a, b) => String(a.account_name).localeCompare(String(b.account_name)));
}

/** Human message for a failed removal. `err.message` from apiFetch contains the HTTP status. */
export function removalError(err) {
  const m = String(err?.message || "");
  if (/403/.test(m)) return { kind: "denied", gone: false, text: "You don't have permission to remove accounts. Ask an administrator (permission: Remove Accounts)." };
  if (/404/.test(m)) return { kind: "gone", gone: true, text: "That account/region was already removed." };
  if (/401/.test(m)) return { kind: "auth", gone: false, text: "Your session expired. Sign in again." };
  return { kind: "error", gone: false, text: "Removal failed. Nothing was changed on the screen; check the server and try again." };
}

export const regionStatusTone = s => ({ critical: "crit", warning: "warn", healthy: "ok" }[s] || "mute");
