// src/hooks/useDashboardData.js
// Fetches the extra real data the Overview dashboard needs, on top of /api/live/accounts
// (which Overview already loads). Every call is an existing endpoint; permission failures
// (403) simply leave that section null so the UI hides or labels it, never fakes it.
import { useCallback, useEffect, useRef, useState } from "react";
import { getAlertsList, getFleetSummary, getFleetDetail, getIncidents, getOpEvents } from "../api/api";
import { useAlertSync } from "./useAlertSync";
import { FIRING_LIMIT, RESOLVED_LIMIT } from "../utils/dashboardModel";

const POLL_MS = 30000;
const INCIDENT_ACCOUNT_CAP = 24;       // one request per account x region row, bounded

const ok = r => (r.status === "fulfilled" ? r.value : null);
const asArray = v => (Array.isArray(v) ? v : null);

export function useDashboardData(rowIds) {
  const [data, setData] = useState({ firing: null, resolved: null, incidents: null, events: null, fleet: null, incidentsCapped: false });
  const [fetchedAt, setFetchedAt] = useState(null);
  const [loading, setLoading] = useState(true);
  const idsRef = useRef(rowIds);
  useEffect(() => { idsRef.current = rowIds; }, [rowIds]);

  const load = useCallback(async () => {
    const ids = [...new Set(idsRef.current || [])].slice(0, INCIDENT_ACCOUNT_CAP);
    const [firing, resolved, fsum, fdet, events, ...inc] = await Promise.allSettled([
      getAlertsList("active", FIRING_LIMIT),
      getAlertsList("resolved", RESOLVED_LIMIT),
      getFleetSummary(), getFleetDetail(),
      getOpEvents({ limit: 100 }),
      ...ids.map(id => getIncidents(id, { status: "active", limit: 50 })),
    ]);
    const incOk = inc.map((r, i) => ({ r, id: ids[i] })).filter(x => x.r.status === "fulfilled");
    const next = {
      firing: asArray(ok(firing)), resolved: asArray(ok(resolved)),
      fleet: ok(fsum) ? { summary: ok(fsum), detail: ok(fdet) } : null,
      events: asArray(ok(events)),
      // null (not []) when the caller may not read incidents at all, so the UI can say so
      incidents: ids.length && incOk.length === 0 ? null
        : incOk.flatMap(({ r, id }) => (asArray(r.value) || []).map(x => ({ ...x, account_row_id: id }))),
      incidentsCapped: (idsRef.current || []).length > INCIDENT_ACCOUNT_CAP,
    };
    setData(prev => ({
      ...next,
      // keep last good data for a section whose refresh failed transiently
      firing: next.firing ?? prev.firing, resolved: next.resolved ?? prev.resolved,
    }));
    setFetchedAt(Date.now()); setLoading(false);
  }, []);

  const idKey = (rowIds || []).join(",");
  useEffect(() => {
    if (!idKey) return undefined;                     // nothing to scope until accounts are known
    const first = setTimeout(load, 0);
    const t = setInterval(load, POLL_MS);
    return () => { clearTimeout(first); clearInterval(t); };
  }, [idKey, load]);
  useAlertSync(load);

  return { ...data, fetchedAt, loading, reload: load };
}
