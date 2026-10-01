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
import { createContext, useContext, useState } from "react";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";
import { useTimezone } from "../contexts/TimezoneContext";
import { Maximize2Icon } from "./icons";
import MetricZoomModal from "./MetricZoomModal";
import { fmtMetricValue, fmtAxisValue, fmtPeriod, makeTickFormatter, timeTicks, niceAxis, fmtFullTime, STAT_FIELD } from "../utils/metricFormat";
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
  //   Min/Max swap, and the threshold lines are mirrored (100 - value, "<")
  //   because alerts are defined on the stored used-% scale.
  forceTitle, invert = false,
}) {
  const { ianaName } = useTimezone();
  const ctx = useContext(MetricPanelContext);
  const [localStat, setLocalStat] = useState(null);
  const [zoomOpen, setZoomOpen] = useState(false);

  const windowHours = ctx.windowHours || 6;

  // data === null: backend says this metric can structurally never have data -> hide
  if (rawData === null) return null;
  const inv100 = (x) => (x == null ? x : Math.round((100 - x) * 1e6) / 1e6);
  const data = invert && Array.isArray(rawData)
    ? rawData.map(d => ({ ...d, v: inv100(d.v), a: inv100(d.a), mn: inv100(d.mx), mx: inv100(d.mn) }))
    : rawData;

  const meta = ctx.meta?.[metricKey || title] || null;
  const shownTitle = forceTitle || meta?.title || title;
  const cwUnit = meta?.unit || null;
  const stats = meta?.stats || [];
  const nativeStat = meta?.native_stat || "Average";
  const wanted = localStat || (ctx.statOverride !== "auto" ? ctx.statOverride : null);
  const stat = wanted && stats.includes(wanted) ? wanted : (stats.includes(nativeStat) ? nativeStat : (stats[0] || null));
  const field = stat ? STAT_FIELD[stat] : "v";
  // the page-wide dropdown asked for a statistic this metric cannot honestly offer
  const statNa = !localStat && ctx.statOverride !== "auto" && !stats.includes(ctx.statOverride);

  // Inverted charts (Windows "% Free Space" is stored as used %): the alert is
  // evaluated on the stored used-% scale, so each line is drawn at 100 - value
  // and the comparison flips (used > 80  ==  free < 20).
  const flip = { ">": "<", ">=": "<=", "<": ">", "<=": ">=" };
  const rawTh = meta?.threshold || null;
  const th = rawTh && invert
    ? { ...rawTh, warning: inv100(rawTh.warning), critical: inv100(rawTh.critical), comparison: flip[rawTh.comparison] || rawTh.comparison }
    : rawTh;
  const warnLine = th ? th.warning : (invert ? inv100(warningThreshold) : warningThreshold);
  const critLine = th ? th.critical : (invert ? inv100(criticalThreshold) : criticalThreshold);
  const sameLine = warnLine != null && critLine != null && warnLine === critLine;   // anomaly-only bound
  const alert = meta?.alert && ["firing"].includes(meta.alert.state) ? meta.alert : null;
  const sevColor = alert ? SEV_COLOR[alert.severity] || SEV_COLOR.INFO : null;

  const fmt = (v) => (valueFormatter && !cwUnit ? valueFormatter(v) : fmtMetricValue(v, cwUnit || undefined, unit));
  const fmtTick = (v) => (yTickFormatter && !cwUnit ? yTickFormatter(v) : fmtAxisValue(v, cwUnit));

  const bucket = ctx.bucketSecs;
  // The period of a point can never be finer than the collection period: a 5-min
  // metric charted on 1H still has one point per 5 min (the console says 5 min too).
  const periodLabel = fmtPeriod(Math.max(bucket || 0, meta?.period_seconds || 0) || null);
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
  // every plotted point is ONE stored datapoint (range finer than the collection
  // period): Average = Minimum = Maximum there, so the selector cannot change the
  // chart. It starts to matter on 1W / 1M where a bucket holds several datapoints.
  const singlePt = data.length > 0 && data.every(d => d.n === 1);
  const last = [...data].reverse().find(d => (d[field] ?? d.v) != null);
  const latest = last ? (last[field] ?? last.v) : null;
  const lastT = last ? new Date(last.t).getTime() : null;

  const end = Math.max(Date.now(), lastT || 0);
  const start = end - windowHours * 3600 * 1000;
  const { ticks: xTicks, step: xStep } = timeTicks(start, end, ianaName);
  const tickFmt = makeTickFormatter(windowHours, ianaName, xStep);
  const nums = pts.map(p => p.v).filter(v => v != null);
  const isPct = cwUnit === "Percent" || unit === "%";
  const dataMax = Math.max(0, ...nums);
  // A threshold far above anything the metric does (e.g. the 5 MB anomaly floor on
  // a ~100 kB/5 min network chart) must not squash the real data into a flat line:
  // it stays in the legend ("above chart") but does not stretch the axis. Percent
  // charts keep their lines (70 / 90 % are meaningful even when load is 7 %).
  const isFar = (v) => v != null && !isPct && dataMax > 0 && v > dataMax * 4;
  const lines = [warnLine, critLine].filter(v => v != null && !isFar(v));
  const hi = Math.max(...nums, ...lines);
  const lo = Math.min(0, ...nums, ...lines);
  const axis = niceAxis(hi, lo, isPct);
  const pad = (hi - lo) * 0.06 || 1;
  const yDomain = axis ? [0, axis.max] : [lo === 0 ? 0 : lo - pad, hi + pad];

  return (
    <div id={metricAnchor(metricKey || title)} className={`chart-box mc-card ${alert ? "mc-alert" : ""}`} style={alert ? { borderColor: sevColor, boxShadow: `0 0 0 1px ${sevColor}55` } : undefined}>
      {header(
        <span className="chart-header-right">
          <span className="chart-latest" style={{ color }} title={`Latest ${stat || "value"}${periodLabel ? ` (${periodLabel} period)` : ""}`}>{fmt(latest)}</span>
          <button className="chart-expand-btn" onClick={() => setZoomOpen(true)} title={`Zoom ${shownTitle}`}><Maximize2Icon size={13} /></button>
        </span>
      )}
      {badges}
      {stats.length > 0 && (
        <div className="mc-controls">
          <label>Statistic
            <select value={stat} onChange={e => setLocalStat(e.target.value)} className="mc-select" disabled={singlePt}
                    title={singlePt ? "At this range each point is a single stored datapoint, so Average, Minimum and Maximum are identical. Use 1W or 1M to compare statistics." : undefined}>
              {stats.map(s => <option key={s} value={s}>{s}{s === nativeStat ? " (default)" : ""}</option>)}
            </select>
          </label>
          {statNa && <span className="mc-na" title={`${ctx.statOverride} is not valid for this metric (it is stored as ${nativeStat}), so ${stat} is shown`}>{ctx.statOverride} n/a</span>}
          {periodLabel && <span className="mc-period" title="Each point aggregates this much time (chosen from the time range, like the CloudWatch console)">Period: {periodLabel}</span>}
        </div>
      )}
      <ResponsiveContainer width="100%" height={110}>
        <LineChart data={pts} margin={{ top: 4, right: 6, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="rgba(99,130,190,0.08)" strokeDasharray="3 3" />
          <XAxis dataKey="t" type="number" scale="time" domain={[start, end]} allowDataOverflow tickFormatter={tickFmt}
                 ticks={xTicks} tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false} />
          <YAxis domain={yDomain} ticks={axis ? axis.ticks : undefined} interval={0} tick={{ fontSize: 9, fill: "#3d5070" }} tickLine={false} axisLine={false}
                 width={44} tickFormatter={fmtTick} />
          <Tooltip
            contentStyle={{ background: "#0b1220", border: "1px solid rgba(99,130,190,0.2)", borderRadius: 6, fontSize: 11 }}
            labelStyle={{ color: "#7a90b8" }} labelFormatter={(ms) => fmtFullTime(ms, ianaName)}
            formatter={(value) => [fmt(value), `${shownTitle}${stat ? ` · ${stat}` : ""}`]} itemStyle={{ color }}
          />
          {warnLine != null && !sameLine && !isFar(warnLine) && <ReferenceLineY y={warnLine} color="#f59e0b" dash="4 4" />}
          {critLine != null && !isFar(critLine) && <ReferenceLineY y={critLine} color="#ef4444" dash="2 3" />}
          <Line type="linear" dataKey="v" stroke={color} strokeWidth={2} connectNulls={false}
                dot={pts.length <= 24 ? { r: 2, fill: color } : false} activeDot={{ r: 3, fill: color }} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
      {warnLine == null && critLine == null && th?.mode === "anomaly" && (
        <div className="mc-legend"><span title="The anomaly line needs enough baseline history for this time of the week; until then no alert can fire on this metric.">
          No anomaly line yet (not enough baseline history)</span></div>
      )}
      {(warnLine != null || critLine != null) && (
        <div className="mc-legend">
          {sameLine
            ? <span><i style={{ background: "#ef4444" }} /> Warn/Crit {th?.comparison || ""} {fmt(critLine)}{isFar(critLine) ? " (above chart)" : ""}</span>
            : <>
                {warnLine != null && <span><i style={{ background: "#f59e0b" }} /> Warn {th?.comparison || ""} {fmt(warnLine)}{isFar(warnLine) ? " (above chart)" : ""}</span>}
                {critLine != null && <span><i style={{ background: "#ef4444" }} /> Crit {th?.comparison || ""} {fmt(critLine)}{isFar(critLine) ? " (above chart)" : ""}</span>}
              </>}
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
