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

window.fetch = (input, init = {}) => {
  return nativeFetch(input, { credentials: "include", ...init });
};
