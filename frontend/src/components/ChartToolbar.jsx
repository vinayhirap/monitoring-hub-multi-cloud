// components/ChartToolbar.jsx -- time range tabs + auto-refresh interval +
// manual refresh + "updated Ns ago" + retention note. Shared by every metrics
// panel so range/refresh behave identically for every cloud and service.
import { useEffect, useState } from "react";

// Charts re-read CloudOps' stored data every 5 min, which is exactly how often
// the collector writes new points (polling_model.py). Anything faster only
// re-reads the same rows, so there is no user-facing interval selector.
// Refreshing never asks AWS for new data: AWS is polled by the collector only.
export const AUTO_REFRESH_MS = 300000;
export const STAT_OVERRIDES = ["auto", "Average", "Minimum", "Maximum", "Sum", "SampleCount"];

export default function ChartToolbar({
  ranges, timeRange, onTimeRangeChange, onRefresh, lastUpdated, loading,
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
        <button className="mc-refresh-btn" onClick={onRefresh} disabled={loading}
                title="Reload the charts from CloudOps. This does not poll AWS; new data arrives every 5 min from the collector. Charts also reload automatically every 5 min while this tab is visible.">{loading ? "⟳ …" : "⟳ Refresh"}</button>
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
