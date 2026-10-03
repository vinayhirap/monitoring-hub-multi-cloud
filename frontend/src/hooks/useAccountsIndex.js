// src/hooks/useAccountsIndex.js
// Account x region rows for the shell (sidebar tree, scope switcher, palette).
// Reads Overview's SWR cache first (no extra request on the common path) and
// fetches /api/live/accounts only if nothing usable is cached. A 403/empty
// result simply yields [] so RBAC-limited users see no Infrastructure tree.
import { useCallback, useEffect, useState } from "react";
import { getLiveAccounts } from "../api/api";
import { getCached, setCached } from "../utils/dataCache";

const KEY = "overview:accounts";
const FRESH_MS = 5 * 60 * 1000;

function read() {
  const c = getCached(KEY);
  return { rows: Array.isArray(c?.data?.accounts) ? c.data.accounts : [], ts: c?.ts ?? 0 };
}

export function useAccountsIndex(enabled = true) {
  const [rows, setRows] = useState(() => read().rows);
  const refresh = useCallback(async () => {
    try {
      const data = await getLiveAccounts();
      if (Array.isArray(data)) { setCached(KEY, { accounts: data }); setRows(data); }
    } catch { /* RBAC / offline: keep whatever we have */ }
  }, []);
  useEffect(() => {
    if (!enabled) return;
    const first = setTimeout(() => { if (Date.now() - read().ts > FRESH_MS) refresh(); }, 0);
    const t = setInterval(() => setRows(prev => { const r = read().rows; return r.length || !prev.length ? r : prev; }), 30000);
    const onChanged = () => setRows(read().rows);              // Settings > Accounts & Regions removed one
    window.addEventListener("mh:accounts-changed", onChanged);
    return () => { clearTimeout(first); clearInterval(t); window.removeEventListener("mh:accounts-changed", onChanged); };
  }, [enabled, refresh]);
  return rows;
}
