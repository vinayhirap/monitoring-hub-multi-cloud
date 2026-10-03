// utils/loginFlow.js -- pure helpers for the sign-in page. No network, no DOM.

export const FALLBACK_AFTER_LOGIN = "/overview";

/** A post-login return target is only accepted if it is a plain path inside THIS app.
 *  Anything that a browser could resolve to another site (//host, /\host, scheme:, encoded variants)
 *  or that would loop back to the login page is rejected. Returns the path or null. */
export function sanitizeNext(raw) {
  if (typeof raw !== "string") return null;
  const s = raw.trim();
  if (!s || s.length > 2000) return null;
  // eslint-disable-next-line no-control-regex
  const bad = /[\u0000-\u001f\u007f\\]/;                     // control chars and backslashes (browsers treat "\" as "/")
  if (bad.test(s)) return null;
  let decoded;
  try { decoded = decodeURIComponent(s); } catch { return null; }
  if (bad.test(decoded)) return null;
  if (!s.startsWith("/") || s.startsWith("//") || decoded.startsWith("//")) return null;
  if (/^\/+[a-z][a-z0-9+.-]*:/i.test(decoded)) return null;      // "/javascript:..." , "/http://..."
  const path = s.split(/[?#]/)[0];
  if (path === "/login" || path.startsWith("/login/") || path === "/logout") return null;
  return s;
}

/** Retry-After is either delta-seconds or an HTTP date. Returns whole seconds clamped to 1..3600, or null. */
export function parseRetryAfter(h, now = Date.now()) {
  if (h == null || h === "") return null;
  const t = String(h).trim();
  let sec;
  if (/^\d+$/.test(t)) sec = Number(t);
  else { const d = Date.parse(t); if (!Number.isFinite(d)) return null; sec = Math.ceil((d - now) / 1000); }
  if (!Number.isFinite(sec)) return null;
  return Math.min(3600, Math.max(1, sec));
}

export function formatWait(sec) {
  if (sec < 60) return `${sec} second${sec === 1 ? "" : "s"}`;
  const m = Math.ceil(sec / 60);
  return `${m} minute${m === 1 ? "" : "s"}`;
}

/** What went wrong, in words a person can act on. `r` is the login result: { status, retryAfter, network }.
 *  A wrong password gives ONE generic message (no hint whether the username exists). */
export function loginFailure(r = {}) {
  if (r.network) return { kind: "network", fieldsInvalid: false, waitSec: 0, message: "Can't reach the server. Check your connection and try again." };
  const s = Number(r.status);
  if (s === 429) {
    const waitSec = parseRetryAfter(r.retryAfter) || 60;
    return { kind: "rate-limited", fieldsInvalid: false, waitSec, message: `Too many sign-in attempts. Wait about ${formatWait(waitSec)}, then try again.` };
  }
  if (s === 401 || s === 400) return { kind: "credentials", fieldsInvalid: true, waitSec: 0, message: "Invalid username or password." };
  if (s === 403) return { kind: "denied", fieldsInvalid: false, waitSec: 0, message: "This account can't sign in right now. Contact your administrator." };
  if (s >= 500) return { kind: "server", fieldsInvalid: false, waitSec: 0, message: "The server had a problem signing you in. Try again in a moment; if it keeps happening, tell an administrator." };
  return { kind: "unknown", fieldsInvalid: false, waitSec: 0, message: "Sign-in failed. Try again." };
}

/** True when the page is served over plain HTTP from a real host (not localhost). */
export function insecureContext(loc) {
  if (!loc || loc.protocol !== "http:") return false;
  return !["localhost", "127.0.0.1", "[::1]", "::1"].includes(loc.hostname);
}

// A session can end while another request is already navigating to /login (the route guard and the API error handler race),
// so the API handler leaves a short-lived note instead of relying on a query parameter that only one of the two paths adds.
export const EXPIRED_KEY = "mh_session_expired";
const EXPIRED_NOTE_MS = 2 * 60 * 1000;
export function markSessionExpired(storage, now = Date.now()) { try { storage.setItem(EXPIRED_KEY, String(now)); } catch { /* storage unavailable: the ?reason=expired parameter still works */ } }
/** True if the sign-in page should say "your session expired". Consumes the note so it is shown once. */
export function takeSessionExpired(search, storage, now = Date.now()) {
  let flagged = false;
  try {
    const v = Number(storage.getItem(EXPIRED_KEY));
    storage.removeItem(EXPIRED_KEY);
    flagged = Number.isFinite(v) && v > 0 && now - v >= 0 && now - v < EXPIRED_NOTE_MS;
  } catch { /* ignore */ }
  return flagged || new URLSearchParams(search || "").get("reason") === "expired";
}

/** The ONE place a dead session sends the person to the sign-in page (api.js, Alerts, Settings and ServiceDetail each had their own copy).
 *  Records why, and where they were, so signing in again brings them back. */
export function redirectToSignIn(win = window) {
  markSessionExpired(win.sessionStorage);
  if (win.location.pathname === "/login") return;
  const here = win.location.pathname + win.location.search + win.location.hash;
  win.location.href = `/login?reason=expired&next=${encodeURIComponent(here)}`;
}
