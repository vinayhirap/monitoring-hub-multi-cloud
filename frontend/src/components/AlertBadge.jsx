// src/components/AlertBadge.jsx
// The one CRITICAL / WARNING badge used on every resource row. `info` is an
// entry of GET /api/alerts/by-resource. Only FIRING alerts paint red/amber;
// a resource whose only alerts are stale, acknowledged or suppressed gets a
// neutral chip instead, so "no data" never looks like "on fire" nor "fine".
import { RedDotIcon, AlertTriangleIcon } from "./icons";

const STYLES = {
  CRITICAL: { bg: "rgba(239,68,68,0.15)",  fg: "#ef4444", bd: "rgba(239,68,68,0.3)" },
  WARNING:  { bg: "rgba(245,158,11,0.15)", fg: "#f59e0b", bd: "rgba(245,158,11,0.3)" },
  INFO:     { bg: "rgba(59,130,246,0.15)", fg: "#60a5fa", bd: "rgba(59,130,246,0.3)" },
  NEUTRAL:  { bg: "rgba(148,163,184,0.12)", fg: "#94a3b8", bd: "rgba(148,163,184,0.3)" },
};

function chip(kind, children, title) {
  const s = STYLES[kind];
  return (
    <span title={title} style={{
      fontSize: 9, fontWeight: 700, padding: "1px 5px", borderRadius: 4,
      background: s.bg, color: s.fg, border: `1px solid ${s.bd}`,
      fontFamily: "var(--font-mono)", display: "inline-flex", alignItems: "center", gap: 3,
      whiteSpace: "nowrap",
    }}>{children}</span>
  );
}

export default function AlertBadge({ info }) {
  if (!info) return null;
  if (info.worst) {
    const n = info.total > 1 ? ` ×${info.total}` : "";
    const title = `${info.critical} critical, ${info.warning} warning${info.info ? `, ${info.info} info` : ""} firing`;
    return chip(info.worst, <>
      {info.worst === "CRITICAL" ? <RedDotIcon size={9} /> : <AlertTriangleIcon size={9} />}
      {info.worst}{n}
    </>, title);
  }
  if (info.stale) return chip("NEUTRAL", "NO DATA", `${info.stale} alert(s) have had no fresh reading -- state unknown`);
  if (info.acknowledged) return chip("NEUTRAL", "ACK", `${info.acknowledged} acknowledged alert(s)`);
  if (info.suppressed) return chip("NEUTRAL", "MUTED", `${info.suppressed} muted / in maintenance`);
  return null;
}
