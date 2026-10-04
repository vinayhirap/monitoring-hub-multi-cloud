// src/hooks/useResourceAlerts.js
// Per-resource alert state for one account (optionally one service), from
// GET /api/alerts/by-resource -- the same rollup that backs the Overview
// banner, the Services tiles and the Alerts tabs, so a row badge can never
// disagree with them. Shared by EVERY resource table (EC2, EBS, RDS, Lambda,
// S3, ELB, ECS and the generic extended/directory pages).
import { useEffect, useState, useCallback, useRef } from "react";
import { getAlertsByResource } from "../api/api";
import { useAlertSync } from "./useAlertSync";
import { visibleInterval } from "../utils/poll";

// Safety net only: changes arrive instantly through useAlertSync.
const POLL_MS = 15000;

export function useResourceAlerts(accountId, service) {
  const [byResource, setByResource] = useState({});

  const loadRef = useRef(() => {});

  useEffect(() => {
    if (accountId == null) return undefined;
    let cancelled = false;
    const load = () =>
      getAlertsByResource(accountId, service)
        .then(d => { if (!cancelled && d && typeof d === "object") setByResource(d); })
        .catch(() => {});   // keep last-known badges on a transient failure
    loadRef.current = load;
    load();
    const stop = visibleInterval(load, POLL_MS);          // audit C4: no background-tab polling
    return () => { cancelled = true; stop(); loadRef.current = () => {}; };
  }, [accountId, service]);

  // refetch in the same tick as every other page when alert state changes
  useAlertSync(() => loadRef.current(), { enabled: accountId != null });

  // Resource ids differ per service (instance_id, volume_id, bucket name,
  // ARN...). Callers pass every identifier a row has; first hit wins.
  const lookup = useCallback((...ids) => {
    for (const id of ids) {
      if (id && byResource[id]) return byResource[id];
    }
    return null;
  }, [byResource]);

  return { byResource, lookup };
}
