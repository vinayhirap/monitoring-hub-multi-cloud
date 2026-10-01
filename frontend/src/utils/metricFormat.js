// utils/metricFormat.js -- ONE formatter for every metric value / axis tick /
// period label, keyed by the CloudWatch-style unit string the backend
// (app/metric_display.py) sends. Unknown units fall through to a plain number
// so a new cloud or metric can never throw here.
const trim = (v, d = 2) => Number(Number(v).toFixed(d)).toString();

const SI = ["", "k", "M", "G", "T"];
function si(v, d = 2) {
  let i = 0, x = Math.abs(v);
  while (x >= 1000 && i < SI.length - 1) { x /= 1000; i++; }
  return `${v < 0 ? "-" : ""}${trim(x, d)}${SI[i]}`;
}

export function fmtSeconds(v) {
  const a = Math.abs(v);
  if (a === 0) return "0 s";
  if (a < 0.001) return `${trim(v * 1e6, 1)} µs`;
  if (a < 1) return `${trim(v * 1000, 1)} ms`;
  if (a < 120) return `${trim(v, 2)} s`;
  return `${trim(v / 60, 2)} min`;
}

export function fmtMetricValue(v, unit, symbol) {
  if (v == null || Number.isNaN(v)) return "—";
  switch (unit) {
    case "Percent":       return `${trim(v, 2)}%`;
    case "Seconds":       return fmtSeconds(v);
    case "Milliseconds":  return Math.abs(v) >= 1000 ? `${trim(v / 1000, 2)} s` : `${trim(v, 2)} ms`;
    case "Microseconds":  return Math.abs(v) >= 1000 ? `${trim(v / 1000, 2)} ms` : `${trim(v, 1)} µs`;
    case "Bytes":         return `${si(v)}B`;
    case "Bytes/Second":  return `${si(v)}B/s`;
    case "KiB/s":         return `${trim(v, 2)} KiB/s`;
    case "Count/Second":  return `${trim(v, 2)} /s`;
    case "Count":
    case "None":          return si(v);
    default:              return `${Math.abs(v) >= 1000 ? si(v) : trim(v, 2)}${symbol ? ` ${symbol}` : ""}`;
  }
}

export function fmtAxisValue(v, unit) {
  if (v == null || Number.isNaN(v)) return "";
  if (unit === "Seconds") return fmtSeconds(v);
  if (unit === "Percent") return `${trim(v, 1)}`;
  if (Math.abs(v) >= 1000) return si(v, 1);
  return trim(v, Math.abs(v) < 10 ? 2 : 1);
}

export function fmtPeriod(secs) {
  if (!secs) return null;
  if (secs % 86400 === 0) return `${secs / 86400} day${secs > 86400 ? "s" : ""}`;
  if (secs % 3600 === 0) return `${secs / 3600} hr`;
  if (secs % 60 === 0) return `${secs / 60} min`;
  return `${secs} s`;
}

// Axis tick label: time only for <=1D windows, date + time beyond (the old
// HH:MM-only ticks repeated "00:15, 15:15, 06:10..." across a 1W-1Y axis).
export function makeTickFormatter(windowHours, ianaName) {
  const dateAndTime = windowHours > 36;
  return (ms) => {
    const d = new Date(ms);
    const opts = dateAndTime
      ? { month: "short", day: "numeric", ...(windowHours <= 24 * 10 ? { hour: "2-digit", minute: "2-digit", hour12: false } : {}), timeZone: ianaName }
      : { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ianaName };
    return d.toLocaleString("en-US", opts);
  };
}

export const fmtFullTime = (ms, ianaName) =>
  new Date(ms).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ianaName });

export const STAT_FIELD = { Average: "a", Minimum: "mn", Maximum: "mx", Sum: "s", SampleCount: "n" };
