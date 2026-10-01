// hooks/useAutoRefresh.js -- run `fn` every `ms` while the tab is visible
// (ms = 0/null -> off). Fires once immediately when the tab becomes visible
// again so a backgrounded tab does not sit on stale charts.
import { useEffect, useRef } from "react";

export function useAutoRefresh(fn, ms, enabled = true) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!enabled || !ms) return undefined;
    const tick = () => { if (!document.hidden) ref.current(); };
    const t = setInterval(tick, ms);
    const vis = () => { if (!document.hidden) ref.current(); };
    document.addEventListener("visibilitychange", vis);
    return () => { clearInterval(t); document.removeEventListener("visibilitychange", vis); };
  }, [ms, enabled]);
}
