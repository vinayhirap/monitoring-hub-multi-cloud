// src/utils/alertGroups.js
// Severity sections for the Alerts table. The server already orders rows
// CRITICAL -> WARNING -> INFO (then newest first); this turns that order into
// visible section headers so an operator can see where each severity starts.

// Tabs whose rows are sections of "things still open". Resolved is chronological
// (newest resolution first) and All mixes states, so neither is sectioned.
export const SECTIONED_TABS = new Set([
  "active", "critical", "attention", "tuning", "stale", "acknowledged", "suppressed",
]);

export const SEVERITY_LABEL = { CRITICAL: "Critical", WARNING: "Warning", INFO: "Info" };

const sevOf = (a) => String((a && a.severity) || "INFO").toUpperCase();

/**
 * @returns {Object<number, {sev:string,label:string,count:number}>} a header for every row
 * whose severity differs from the row above it (and for the first row). Empty when the tab
 * is not sectioned.
 */
export function severityHeaders(rows, tab) {
  if (!SECTIONED_TABS.has(tab) || !Array.isArray(rows) || rows.length === 0) return {};
  const totals = {};
  rows.forEach((r) => { const s = sevOf(r); totals[s] = (totals[s] || 0) + 1; });
  const out = {};
  rows.forEach((r, i) => {
    const s = sevOf(r);
    if (i === 0 || sevOf(rows[i - 1]) !== s) {
      out[i] = { sev: s, label: SEVERITY_LABEL[s] || s, count: totals[s] };
    }
  });
  return out;
}
