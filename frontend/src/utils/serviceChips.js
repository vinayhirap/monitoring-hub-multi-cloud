// monitoring-hub/frontend/src/utils/serviceChips.js
//
// What the Overview account and region cards show under the health ring: one
// chip per SERVICE the account actually monitors, worst alert state first.
//
// It used to be four hardcoded chips (EC2, EBS, S3, Lambda) next to a ring that
// counted every service on the Services page, so a card could say "12 services"
// and show four of them. The chips now come from the same set the ring counts
// (row.active_services, plus any service with a firing alert, which the backend
// also adds to the ring), so the two always agree.
import { SERVICE_LABEL, parseTs, FRESH_MIN, STALE_MIN } from "./dashboardModel.js";

// Live per-type totals every row already carries. Used for a chip's number only
// when the DB-backed row.service_counts has nothing for that service (a payload
// cached before the field existed, or the count query failed).
const LIVE_TOTAL_FIELD = {
  ec2: "ec2_total", ebs: "ebs_total", rds: "rds_total", lambda: "lambda_total",
  s3: "s3_total", elb: "elb_total", ecs: "ecs_total",
};

const SEV_RANK = { critical: 0, warning: 1, none: 2 };

/** Short display name for a service key. Known core services keep their usual
 *  spelling; otherwise the catalog's display_service (row.service_labels); otherwise
 *  the key itself, tidied up. */
export function serviceChipLabel(key, labels) {
  const k = String(key || "");
  if (SERVICE_LABEL[k]) return SERVICE_LABEL[k];
  if (labels && labels[k]) return String(labels[k]);
  if (k.length <= 4) return k.toUpperCase();
  const spaced = k.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Chips for one or more region rows of the SAME account (an account card passes
 *  all of its regions, a region row passes just its own).
 *
 *  Returns [{ key, label, count|null, sev: "critical"|"warning"|"none", rowId, sub }]
 *  sorted: critical, then warning, then healthy; largest count first inside each
 *  group; label as the final tie-break so the order is stable between polls.
 *  rowId is the first region row that has the service, i.e. where the chip links. */
export function buildServiceChips(rows) {
  const byKey = new Map();
  const ec2 = { total: 0, running: 0, stopped: 0 };

  for (const r of rows || []) {
    const crit = new Set(r.health_ring?.critical_services || []);
    const warn = new Set(r.health_ring?.warning_services || []);
    ec2.total   += Number(r.ec2_total)   || 0;
    ec2.running += Number(r.ec2_running) || 0;
    ec2.stopped += Number(r.ec2_stopped) || 0;

    const keys = new Set([...(r.active_services || []), ...crit, ...warn]);
    for (const key of keys) {
      let chip = byKey.get(key);
      if (!chip) {
        chip = { key, label: serviceChipLabel(key, r.service_labels), count: null, sev: "none", rowId: r.id, sub: null };
        byKey.set(key, chip);
      }
      const fromDb   = r.service_counts?.[key];
      const fromLive = LIVE_TOTAL_FIELD[key] ? r[LIVE_TOTAL_FIELD[key]] : undefined;
      const n = typeof fromDb === "number" ? fromDb : typeof fromLive === "number" ? fromLive : null;
      if (n != null) chip.count = (chip.count ?? 0) + n;
      if (crit.has(key)) chip.sev = "critical";
      else if (warn.has(key) && chip.sev !== "critical") chip.sev = "warning";
    }
  }

  // EC2 is the one service where state matters, so it keeps its "running / stopped"
  // detail. It comes from the live describe call and the chip number from the DB;
  // when the two disagree (discovery lag) the detail is dropped rather than shown
  // next to a number it does not add up to.
  const e = byKey.get("ec2");
  if (e && e.count != null && e.count === ec2.total && ec2.total > 0) {
    e.sub = `${ec2.running} running · ${ec2.stopped} stopped`;
  }

  return [...byKey.values()].sort((a, b) =>
    SEV_RANK[a.sev] - SEV_RANK[b.sev] ||
    (b.count ?? -1) - (a.count ?? -1) ||
    a.label.localeCompare(b.label));
}

/** How fresh an account's data is, judged by its LEAST recently synced region.
 *  Same thresholds as the "Regions synced" KPI (FRESH_MIN / STALE_MIN), so the
 *  dot on a card and that KPI never tell different stories.
 *  Returns { level: "fresh"|"aging"|"stale"|"unknown", ageMs|null }. */
export function syncFreshness(rows, now = Date.now()) {
  const ages = (rows || []).map(r => {
    const t = parseTs(r.last_synced_at);
    return t == null ? null : Math.max(0, now - t);
  });
  if (!ages.length || ages.some(a => a == null)) return { level: "unknown", ageMs: null };
  const ageMs = Math.max(...ages);
  const level = ageMs <= FRESH_MIN * 60000 ? "fresh" : ageMs <= STALE_MIN * 60000 ? "aging" : "stale";
  return { level, ageMs };
}
