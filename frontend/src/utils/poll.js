// Polling helpers (audit C4). Every open tab polls independently, so load grows with the number of open tabs.
//   visibleInterval(fn, ms)  like setInterval, but silent while the tab is hidden, and runs once on return so the
//                            data is fresh the moment someone looks again.
//   backoffSkips(failures)   after consecutive failures, skip 1, 3, 7 (max) ticks before retrying instead of
//                            hammering a struggling server every few seconds.

export function backoffSkips(failures) {
  return failures <= 0 ? 0 : Math.min(2 ** failures - 1, 7);
}

export function visibleInterval(fn, ms, doc = globalThis.document, setI = setInterval, clearI = clearInterval) {
  const tick = () => { if (!doc || !doc.hidden) fn(); };
  const timer = setI(tick, ms);
  const onVisible = () => { if (doc && !doc.hidden) fn(); };
  if (doc && doc.addEventListener) doc.addEventListener("visibilitychange", onVisible);
  return () => {
    clearI(timer);
    if (doc && doc.removeEventListener) doc.removeEventListener("visibilitychange", onVisible);
  };
}
