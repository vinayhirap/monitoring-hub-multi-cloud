// components/MetricChartCard.jsx
//
// THE chart card for every metric on every page (bespoke EC2/EBS/RDS/Lambda/
// S3/ELB/ECS and the generic extended/Azure/GCP page). One component, driven
// by backend metadata (app/metric_meta.py) so a new cloud/service/metric gets
// the same behaviour with zero per-metric code:
//   * AWS-console title + CloudWatch unit/format, statistic selector
//     (Average/Minimum/Maximum/Sum/SampleCount where mathematically valid)
//   * REAL polling cadence badge (from app/collector/polling_model.py)
//   * warning/critical lines = the CURRENT effective thresholds (static,
//     dynamic baseline or anomaly), refreshed with the data
//   * metric in alert -> coloured border + CRITICAL/WARNING badge
//   * fixed time window (not dataMin..dataMax), date-aware ticks, gaps shown
//     as gaps, linear lines (no invented overshoot), 0-based axis
import { createContext, useContext, useMemo, useState } from "react";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";
import { useTimezone } from "../contexts/TimezoneContext";
import { Maximize2Icon } from "./icons";
import MetricZoomModal from "./MetricZoomModal";
import { fmtMetricValue, fmtAxisValue, fmtPeriod, makeTickFormatter, fmtFullTime, STAT_FIELD } from "../utils/metricFormat";
import "./MetricChartCard.css";

// value: { meta: {metricName: entry}, windowHours, bucketSecs, statOverride }
export const MetricPanelContext = createContext({ meta: {}, windowHours: 6, bucketSecs: null, statOverride: "auto" });
export const metricAnchor = (name) => `mc-${String(name).replace(/[^a-zA-Z0-9_-]/g, "_")}`;

const SEV_COLOR = { CRITICAL: "#ef4444", WARNING: "#f59e0b", INFO: "#38bdf8" };

export default function MetricChartCard({
  title, metricKey, data: rawData, color = "#2bb3ac", unit = "", description, emptyReason, timeRange,
  warningThreshold, criticalThreshold, valueFormatter, yTickFormatter,
  // forceTitle: page-supplied title that wins over the catalog title (e.g. the
  //   Windows counter name "LogicalDisk % Free Space" from the CW agent).
  // invert: show 100 - value (Windows "% Free Space" is stored as used %).
  //   Min/Max swap, and threshold lines are dropped because thresholds are
  //   defined on the stored used-% scale, not on the inverted one.
  forceTitle, invert = false,
}) {
  const { ianaName } = useTimezone();
  const ctx = useContext(MetricPanelContext);
  const [localStat, setLocalStat] = useState(null);
  const [zoomOpen, setZoomOpen] = useState(false);

  const windowHours = ctx.windowHours || 6;
  const tickFmt = useMemo(() => makeTickFormatter(windowHours, ianaName), [windowHours, ianaName]);

  // data === null: backend says this metric can structurally never have data -> hide
  if (rawData === null) return null;
  const inv = (x) => (x == null ? x : Math.round((100 - x) * 1e6) / 1e6);
  const data = invert && Array.isArray(rawData)
    ? rawData.map(d => ({ ...d, v: inv(d.v), a: inv(d.a), mn: inv(d.mx), mx: inv(d.mn) }))
    : rawData;

  const meta = ctx.meta?.[metricKey || title] || null;
  const shownTitle = forceTitle || meta?.title || title;
  const cwUnit = meta?.unit || null;
  const stats = meta?.stats || [];
  const nativeStat = meta?.native_stat || "Average";
  const wanted = localStat || (ctx.statOverride !== "auto" ? ctx.statOverride : null);
  const stat = wanted && stats.includes(wanted) ? wanted : (stats.includes(nativeStat) ? nativeStat : (stats[0] || null));
  const field = stat ? STAT_FIELD[stat] : "v";

  const th = invert ? null : (meta?.threshold || null);
  const warnLine = invert ? null : (th ? th.warning : warningThreshold);
  const critLine = invert ? null : (th ? th.critical : criticalThreshold);
  const alert = meta?.alert && ["firing"].includes(meta.alert.state) ? meta.alert : null;
  const sevColor = alert ? SEV_COLOR[alert.severity] || SEV_COLOR.INFO : null;

  const fmt = (v) => (valueFormatter && !cwUnit ? valueFormatter(v) : fmtMetricValue(v, cwUnit || undefined, unit));
  const fmtTick = (v) => (yTickFormatter && !cwUnit ? yTickFormatter(v) : fmtAxisValue(v, cwUnit));

  const bucket = ctx.bucketSecs;
  const periodLabel = fmtPeriod(bucket || meta?.period_seconds);
  const pollTip = meta?.poll_label
    ? `Collected every ${meta.poll_label}` +
      (meta.period_seconds ? ` · CloudWatch period ${fmtPeriod(meta.period_seconds)}` : "") +
      (meta.tier ? ` · tier: ${meta.tier}` : "") + (meta.poll_source ? `\nSource: ${meta.poll_source}` : "")
    : null;

  const header = (extra) => (
    <div className="chart-header mc-header">
      <span className="mc-titles">
        <span className="chart-title" title={description || meta?.description || shownTitle}>{shownTitle}</span>
        {meta && meta.metric_name !== shownTitle && <span className="mc-sub mono">{meta.metric_name}</span>}
      </span>
      {extra}
    </div>
  );
  const badges = (
    <div className="mc-badges">
      {alert && <span className="mc-badge mc-sev" style={{ background: `${sevColor}22`, color: sevColor, borderColor: `${sevColor}66` }}>● {alert.severity} ALERT</span>}
      {meta?.alert && !alert && <span className="mc-badge mc-muted">{meta.alert.state}</span>}
      {meta?.poll_label && <span className="mc-badge mc-poll" title={pollTip}>⏱ polled every {meta.poll_label}</span>}
      {th?.mode && th.mode !== "static" && <span className="mc-badge mc-dyn" title="Threshold follows this resource's own baseline (Settings → Thresholds)">{th.mode} threshold</span>}
    </div>
  );

  if (!data || data.length === 0) {
    return (
      <div id={metricAnchor(metricKey || title)} className={`chart-box mc-card ${alert ? "mc-alert" : ""}`} style={alert ? { borderColor: sevColor } : undefined}>
        {header()}
        {badges}
        <div className="chart-empty">{emptyReason || `No data in last ${timeRange || "6H"}`}{meta?.poll_label ? ` · collected every ${meta.poll_label}` : ""}</div>
      </div>
    );
  }

  // series for the chosen statistic; break the line where data is missing
  const gapMs = Math.max(bucket || 0, meta?.period_seconds || 0, (meta?.poll_seconds || 0) / 2, 60) * 2.5 * 1000;
  const pts = [];
  data.forEach((d, i) => {
    const t = new Date(d.t).getTime();
    const raw = d[field] ?? d.v;
    if (i > 0 && t - pts[pts.length - 1].t > gapMs) pts.push({ t: pts[pts.length - 1].t + gapMs / 2.5, v: null });
    pts.push({ t, v: raw, n: d.n });
  });
  const last = [...data].reverse().find(d => (d[field] ?? d.v) != null);
  const latest = last ? (last[field] ?? last.v) : null;
  const lastT = last ? new Date(last.t).getTime() : null;

  const end = Math.max(Date.now(), lastT || 0);
  const start = end - windowHours * 3600 * 1000;
  const nums = pts.map(p => p.v).filter(v => v != null);
  const lines = [warnLine, critLine].filter(v => v != null);
  const hi = Math.max(...nums, ...lines);
  const lo = Math.min(0, ...nums, ...lines);
  const pad = (hi - lo) * 0.06 || 1;

  return (
    <div id={metricAnchor(metricKey || title)} className={`chart-box mc-card ${alert ? "mc-alert" : ""}`} style={alert ? { borderColor: sevColor, boxShadow: `0 0 0 1px ${sevColor}55` } : undefined}>
      {header(
        <span className="chart-header-right">
          <span className="chart-latest" style={{ color }}>{fmt(latest)}</span>
          <button className="chart-expand-btn" onClick={() => setZoomOpen(true)} title={`Zoom ${shownTitle}`}><Maximize2Icon size={13} /></button>
        </span>
      )}
      {badges}
      {stats.length > 0 && (
        <div className="mc-controls">
          <label>Statistic
            <select value={stat} onChange={e => setLocalStat(e.target.value)} className="mc-select">
              {stats.map(s => <option key={s} value={s}>{s}{s === nativeStat ? " (default)" : ""}</option>)}
            </select>
          </label>
          {periodLabel && <span className="mc-period" title="Each point aggregates this much time (chosen from the time range, like the CloudWatch console)">Period: {periodLabel}</span>}
        </div>
      )}
      <ResponsiveContainer width="100%" height={110}>
        <LineChart data={pts} margin={{ top: 4, right: 6, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="rgba(99,130,190,0.08)" strokeDasharray="3 3" />
          <XAxis dataKey="t" type="number" scale="time" domain={[start, end]} allowDataOverflow tickFormatter={tickFmt}
                 tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false} tickCount={5} />
          <YAxis domain={[lo === 0 ? 0 : lo - pad, hi + pad]} tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false}
                 width={44} tickFormatter={fmtTick} />
          <Tooltip
            contentStyle={{ background: "#0b1220", border: "1px solid rgba(99,130,190,0.2)", borderRadius: 6, fontSize: 11 }}
            labelStyle={{ color: "#7a90b8" }} labelFormatter={(ms) => fmtFullTime(ms, ianaName)}
            formatter={(value) => [fmt(value), `${shownTitle}${stat ? ` · ${stat}` : ""}`]} itemStyle={{ color }}
          />
          {warnLine != null && <ReferenceLineY y={warnLine} color="#f59e0b" dash="4 4" />}
          {critLine != null && <ReferenceLineY y={critLine} color="#ef4444" dash="2 3" />}
          <Line type="linear" dataKey="v" stroke={color} strokeWidth={2} connectNulls={false}
                dot={pts.length <= 24 ? { r: 2, fill: color } : false} activeDot={{ r: 3, fill: color }} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
      {(warnLine != null || critLine != null) && (
        <div className="mc-legend">
          {warnLine != null && <span><i style={{ background: "#f59e0b" }} /> Warn {th?.comparison || ""} {fmt(warnLine)}</span>}
          {critLine != null && <span><i style={{ background: "#ef4444" }} /> Crit {th?.comparison || ""} {fmt(critLine)}</span>}
        </div>
      )}
      <MetricZoomModal open={zoomOpen} onClose={() => setZoomOpen(false)} title={shownTitle}
        data={data.map(d => ({ ...d, v: d[field] ?? d.v }))} unit={unit} color={color} valueFormatter={fmt} />
    </div>
  );
}

// Recharts ReferenceLine wrapper kept local so threshold lines span the full
// window (a Line series only covered the data's own x-range).
import { ReferenceLine } from "recharts";
function ReferenceLineY({ y, color, dash }) {
  return <ReferenceLine y={y} stroke={color} strokeDasharray={dash} strokeWidth={1} ifOverflow="extendDomain" />;
}

// "Metrics in alert" strip: which metrics of THIS resource are firing, with
// click-to-scroll to the chart (or a plain chip when the metric has no chart).
export function AlertedMetricsStrip({ meta, unmatched = [] }) {
  const firing = Object.values(meta || {}).filter(m => m.alert && m.alert.state === "firing");
  const extra = (unmatched || []).filter(u => u.state === "firing");
  if (!firing.length && !extra.length) return null;
  const order = { CRITICAL: 0, WARNING: 1, INFO: 2 };
  firing.sort((a, b) => (order[a.alert.severity] ?? 3) - (order[b.alert.severity] ?? 3));
  return (
    <div className="mc-strip">
      <span className="mc-strip-label">Metrics in alert</span>
      {firing.map(m => (
        <button key={m.metric_name} className="mc-chip" style={{ borderColor: SEV_COLOR[m.alert.severity], color: SEV_COLOR[m.alert.severity] }}
                onClick={() => document.getElementById(metricAnchor(m.metric_name))?.scrollIntoView({ behavior: "smooth", block: "center" })}>
          ● {m.alert.severity} · {m.title}
        </button>
      ))}
      {extra.map(u => (
        <span key={u.metric} className="mc-chip" style={{ borderColor: SEV_COLOR[u.severity], color: SEV_COLOR[u.severity] }} title="No chart for this metric on this page">
          ● {u.severity} · {u.metric}
        </span>
      ))}
    </div>
  );
}
