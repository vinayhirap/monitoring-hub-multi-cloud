// src/components/MetricZoomModal.jsx
//
// Replacement for a previous "zoomed chart" modal that was found live on
// Prod (a popup with its own "Local timezone"/"UTC timezone" dropdown,
// triggered by clicking an inline MetricChart) but had NO corresponding
// source anywhere in this repository's git history -- confirmed via
// `git log --all -S` across every commit and branch. Whatever was
// running in production was an orphaned build artifact with nothing to
// patch; this is a clean, from-scratch replacement.
//
// The bug that made the old modal worth replacing rather than guessing
// at: toggling its own separate timezone dropdown showed either no
// conversion at all, or the IST offset subtracted a SECOND time,
// depending on which way you toggled it -- classic symptom of manual
// offset arithmetic instead of a real Intl/timeZone-aware conversion.
//
// Design choice made deliberately here, per the explicit requirement
// that changing the timezone from the header must change EVERY
// timestamp in the app, consistently, every time: this modal has NO
// timezone control of its own. It reads `ianaName` from the same
// TimezoneContext every other page in the app already uses correctly
// (see contexts/TimezoneContext.jsx and its docstring). A second,
// independent selector is exactly what let the old modal drift out of
// sync with the rest of the app in the first place -- removing it
// removes the whole bug class by construction, not just this instance
// of it.
//
// Deliberately NOT reproduced from the old modal: a period ("5
// minutes") / statistic ("Average") dropdown pair. Those controls had
// no real backend behind them -- the live metrics endpoint
// (get_ec2_metric_series in app/aws/collector_direct.py) always queries
// a fixed 60-second period with a statistic baked in per metric (see
// app/aws/metric_catalog_data.py), not a per-request choice. Shipping
// a dropdown that LOOKS functional but silently does nothing would be
// worse than not having it; wiring it for real would mean threading a
// period/stat override through the API, which is a separate piece of
// work, not a timezone fix.
import { useMemo, useState } from "react";
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
} from "recharts";
import { useTimezone, formatInTz } from "../contexts/TimezoneContext";
import { XIcon, RefreshCwIcon, ExternalLinkIcon } from "./icons";

const RANGE_OPTIONS = [
  { label: "1H", hours: 1 },
  { label: "3H", hours: 3 },
  { label: "1D", hours: 24 },
  { label: "1W", hours: 24 * 7 },
];

export default function MetricZoomModal({
  open, onClose, title, data, unit = "", color = "#22d3ee",
  seriesLabel, onRefresh, viewInMetricsHref, valueFormatter,
}) {
  const { ianaName, timezone } = useTimezone();
  // Defaults to the widest range that still fits the data actually
  // passed in -- this modal windows client-side over whatever the
  // parent already fetched rather than re-querying CloudWatch itself,
  // so "1W" only shows real data if the parent fetched a week's worth.
  const [rangeHours, setRangeHours] = useState(RANGE_OPTIONS[RANGE_OPTIONS.length - 1].hours);

  const windowed = useMemo(() => {
    if (!data || data.length === 0) return [];
    const cutoff = Date.now() - rangeHours * 3600 * 1000;
    return data
      .map(d => ({ t: new Date(d.t).getTime(), v: d.v }))
      .filter(d => d.t >= cutoff);
  }, [data, rangeHours]);

  if (!open) return null;

  const fmtTick = (ms) => formatInTz(ms, ianaName, { hour: "2-digit", minute: "2-digit", hour12: false });
  const fmtTooltipLabel = (ms) => formatInTz(ms, ianaName, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });

  return (
    <div className="mzm-overlay" onClick={onClose}>
      <div className="mzm-panel" onClick={(e) => e.stopPropagation()}>
        <div className="mzm-header">
          <div className="mzm-title">{title}</div>
          <button className="mzm-icon-btn" onClick={onClose} title="Close">
            <XIcon size={16} />
          </button>
        </div>

        <div className="mzm-controls">
          <div className="mzm-range-group">
            {RANGE_OPTIONS.map(opt => (
              <button
                key={opt.label}
                className={`mzm-range-btn ${rangeHours === opt.hours ? "active" : ""}`}
                onClick={() => setRangeHours(opt.hours)}
              >
                {opt.label}
              </button>
            ))}
          </div>
          <div className="mzm-controls-right">
            {/* No timezone selector here on purpose -- see file header
                comment. This just tells the viewer where the current
                display timezone comes from, so it's never a mystery. */}
            <span className="mzm-tz-note">Times shown in {timezone} — change in the header</span>
            {onRefresh && (
              <button className="mzm-icon-btn" onClick={onRefresh} title="Refresh">
                <RefreshCwIcon size={14} />
              </button>
            )}
          </div>
        </div>

        <div className="mzm-chart">
          {windowed.length === 0 ? (
            <div className="mzm-empty">No data in the selected range</div>
          ) : (
            <ResponsiveContainer width="100%" height={320}>
              <LineChart data={windowed} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                <CartesianGrid stroke="rgba(99,130,190,0.08)" strokeDasharray="3 3" />
                <XAxis
                  dataKey="t" type="number" domain={["dataMin", "dataMax"]} scale="time"
                  tickFormatter={fmtTick} tick={{ fontSize: 11, fill: "#7a90b8" }}
                  tickLine={false} axisLine={false}
                />
                <YAxis tick={{ fontSize: 11, fill: "#7a90b8" }} tickLine={false} axisLine={false} />
                <Tooltip
                  contentStyle={{ background: "#0b1220", border: "1px solid rgba(99,130,190,0.2)", borderRadius: 6, fontSize: 12 }}
                  labelStyle={{ color: "#7a90b8" }}
                  labelFormatter={fmtTooltipLabel}
                  formatter={(value) => [valueFormatter ? valueFormatter(value) : `${Number(value).toFixed(2)}${unit}`, seriesLabel || title]}
                />
                <Line type="monotone" dataKey="v" stroke={color} strokeWidth={2} dot={false} activeDot={{ r: 3, fill: color }} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>

        {seriesLabel && <div className="mzm-legend"><span className="mzm-legend-dot" style={{ background: color }} />{seriesLabel}</div>}

        <div className="mzm-footer">
          {viewInMetricsHref && (
            <a className="mzm-btn mzm-btn-primary" href={viewInMetricsHref} target="_blank" rel="noreferrer">
              <ExternalLinkIcon size={14} /> View in metrics
            </a>
          )}
          <button className="mzm-btn" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
