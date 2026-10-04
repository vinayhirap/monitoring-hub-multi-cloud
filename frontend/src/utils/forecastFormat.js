// Capacity-forecast wording (audit F5/H4). The forecast is a straight-line fit, so "283.7 days" and "~283.7 days"
// claim far more precision than exists. Round to what the method can support, and say "over a year" past that.
export function roundDays(d) {
  if (d === null || d === undefined || d === "") return null;      // Number(null) is 0: a missing value is not "under a day"
  const n = Number(d);
  if (!Number.isFinite(n) || n < 0) return null;
  if (n < 1) return 0;
  if (n < 30) return Math.round(n);               // near term: whole days
  if (n < 100) return Math.round(n / 5) * 5;      // 30-99: nearest 5
  if (n < 365) return Math.round(n / 10) * 10;    // 100-364: nearest 10
  return Infinity;                                // 1 year or more
}

/** "11 days", "about 280 days", "under a day", "over a year", or "unknown". */
export function formatDaysLeft(d) {
  const r = roundDays(d);
  if (r === null) return "unknown";
  if (r === 0) return "under a day";
  if (r === Infinity) return "over a year";
  const n = Number(d);
  const unit = r === 1 ? "day" : "days";
  return n < 30 ? `${r} ${unit}` : `about ${r} ${unit}`;
}

/** Short badge form: "11d left", "~280d left", "<1d left", "1y+ left". */
export function formatDaysLeftShort(d) {
  const r = roundDays(d);
  if (r === null) return "n/a";
  if (r === 0) return "<1d left";
  if (r === Infinity) return "1y+ left";
  return Number(d) < 30 ? `${r}d left` : `~${r}d left`;
}
