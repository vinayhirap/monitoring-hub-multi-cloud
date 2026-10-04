// src/hooks/useDashboardData.js
// Fetches the extra real data the Overview dashboard needs, on top of /api/live/accounts (which Overview already loads).
// Every call is an existing endpoint. Each source is tracked on its own with THREE states, so the UI never confuses them:
//   undefined = still loading (first fetch not back yet)   -> show a skeleton, never a dash or a zero
//   null      = this role may not read it (HTTP 403)        -> show "not available for this role"
//   value     = data. A later transient failure KEEPS the last good value instead of blanking the tile.
// Sources arrive independently (the slow per-account incident lookups no longer hold back the alert numbers).
import { useCallback, useEffect, useRef, useState } from "react";
import { getAlertsList, getFleetSummary, getFleetDetail, getIncidents, getOpEvents } from "../api/api";
import { useAlertSync } from "./useAlertSync";
import { FIRING_LIMIT, RESOLVED_LIMIT } from "../utils/dashboardModel";
import { visibleInterval } from "../utils/poll";

const POLL_MS = 30000;
const INCIDENT_ACCOUNT_CAP = 24;       // one request per account x region row, bounded
const asArray = v => (Array.isArray(v) ? v : null);
const forbidden = e => /\b403\b/.test(String(e?.message || ""));

export function useDashboardData(rowIds) {
  const [data, setData] = useState({ firing: undefined, resolved: undefined, incidents: undefined, events: undefined, fleet: undefined, incidentsCapped: false });
  const [fetchedAt, setFetchedAt] = useState(null);
  const idsRef = useRef(rowIds);
  const inflight = useRef(false), again = useRef(false), loadRef = useRef(null);
  useEffect(() => { idsRef.current = rowIds; }, [rowIds]);

  const load = useCallback(async () => {
    if (inflight.current) { again.current = true; return; }          // one pass at a time; a change during a pass triggers one more
    inflight.current = true;
    const ids = [...new Set(idsRef.current || [])].slice(0, INCIDENT_ACCOUNT_CAP);
    const put = (key, value) => setData(p => ({ ...p, [key]: value }));
    const fail = (key, e) => setData(p => (forbidden(e) ? { ...p, [key]: null } : p[key] === undefined ? { ...p, [key]: null } : p));   // 403 = not allowed; a first-ever failure = unavailable; a later blip keeps the last good value
    const src = (key, promise, map = asArray) => promise.then(v => put(key, map(v)), e => fail(key, e));
    await Promise.all([
      src("firing", getAlertsList("active", FIRING_LIMIT)),
      src("resolved", getAlertsList("resolved", RESOLVED_LIMIT)),
      src("events", getOpEvents({ limit: 100 })),
      Promise.allSettled([getFleetSummary(), getFleetDetail()]).then(([s, d]) => {
        if (s.status === "fulfilled" && s.value) put("fleet", { summary: s.value, detail: d.status === "fulfilled" ? d.value : null });
        else fail("fleet", s.reason);
      }),
      Promise.allSettled(ids.map(id => getIncidents(id, { status: "active", limit: 50 }))).then(res => {
        const okRows = res.map((r, i) => ({ r, id: ids[i] })).filter(x => x.r.status === "fulfilled");
        if (ids.length && okRows.length === 0) { fail("incidents", res.find(r => r.status === "rejected")?.reason); return; }
        put("incidents", okRows.flatMap(({ r, id }) => (asArray(r.value) || []).map(x => ({ ...x, account_row_id: id }))));
      }),
    ]);
    setData(p => ({ ...p, incidentsCapped: (idsRef.current || []).length > INCIDENT_ACCOUNT_CAP }));
    setFetchedAt(Date.now());
    inflight.current = false;
    if (again.current) { again.current = false; loadRef.current?.(); }
  }, []);
  useEffect(() => { loadRef.current = load; }, [load]);

  const idKey = (rowIds || []).join(",");
  useEffect(() => {
    if (!idKey) return undefined;                     // nothing to scope until accounts are known
    const first = setTimeout(load, 0);
    const stop = visibleInterval(load, POLL_MS);          // audit C4: no background-tab polling
    return () => { clearTimeout(first); stop(); };
  }, [idKey, load]);
  useAlertSync(load);

  const ready = ["firing", "resolved", "incidents", "events", "fleet"].every(k => data[k] !== undefined);
  return { ...data, fetchedAt, ready, loading: !ready, reload: load };
}
