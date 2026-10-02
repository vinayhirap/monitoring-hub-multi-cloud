// hooks/useInsightsRegistry.js -- collects the series each MetricChartCard on a resource panel is drawing,
// so the insights shown next to the charts are computed from exactly the same points.
import { useCallback, useMemo, useRef, useState } from "react";

export function useInsightsRegistry() {
  const map = useRef(new Map());
  const [version, setVersion] = useState(0);
  const report = useCallback((key, p) => {
    const last = p.pts[p.pts.length - 1];
    const sig = `${p.title}|${p.unit}|${p.scope}|${p.pts.length}|${last?.t}|${last?.v}|${p.warn}|${p.crit}|${p.sev}`;
    const prev = map.current.get(key);
    if (prev && prev.sig === sig) return;                   // unchanged: no re-render loop
    map.current.set(key, { ...p, key, sig });
    setVersion(v => v + 1);
  }, []);
  return useMemo(() => ({
    report, version,
    forScope: scope => [...map.current.values()].filter(e => e.scope === scope),
  }), [report, version]);
}
