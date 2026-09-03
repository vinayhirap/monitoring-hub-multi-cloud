// src/utils/dataCache.js
/**
 * Lightweight stale-while-revalidate cache for the AWS-data list/dashboard
 * fetches that show a full-page "Fetching live AWS data…" (or equivalent)
 * placeholder on every load or reload — Overview's account+alert list,
 * AccountDetail's EC2 instance list, ServiceDetail's per-service resource
 * list. Backed by localStorage so it survives a full page reload, not
 * just client-side navigation.
 *
 * Pattern: on mount (or when the page's key params change — account id,
 * service), read whatever's cached and render it immediately with no
 * spinner, while a fresh fetch runs in the background; when the fresh
 * fetch resolves, swap it in and refresh the cache. On a genuine
 * first-ever visit (nothing cached yet) the page still shows its normal
 * loading state — there's nothing to show early in that case.
 *
 * Deliberately NOT used for per-instance CloudWatch chart time-series
 * fetches (MetricChart data) — those depend on a specific resource +
 * time-range selection, change constantly, and are already fast; the
 * caching payoff there is much lower and the key space far larger. This
 * only covers the slow "list of things" fetches that reliably show the
 * same shape of data on every reload.
 */
const PREFIX = "mh_cache:";
const MAX_AGE_MS = 24 * 60 * 60 * 1000; // ignore anything older than this rather than show ancient data

export function getCached(key) {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.ts !== "number") return null;
    if (Date.now() - parsed.ts > MAX_AGE_MS) {
      localStorage.removeItem(PREFIX + key);
      return null;
    }
    return parsed; // { data, ts }
  } catch {
    return null;
  }
}

export function setCached(key, data) {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify({ data, ts: Date.now() }));
  } catch {
    // localStorage full, disabled, or unavailable (private browsing) —
    // caching is a nice-to-have here, never something a live fetch
    // should fail or block on.
  }
}

export function clearCached(key) {
  try {
    localStorage.removeItem(PREFIX + key);
  } catch {
    // same as above — never let cache cleanup throw
  }
}
