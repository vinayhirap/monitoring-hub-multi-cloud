// "Firing by priority" strip (phase 4b). Renders nothing until PRIORITY_ENABLED is on server-side,
// so deploying the UI changes nothing on screen. Clicking a level calls onPick(level) when given.
import { useCallback, useEffect, useState } from "react";
import { useAlertSync } from "../hooks/useAlertSync";

export default function PriorityStrip({ accountId = "", active = "", onPick = null, title = "Firing by priority" }) {
  const [data, setData] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api/alerts/priority-summary${accountId ? `?account_id=${accountId}` : ""}`,
        { credentials: "include" });
      if (res.ok) setData(await res.json());
    } catch {
      // keep the last-known numbers
    }
  }, [accountId]);

  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load]);
  useAlertSync(load);

  if (!data || !data.enabled) return null;
  return (
    <div className="prio-strip" role="group" aria-label={title}>
      <span className="prio-strip-title">{title}</span>
      {[1, 2, 3, 4].map(p => {
        const n = data.counts?.[String(p)] ?? 0;
        const cls = `prio-card prio-p${p}${n ? " prio-has" : ""}${String(active) === String(p) ? " prio-on" : ""}`;
        return onPick ? (
          <button key={p} type="button" className={cls} onClick={() => onPick(String(active) === String(p) ? "" : String(p))}
                  title={`Show only P${p} alerts`}>
            <span className="prio-card-label">P{p}</span><span className="prio-card-n">{n}</span>
          </button>
        ) : (
          <span key={p} className={cls}><span className="prio-card-label">P{p}</span><span className="prio-card-n">{n}</span></span>
        );
      })}
    </div>
  );
}
