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
  // Default to the widest range that's actually reachable given what the
  // parent handed over, not blindly "1W" -- with the availability check
  // added below, a range wider than the loaded data gets disabled, and
  // defaulting to a disabled button would be a confusing way to open a
  // chart (exactly what a live screenshot showed: "1W" selected, but
  // the axis only spanned the page's default 6H load).
  const [rangeHours, setRangeHours] = useState(() => {
    if (!data || data.length < 2) return RANGE_OPTIONS[RANGE_OPTIONS.length - 1].hours;
    const times = data.map(d => new Date(d.t).getTime());
    const spanMs = Math.max(...times) - Math.min(...times);
    const reachable = RANGE_OPTIONS.filter(o => o.hours * 3600 * 1000 <= spanMs * 1.05);
    return reachable.length ? reachable[reachable.length - 1].hours : RANGE_OPTIONS[0].hours;
  });

  // Bug found live (screenshots showed 1H/3H correctly returning
  // "No data" while 1D/1W worked fine): this modal windows over
  // whatever `data` the PARENT page already fetched -- it does NOT
  // independently re-query CloudWatch. The parent's own inline range
  // selector (1H..ALL, in ServiceDetail.jsx) decides how wide/coarse
  // that fetch is; if it's currently set to something wide (1M, ALL),
  // the returned points can be sparse enough that the most recent one
  // is genuinely hours old. Filtering against Date.now() (the
  // browser's live clock) in that case correctly, but unhelpfully,
  // excludes everything for a narrow "1H" window -- there's no data
  // *right now*, even though there's plenty a few hours back.
  //
  // Fix: anchor the cutoff to the LATEST point actually present in
  // `data`, not to the browser's clock. This guarantees every range
  // option shows at least the most recent available reading -- "1H"
  // now means "the last hour of data we actually have", which is the
  // only thing this modal can honestly promise without independently
  // re-fetching from the live metrics endpoint (a larger change: it
  // would need instance/account/region plumbed down through every
  // MetricChart call site, not just a modal-local fix).
  const latestPointTime = useMemo(() => {
    if (!data || data.length === 0) return null;
    return Math.max(...data.map(d => new Date(d.t).getTime()));
  }, [data]);

  // Second bug found live, same root cause: with the page's main range
  // selector sitting on its default "6H", this modal's own "1W" button
  // silently showed just those same ~6 hours -- correct data, but
  // mislabeled as a week, with nothing telling the viewer why a "1W"
  // chart looked identical to "1D". availableSpanMs is how much history
  // the parent actually handed over; any range option that asks for
  // more than that is flagged rather than silently truncated.
  const earliestPointTime = useMemo(() => {
    if (!data || data.length === 0) return null;
    return Math.min(...data.map(d => new Date(d.t).getTime()));
  }, [data]);
  const availableSpanMs = (latestPointTime != null && earliestPointTime != null)
    ? latestPointTime - earliestPointTime
    : null;
  const TOLERANCE = 1.05; // 5% grace so "6H loaded" doesn't flag the 6H button itself
  const exceedsAvailable = (hours) =>
    availableSpanMs != null && hours * 3600 * 1000 > availableSpanMs * TOLERANCE;

  const windowed = useMemo(() => {
    if (!data || data.length === 0 || latestPointTime == null) return [];
    const cutoff = latestPointTime - rangeHours * 3600 * 1000;
    return data
      .map(d => ({ t: new Date(d.t).getTime(), v: d.v }))
      .filter(d => d.t >= cutoff);
  }, [data, rangeHours, latestPointTime]);

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
            {RANGE_OPTIONS.map(opt => {
              const unreachable = exceedsAvailable(opt.hours);
              return (
                <button
                  key={opt.label}
                  className={`mzm-range-btn ${rangeHours === opt.hours ? "active" : ""} ${unreachable ? "unavailable" : ""}`}
                  onClick={() => setRangeHours(opt.hours)}
                  disabled={unreachable}
                  title={unreachable
                    ? `Only ${formatSpan(availableSpanMs)} of data is loaded on this page right now -- ${opt.label} would show the same thing. Pick a wider range on the page itself (above the chart), then re-open this zoom.`
                    : undefined}
                >
                  {opt.label}
                </button>
              );
            })}
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
        {availableSpanMs != null && exceedsAvailable(RANGE_OPTIONS[RANGE_OPTIONS.length - 1].hours) && (
          // Second bug's fix, visible half: don't just quietly disable the
          // wider buttons -- say in plain words why "1W" can't show a week
          // right now, since that's exactly what looked like a silent lie
          // before this fix (a "1W" chart with only ~6h of real data in it).
          <div className="mzm-span-note">
            Only {formatSpan(availableSpanMs)} of data is loaded for this chart. To zoom further back,
            pick a wider range on the page itself (above the chart), then re-open this zoom.
          </div>
        )}

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

/** "5h 25m" / "45m" / "3d" -- for the "only X of data is loaded" note. */
function formatSpan(ms) {
  const totalMin = Math.round(ms / 60000);
  const days = Math.floor(totalMin / 1440);
  const hours = Math.floor((totalMin % 1440) / 60);
  const mins = totalMin % 60;
  if (days > 0) return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
  if (hours > 0) return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
  return `${mins}m`;
}
