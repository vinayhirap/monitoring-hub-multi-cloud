// src/utils/metricLabels.js
// ONE place that turns a stored metric name into what a person should read.
//
// The labels are GENERATED from the seed metric catalogues (app/aws|azure|gcp/metric_catalog_data.py) by
// scripts/generate_metric_labels.py -> metricLabels.generated.js, so a metric added to a catalogue gets a
// proper name (or a failing test) instead of a raw key reaching the UI. Do not hand-type labels here.
import { GENERATED_METRIC_LABELS, GENERATED_PERCENT_METRICS } from "./metricLabels.generated.js";

const PERCENT = new Set(GENERATED_PERCENT_METRICS);
// per-mount disk series published by the CloudWatch agent: disk_used_percent__var_lib_mysql
const MOUNT = /^(disk_used_percent)__(.+)$/;

const ACRONYMS = { cpu: "CPU", io: "I/O", iops: "IOPS", http: "HTTP", https: "HTTPS", db: "DB", dns: "DNS",
                   api: "API", ip: "IP", id: "ID", vm: "VM", os: "OS", sql: "SQL", url: "URL" };
const LOWER = new Set(["per", "of", "to", "and", "for", "by", "from"]);
const TOKEN = /[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+[0-9]*|[0-9]+[A-Z]*(?![a-z])|[0-9]+[a-z]*/g;

// Only for names that are in NO catalogue (e.g. metrics discovered at runtime): split on separators and
// camel case and title-case the words. Names stored lower-case with no separators cannot be split.
function fallbackLabel(raw) {
  const words = [];
  raw.replace(/[/_.-]/g, " ").split(/\s+/).filter(Boolean).forEach((part) => {
    (part.match(TOKEN) || [part]).forEach((tok) => words.push(tok));
  });
  return words.map((tok, i) => {
    const low = tok.toLowerCase();
    if (ACRONYMS[low]) return ACRONYMS[low];
    if (/^[0-9]+[A-Za-z]*$/.test(tok)) return low;
    if (i > 0 && LOWER.has(low)) return low;
    return tok.charAt(0).toUpperCase() + tok.slice(1).toLowerCase();
  }).join(" ");
}

/** "disk_used_percent" -> "Disk Utilization";  "disk_used_percent__var_lib" -> "Disk Utilization (/var/lib)" */
export function metricLabel(name) {
  if (!name) return "";
  const raw = String(name);
  const lower = raw.toLowerCase();
  if (GENERATED_METRIC_LABELS[lower]) return GENERATED_METRIC_LABELS[lower];
  const m = lower.match(MOUNT);
  if (m) return `${GENERATED_METRIC_LABELS[m[1]]} (/${m[2].replace(/_/g, "/")})`;
  return fallbackLabel(raw);
}

/** "%" for metrics whose catalogue unit is Percent (and per-mount disk series), otherwise "". */
export function metricUnit(name) {
  const n = String(name || "").toLowerCase();
  return PERCENT.has(n) || MOUNT.test(n) ? "%" : "";
}

/** Compact, unit-aware number: 95.13 -> "95.13%" for CPU, 12100 -> "12.1K" for ops. */
export function formatMetricValue(name, v) {
  if (v == null || v === "") return "—";
  const n = parseFloat(v);
  if (Number.isNaN(n)) return String(v);
  const abs = Math.abs(n);
  let out;
  if (abs >= 1e12)      out = (n / 1e12).toFixed(2) + "T";
  else if (abs >= 1e9)  out = (n / 1e9).toFixed(2) + "G";
  else if (abs >= 1e6)  out = (n / 1e6).toFixed(2) + "M";
  else if (abs >= 1e4)  out = (n / 1e3).toFixed(1) + "K";
  else if (abs !== 0 && abs < 0.1) out = n.toPrecision(2);
  else out = n % 1 === 0 ? String(n) : n.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
  return out + metricUnit(name);
}
