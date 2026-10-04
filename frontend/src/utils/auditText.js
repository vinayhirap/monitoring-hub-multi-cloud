// Audit-log wording for people. The log stores plain text written by whatever action ran, and older rows (and a few
// existing messages) name an account by its database id: "account 10: 6 metric threshold(s) set from defaults",
// "account=7 metric_id=312 warn=70 crit=90". Nobody reads "account 10". This turns those into the account's real name
// for display, search and CSV export. The stored text is not changed.

const ACCOUNT_REF = /\baccount[ =]#?(\d+)\b/gi;

/** @param {string} text  @param {Record<string|number,string>} nameById */
export function humanizeAuditText(text, nameById = {}) {
  if (typeof text !== "string" || !text) return text;
  let out = text.replace(ACCOUNT_REF, (whole, id) => nameById[id] || nameById[Number(id)] || whole);
  // legacy message shapes
  out = out.replace(/\bmetric_id=(\d+)\s+warn=(\S+)\s+crit=(\S+)/g, "metric #$1 set to warning $2, critical $3");
  out = out.replace(/(\d+) metric threshold\(s\) set from defaults/g,
    (m, n) => `recommended defaults applied to ${n} metric ${Number(n) === 1 ? "threshold" : "thresholds"}`);
  // "6 metric(s)" / "1 alert(s)": pick the right plural
  out = out.replace(/\b(\d+)\s+([A-Za-z]+)\(s\)/g, (m, n, word) => `${n} ${Number(n) === 1 ? word : word + "s"}`);
  return out;
}

/** Returns a copy of an audit payload whose `detail` text is humanised. */
export function humanizePayload(payload, nameById = {}) {
  if (!payload || typeof payload !== "object" || typeof payload.detail !== "string") return payload;
  return { ...payload, detail: humanizeAuditText(payload.detail, nameById) };
}
