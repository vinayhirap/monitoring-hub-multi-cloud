// src/api/httpDefaults.js
/**
 * The entire frontend talks to one backend over relative paths — Vite
 * proxies /api in dev, Nginx proxies /api in prod. That backend
 * authenticates via an httpOnly session cookie (see Phase 0 on the
 * backend), which only gets attached to a request if it opts in with
 * `credentials: "include"`.
 *
 * There are 30+ fetch() call sites spread across a dozen page files.
 * Rather than trust every one of them to remember that option
 * individually — exactly the kind of thing that's easy to miss on one
 * page and silently leave just that page unauthenticated — this
 * patches fetch once, globally, at app startup. Import this file for
 * its side effect only, before anything else runs (see main.jsx).
 */
const nativeFetch = window.fetch.bind(window);

// SECURITY: only attach credentials:"include" to same-origin requests.
// This patch exists so none of the 30+ fetch() call sites across the
// app have to remember `credentials: "include"` individually for our
// OWN /api/* calls -- it was never meant to apply to a request to
// somewhere else. Without this check, the day any call site fetches a
// third-party or absolute cross-origin URL (an analytics endpoint, a
// CDN health-check, anything), this patch would silently attach the
// httpOnly session cookie to that other origin too. Every current call
// site happens to be same-origin/relative, so this has been a latent
// gap rather than an active leak -- guarding it here removes the trap
// for whatever gets added next.
function isSameOrigin(input) {
  try {
    const url = typeof input === "string" || input instanceof URL
      ? new URL(input, window.location.origin)
      : new URL(input.url, window.location.origin);
    return url.origin === window.location.origin;
  } catch {
    // Unparseable input -- let the native fetch surface its own error
    // rather than guessing; don't add credentials to something we
    // couldn't identify the origin of.
    return false;
  }
}

window.fetch = (input, init = {}) => {
  if (!isSameOrigin(input)) return nativeFetch(input, init);
  return nativeFetch(input, { credentials: "include", ...init });
};
