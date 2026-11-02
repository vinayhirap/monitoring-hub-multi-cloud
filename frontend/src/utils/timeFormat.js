// One timestamp format for the whole app (audit B2). Before this, pages used five formats ("10/3/2026, 21:38",
// "10/3/2026 3:38:07 PM", "03 Oct 2026, 08:04", "29/09/2026", "2026-10-03 21:32:05 IST") and several showed no zone
// at all, so the same event read as two different times and "10/3/2026" was ambiguous for a day-first reader.
//
//   formatStamp(iso, "Asia/Kolkata", "IST")  ->  "03 Oct 2026, 21:38:48 IST"
//   formatDay(iso, "Asia/Kolkata")           ->  "03 Oct 2026"
//   formatShort(iso, "Asia/Kolkata", "IST")  ->  "03 Oct, 21:38 IST"   (tables where the year is obvious)
//
// Day, month name, 24-hour clock, and always the zone label. Anything unparseable returns the fallback.
const PARTS = {
  stamp: { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" },
  short: { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" },
  day:   { day: "2-digit", month: "short", year: "numeric" },
};

function toDate(input) {
  if (input == null || input === "") return null;
  let v = input;
  // A zone-less "YYYY-MM-DD HH:MM:SS" (MySQL) or "YYYY-MM-DDTHH:MM:SS" (Python datetime.isoformat()) is UTC by project
  // convention (see utils/metricFormat). new Date() reads the "T" form as BROWSER-LOCAL time, which showed a 06:03 UTC
  // delivery as "06:03 IST" instead of 11:33 IST, so both forms get an explicit Z here.
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?/.test(v) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(v)) {
    v = v.trim().replace(" ", "T") + "Z";
  }
  const d = v instanceof Date ? v : new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

function render(d, ianaName, kind) {
  const parts = new Intl.DateTimeFormat("en-GB", { ...PARTS[kind], hourCycle: "h23", timeZone: ianaName })
    .formatToParts(d);
  const get = t => parts.find(p => p.type === t)?.value ?? "";
  const date = kind === "short" ? `${get("day")} ${get("month")}` : `${get("day")} ${get("month")} ${get("year")}`;
  if (kind === "day") return date;
  const clock = kind === "stamp" ? `${get("hour")}:${get("minute")}:${get("second")}` : `${get("hour")}:${get("minute")}`;
  return `${date}, ${clock}`;
}

export function formatStamp(input, ianaName = "Asia/Kolkata", tzLabel = "", fallback = "—") {
  const d = toDate(input);
  if (!d) return fallback;
  const s = render(d, ianaName, "stamp");
  return tzLabel ? `${s} ${tzLabel}` : s;
}

export function formatShort(input, ianaName = "Asia/Kolkata", tzLabel = "", fallback = "—") {
  const d = toDate(input);
  if (!d) return fallback;
  const s = render(d, ianaName, "short");
  return tzLabel ? `${s} ${tzLabel}` : s;
}

export function formatDay(input, ianaName = "Asia/Kolkata", fallback = "—") {
  const d = toDate(input);
  return d ? render(d, ianaName, "day") : fallback;
}

/** "Asia/Kolkata" -> "IST", "UTC" -> "UTC": lets a component that only received `ianaName` label its times. */
export function zoneLabel(ianaName) {
  if (ianaName === "Asia/Kolkata") return "IST";
  if (ianaName === "UTC") return "UTC";
  return ianaName || "";
}
