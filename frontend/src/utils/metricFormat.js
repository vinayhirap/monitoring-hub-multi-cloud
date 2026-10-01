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

// Axis tick label. `stepMs` (from timeTicks) decides the shape: day-or-longer
// steps print the date only ("Sep 17"), longer-than-36h windows with shorter
// steps print date + time, everything else prints HH:MM.
export function makeTickFormatter(windowHours, ianaName, stepMs) {
  const dateOnly = stepMs && stepMs >= 86400000;
  const dateAndTime = windowHours > 36;
  return (ms) => {
    const d = new Date(ms);
    const opts = dateOnly
      ? { month: "short", day: "numeric", timeZone: ianaName }
      : dateAndTime
        ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ianaName }
        : { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ianaName };
    return d.toLocaleString("en-US", opts);
  };
}

// ── Round axis ticks (so every chart in a grid lines up like the AWS console) ──
const MIN = 60000, HR = 3600000, DAY = 86400000;
const TIME_STEPS = [5 * MIN, 10 * MIN, 15 * MIN, 30 * MIN, HR, 2 * HR, 3 * HR, 4 * HR, 6 * HR, 12 * HR, DAY, 2 * DAY, 5 * DAY, 7 * DAY];

// UTC offset (ms) of `tz` at instant `ms`; 0 on any failure (never throws).
function tzOffsetMs(ms, tz) {
  try {
    const f = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const p = Object.fromEntries(f.formatToParts(new Date(ms)).map(x => [x.type, x.value]));
    return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - Math.floor(ms / 1000) * 1000;
  } catch { return 0; }
}

// Ticks on round wall-clock times in the viewer's timezone: 10 min for 1H,
// 30 min for 3H, 1 h for 6H, 4 h for 1D, 1 day for 1W, 5 days for 1M.
export function timeTicks(start, end, ianaName, maxTicks = 7) {
  const span = end - start;
  const step = TIME_STEPS.find(s => span / s <= maxTicks) || TIME_STEPS[TIME_STEPS.length - 1];
  const off = tzOffsetMs(end, ianaName);
  const ticks = [];
  for (let t = Math.ceil((start + off) / step) * step - off; t <= end; t += step) ticks.push(t);
  return { ticks, step };
}

// 0-based "nice" y axis: top = 1 / 1.2 / 1.6 / 2 / 2.4 / 3 / 4 / 5 / 6 / 8 / 10 x 10^n,
// 4 equal intervals; Percent never exceeds 100. Returns null when the data has
// negatives / is not finite so the caller keeps its padded auto domain.
const NICE = [1, 1.2, 1.6, 2, 2.4, 3, 4, 5, 6, 8, 10];
export function niceAxis(hi, lo, isPercent) {
  if (!Number.isFinite(hi) || !Number.isFinite(lo) || lo < 0) return null;
  const top = hi > 0 ? hi : 1;
  const pow = Math.pow(10, Math.floor(Math.log10(top)));
  let max = (NICE.find(n => n * pow >= top - 1e-12) || 10) * pow;
  if (isPercent && hi <= 100 && max > 100) max = 100;
  const r = (x) => Number(x.toPrecision(10));
  return { max: r(max), ticks: [0, 1, 2, 3, 4].map(i => r(max * i / 4)) };
}

export const fmtFullTime = (ms, ianaName) =>
  new Date(ms).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ianaName });

export const STAT_FIELD = { Average: "a", Minimum: "mn", Maximum: "mx", Sum: "s", SampleCount: "n" };
