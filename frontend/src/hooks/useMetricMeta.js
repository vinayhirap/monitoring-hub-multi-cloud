// hooks/useMetricMeta.js -- chart metadata (titles, units, stats, polling
// cadence, CURRENT threshold lines, open alerts) for one resource. Works for
// every cloud/service (GET /api/live/metric-meta). Callers invoke `reload`
// from their refresh tick so threshold edits / moving dynamic bands show up
// without a page reload. A failure keeps the last good meta (overlay only).
import { useCallback, useEffect, useRef, useState } from "react";
import { getMetricMeta } from "../api/api";

export function useMetricMeta(accountId, service, resourceIds, enabled = true) {
  const [state, setState] = useState({ metrics: {}, alerts_unmatched: [], retention_days: null });
  const key = `${accountId}|${service}|${(resourceIds || []).join(",")}`;
  const keyRef = useRef(key);
  keyRef.current = key;

  const reload = useCallback(() => {
    if (!enabled || accountId == null || !service || !(resourceIds || []).length) return Promise.resolve();
    const myKey = keyRef.current;
    return getMetricMeta(accountId, service, resourceIds)
      .then(d => { if (keyRef.current === myKey && d && d.metrics) setState(d); })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled]);

  useEffect(() => { setState({ metrics: {}, alerts_unmatched: [], retention_days: null }); reload(); }, [reload]);
  return { ...state, reload };
}
