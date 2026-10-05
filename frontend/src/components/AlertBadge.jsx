// src/components/AlertBadge.jsx
// The one CRITICAL / WARNING badge used on every resource row. `info` is an
// entry of GET /api/alerts/by-resource. Only FIRING alerts paint red/amber;
// a resource whose only alerts are stale, acknowledged or suppressed gets a
// neutral chip instead, so "no data" never looks like "on fire" nor "fine".
import { RedDotIcon, AlertTriangleIcon } from "./icons";
import { plural } from "../utils/plural";

const STYLES = {
  // status tokens (tokens.css) so the chips stay readable in the light theme too
  CRITICAL: { bg: "var(--crit-bg)", fg: "var(--crit-fg)", bd: "var(--crit-line)" },
  WARNING:  { bg: "var(--warn-bg)", fg: "var(--warn-fg)", bd: "var(--warn-line)" },
  INFO:     { bg: "var(--info-bg)", fg: "var(--info-fg)", bd: "var(--info-line)" },
  NEUTRAL:  { bg: "var(--mute-bg)", fg: "var(--mute-fg)", bd: "var(--mute-line)" },
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
  if (info.stale) return chip("NEUTRAL", "NO DATA", `${info.stale} ${info.stale === 1 ? "alert has" : "alerts have"} had no fresh reading: state unknown`);
  if (info.acknowledged) return chip("NEUTRAL", "ACK", `${plural(info.acknowledged, "acknowledged alert")}`);
  if (info.suppressed) return chip("NEUTRAL", "MUTED", `${info.suppressed} muted / in maintenance`);
  return null;
}
