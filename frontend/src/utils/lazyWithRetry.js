// Route-level code splitting (audit G1: one ~1.2 MB bundle for every page). React.lazy alone has a deployment trap:
// pages are separate hashed files, so a tab opened BEFORE a release asks for a file that no longer exists and the
// page dies with "Failed to fetch dynamically imported module". lazyWithRetry reloads once to pick up the new
// index.html, then (if it still fails) lets the error reach the ErrorBoundary instead of looping.
import { lazy } from "react";

const FLAG = "mh:chunk-reload";
const CHUNK_ERROR = /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module|ChunkLoadError|Loading chunk \S+ failed/i;

export function isChunkError(err) {
  return CHUNK_ERROR.test(String(err && err.message ? err.message : err));
}

/** Pure core, injectable for tests. Resolves to the module, reloads once on a stale-chunk error, else rethrows. */
export function loadWithRetry(factory, storage, reload) {
  return factory().then(
    mod => { try { storage?.removeItem(FLAG); } catch { /* storage may be blocked */ } return mod; },
    err => {
      let already = true;
      try { already = storage?.getItem(FLAG) === "1"; } catch { /* treat as already tried: never loop */ }
      if (isChunkError(err) && !already) {
        try { storage.setItem(FLAG, "1"); } catch { return Promise.reject(err); }
        reload();
        return new Promise(() => {});          // the page is reloading; never resolve
      }
      return Promise.reject(err);
    },
  );
}

export function lazyWithRetry(factory) {
  return lazy(() => {
    let storage = null;
    try { storage = window.sessionStorage; } catch { /* blocked */ }
    return loadWithRetry(factory, storage, () => window.location.reload());
  });
}
