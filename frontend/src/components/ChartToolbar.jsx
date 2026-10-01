// components/ChartToolbar.jsx -- time range tabs + auto-refresh interval +
// manual refresh + "updated Ns ago" + retention note. Shared by every metrics
// panel so range/refresh behave identically for every cloud and service.
import { useEffect, useState } from "react";

export const REFRESH_OPTIONS = [
  { label: "Off", ms: 0 }, { label: "15 s", ms: 15000 }, { label: "30 s", ms: 30000 },
  { label: "1 min", ms: 60000 }, { label: "5 min", ms: 300000 },
];
export const DEFAULT_REFRESH_MS = 30000;
export const STAT_OVERRIDES = ["auto", "Average", "Minimum", "Maximum", "Sum", "SampleCount"];

export default function ChartToolbar({
  ranges, timeRange, onTimeRangeChange, refreshMs, onRefreshMsChange, onRefresh, lastUpdated, loading,
  statOverride, onStatOverrideChange, bucketSecs, retentionDays, requestedHours, effectiveHours, showTabs = true,
}) {
  const [, force] = useState(0);
  useEffect(() => { const t = setInterval(() => force(x => x + 1), 5000); return () => clearInterval(t); }, []);
  const ago = lastUpdated ? Math.max(0, Math.round((Date.now() - lastUpdated) / 1000)) : null;
  const limited = retentionDays && requestedHours && effectiveHours && requestedHours > effectiveHours;
  const cap = retentionDays ? retentionDays * 24 : null;
  return (
    <>
      <div className="mc-toolbar">
        {showTabs && (
          <div className="time-range-tabs">
            {ranges.map(t => (
              <button key={t.label} className={`tr-btn ${timeRange === t.hours ? "tr-active" : ""} ${cap && t.hours > cap ? "tr-limited" : ""}`}
                      title={cap && t.hours > cap ? `History is retained for ${retentionDays} days; this shows all of it.` : undefined}
                      onClick={() => onTimeRangeChange(t.hours)}>{t.label}</button>
            ))}
          </div>
        )}
        {onStatOverrideChange && (
          <select value={statOverride} onChange={e => onStatOverrideChange(e.target.value)} title="Statistic applied to every chart that supports it (each chart can still override)">
            {STAT_OVERRIDES.map(s => <option key={s} value={s}>{s === "auto" ? "Statistic: metric default" : `Statistic: ${s}`}</option>)}
          </select>
        )}
        <select value={refreshMs} onChange={e => onRefreshMsChange(Number(e.target.value))} title="Auto-refresh interval (paused while the tab is hidden)">
          {REFRESH_OPTIONS.map(o => <option key={o.ms} value={o.ms}>{o.ms ? `Auto-refresh ${o.label}` : "Auto-refresh off"}</option>)}
        </select>
        <button className="mc-refresh-btn" onClick={onRefresh} disabled={loading}>{loading ? "⟳ …" : "⟳ Refresh"}</button>
        <span className="mc-updated">
          {ago != null ? `Updated ${ago < 5 ? "just now" : `${ago}s ago`}` : ""}
        </span>
      </div>
      {limited && (
        <div className="mc-note">
          ⚠ metric history is kept for {retentionDays} days, so this range shows the last {retentionDays} days (all that exists).
        </div>
      )}
    </>
  );
}
