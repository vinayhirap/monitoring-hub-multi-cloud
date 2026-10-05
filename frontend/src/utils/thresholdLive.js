// Live view of the limits actually in force (GET /api/settings/thresholds/effective).
//
// A threshold card used to show only the typed warn / crit numbers. For a dynamic row each resource really has its own
// learned limit, and for a "collected for anomaly detection only" row the typed 1,000,000 / 5,000,000 are placeholders that
// nothing enforces. These pure helpers turn the server's summary into wording, and merge fresh server state into the page
// WITHOUT overwriting a value somebody is in the middle of typing.
import { formatMetricValue } from "./metricLabels.js";

const num = v => (v === "" || v == null ? NaN : Number(v));
const same = (a, b) => num(a) === num(b) || (Number.isNaN(num(a)) && Number.isNaN(num(b)));

/** "1.1K to 4.2K", or a single number when min == max, or "n/a". */
export function formatSpread(spread, metricName) {
  if (!spread) return "n/a";
  const lo = formatMetricValue(metricName, spread.min), hi = formatMetricValue(metricName, spread.max);
  return lo === hi ? lo : `${lo} to ${hi}`;
}

/** Everything a card needs to explain what is in force, or null for a plain fixed-limit row. */
export function describeLimits(eff, metricName) {
  if (!eff || eff.mode === "static") return null;
  const total = eff.resources_total ?? 0, have = eff.with_limit ?? 0;
  const base = { mode: eff.mode, total, have, learning: eff.learning || 0, updated: eff.baseline_updated_at || null };
  if (have === 0) {
    return { ...base, headline: eff.mode === "anomaly" ? "No learned line yet" : "Using the fixed values for now",
      detail: total === 0 ? "No resources of this type report this metric."
        : "Not enough history per resource yet. Limits appear once each resource has a stable baseline for this hour of the week." };
  }
  const warn = formatSpread(eff.warning, metricName), crit = formatSpread(eff.critical, metricName);
  const same = warn === crit;
  return { ...base,
    headline: eff.mode === "anomaly"
      ? `Learned alert line now: ${warn}`
      : same ? `Learned limit now: ${warn}` : `Learned now: warn ${warn}, crit ${crit}`,
    detail: `${have} of ${total} ${total === 1 ? "resource" : "resources"} have a learned limit` +
      (base.learning ? `; ${base.learning} still learning` : "") + ". Each resource uses its own, refreshed hourly." };
}

/** The caption for the typed inputs, so they are never mistaken for the limit when they are not. */
export function fixedValuesCaption(mode) {
  if (mode === "dynamic") return "Fixed values: used only until a resource has enough history";
  if (mode === "anomaly") return "Placeholder values: not enforced. Alerts use the learned line.";
  return "";
}

export const SAVE_HOLD_MS = 45000;

/**
 * Merge a poll result into the rows on screen.
 *  - mode / use_dynamic / dynamic_k / enabled always follow the server (the auto-tuner can flip these at any time);
 *  - warning / critical follow the server ONLY while the card still holds the last values the server sent, i.e. nobody has
 *    started editing it. An edited card keeps what the person typed until they save;
 *  - for SAVE_HOLD_MS after a Save the saved pair wins over a server pair that differs: with two API workers, the other one
 *    can still answer from its 30 s cache with the OLD numbers, which would make a successful save look lost.
 * @param {object} holds  id -> {until, warning_value, critical_value}, from holdAfterSave()
 * @returns {{rows: object[], lastServer: object}}
 */
export function mergeLiveThresholds(rows, limits, lastServer = {}, holds = {}, now = Date.now()) {
  const nextLast = { ...lastServer };
  const merged = (rows || []).map(t => {
    const s = limits ? limits[t.id] : null;
    if (!s) return t;
    const prev = lastServer[t.id];
    const next = { ...t, mode: s.mode, use_dynamic: s.use_dynamic ? 1 : 0, dynamic_k: s.dynamic_k, enabled: s.enabled ? 1 : 0 };
    const hold = holds[t.id];
    const holding = hold && now < hold.until && !(same(s.warning_value, hold.warning_value) && same(s.critical_value, hold.critical_value));
    if (holding) return next;                                  // keep the saved pair on screen; lastServer stays as saved
    const untouched = !prev || (same(t.warning_value, prev.warning_value) && same(t.critical_value, prev.critical_value));
    if (untouched) {
      next.warning_value = s.warning_value;
      next.critical_value = s.critical_value;
      nextLast[t.id] = { warning_value: s.warning_value, critical_value: s.critical_value };
    } else if (!prev) {
      nextLast[t.id] = { warning_value: s.warning_value, critical_value: s.critical_value };
    }
    return next;
  });
  return { rows: merged, lastServer: nextLast };
}

/** After a successful Save the stored values equal what is on screen: remember them so the card counts as untouched. */
export function markSaved(lastServer, t) {
  return { ...lastServer, [t.id]: { warning_value: t.warning_value, critical_value: t.critical_value } };
}

export function holdAfterSave(holds, t, now = Date.now()) {
  return { ...holds, [t.id]: { until: now + SAVE_HOLD_MS, warning_value: t.warning_value, critical_value: t.critical_value } };
}

/** "just now", "12 min ago", "3 h ago", "2 d ago" for an ISO time; "" when unknown. */
export function ageText(iso, now = Date.now()) {
  const t = iso ? Date.parse(iso) : NaN;
  if (Number.isNaN(t)) return "";
  const m = Math.max(0, Math.round((now - t) / 60000));
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  if (m < 1440) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} d ago`;
}
