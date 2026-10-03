// Console links come back from the API (federation sign-in URLs built server-side).
// They are opened with window.open / tab.location.href, where a `javascript:` or
// `data:` URL would run in this origin (audit E6/E9, defence in depth). Only an
// absolute http(s) URL is ever navigated to.

/** True for an absolute http(s) URL with a host. */
export function isSafeHttpUrl(value) {
  if (typeof value !== "string" || value.length > 8192) return false;
  let u;
  try { u = new URL(value); } catch { return false; }
  return (u.protocol === "https:" || u.protocol === "http:") && !!u.hostname;
}

/** Returns the URL if safe, otherwise throws (callers already handle failures). */
export function assertHttpUrl(value) {
  if (!isSafeHttpUrl(value)) throw new Error("The cloud console returned an unexpected link");
  return value;
}
