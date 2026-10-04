// "Set up CloudOps" card on Overview (audit B9). Built-but-empty features (status page, SLOs, uptime checks,
// escalation, notification channels) used to give an administrator no hint that they existed. Shown to admins only,
// until every step is done or it is dismissed (remembered in this browser).
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { getSetupStatus } from "../api/api";
import { buildChecklist } from "../utils/setupChecklist";
import { Panel } from "./ui";

const KEY = "mh:setup-checklist-dismissed";
const read = () => { try { return localStorage.getItem(KEY) === "1"; } catch { return false; } };

export default function FirstRunChecklist() {
  const { hasPermission, user } = useAuth();
  const [status, setStatus] = useState(null);
  const [hidden, setHidden] = useState(read);

  useEffect(() => {
    if (hidden || String(user?.role || "").toLowerCase() !== "admin") return;
    let alive = true;
    getSetupStatus().then(s => { if (alive) setStatus(s); }).catch(() => {});
    return () => { alive = false; };
  }, [hidden, user?.role]);

  const list = buildChecklist(status, hasPermission, user?.role);
  if (hidden || !list.show) return null;

  function dismiss() {
    try { localStorage.setItem(KEY, "1"); } catch { /* private mode: just hide for this view */ }
    setHidden(true);
  }

  return (
    <Panel title="Finish setting up CloudOps"
           subtitle={`${list.done} of ${list.total} steps done. These features are built but empty until you configure them.`}
           actions={<button type="button" className="ui-btn" onClick={dismiss}>Dismiss</button>}>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
        {list.remaining.map(step => (
          <li key={step.key} style={{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
            <Link to={step.to} className="ov-link" style={{ fontWeight: 600 }}>{step.title} →</Link>
            <span className="ui-sub">{step.hint}</span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}
