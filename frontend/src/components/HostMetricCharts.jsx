// components/HostMetricCharts.jsx
//
// "Metric history" for one host agent: the SAME chart card, toolbar (1H 3H 6H 1D 1W 1M, statistic, refresh) and
// "metrics in alert" strip the EC2 / RDS / Lambda pages use, fed by GET /api/host-agents/{id}/history.
// Stored data only: the server reads metric_history and nothing is requested from the host or any cloud. A bucket exists
// only where the agent delivered a reading, so an offline host's charts simply end at its last report.
//
// Loaded lazily by the host drawer (recharts is the heavy part), so the Host Agents list never pays for it.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Panel, EmptyState, Skeleton } from "./ui";
import MetricChartCard, { MetricPanelContext, AlertedMetricsStrip } from "./MetricChartCard";
import ChartToolbar from "./ChartToolbar";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { getHostAgentHistory } from "../api/hostAgents";
import {
  CHART_RANGES, DEFAULT_CHART_HOURS, CHART_REFRESH_MS, CHART_REFRESH_TIP, CHART_STAT_OPTIONS, chartGroups, chartColor, chartsNote,
} from "../utils/hostAgentsView";
import "../pages/AccountDetail.css";      // .time-range-tabs / .charts-grid / .chart-box: the shared chart styles (same file the EC2 page uses)
import "../pages/HostAgents.css";

const errText = (e) => (e && e.message) || "request failed";

export default function HostMetricCharts({ host }) {
  const hostId = host.id;
  const [timeRange, setTimeRange] = useState(DEFAULT_CHART_HOURS);
  const [statOverride, setStatOverride] = useState("auto");
  const [data, setData] = useState(null);                  // null = first load in flight; else the API payload
  const [error, setError] = useState(null);
  const [doneKey, setDoneKey] = useState(null);         // request key of the last finished load (success or failure)
  const [lastUpdated, setLastUpdated] = useState(null);
  const [reloadN, setReloadN] = useState(0);
  const reqKey = `${hostId}|${timeRange}|${reloadN}`;
  const loading = doneKey !== reqKey;                    // derived, so no state is set synchronously inside the effect

  useEffect(() => {
    let live = true;
    getHostAgentHistory(hostId, timeRange)
      .then(d => {
        if (!live) return;
        setData(d && typeof d === "object" ? d : { series: [], meta: {} });
        setError(null);
        setLastUpdated(Date.now());
      })
      .catch(e => {
        if (!live) return;
        setError(e?.status === 404 ? "This host or the host agent feature is no longer available." : errText(e));
        setData(prev => prev ?? { series: [], meta: {} });      // keep the last good charts on a failed refresh
      })
      .finally(() => { if (live) setDoneKey(reqKey); });
    return () => { live = false; };
  }, [hostId, timeRange, reloadN, reqKey]);
  useAutoRefresh(() => setReloadN(n => n + 1), CHART_REFRESH_MS);
  const refresh = useCallback(() => setReloadN(n => n + 1), []);

  const groups = useMemo(() => chartGroups(data?.series), [data]);
  const note = chartsNote(data, host);
  const rangeLabel = CHART_RANGES.find(r => r.hours === timeRange)?.label || "6H";
  const ctx = useMemo(() => ({
    meta: data?.meta || {}, windowHours: data?.effective_hours || timeRange,
    bucketSecs: data?.bucket_secs || null, statOverride,
  }), [data, timeRange, statOverride]);
  const hasSeries = groups.length > 0;

  return (
    <Panel title="Metric history" subtitle="Charted from the readings the agent delivered (stored data, nothing estimated)">
      {data == null && <div aria-busy="true"><Skeleton h={120} /></div>}
      {data != null && (
        <ChartToolbar
          ranges={CHART_RANGES} timeRange={timeRange} onTimeRangeChange={setTimeRange}
          onRefresh={refresh} lastUpdated={lastUpdated} loading={loading}
          statOverride={statOverride} onStatOverrideChange={setStatOverride}
          bucketSecs={data.bucket_secs} retentionDays={data.retention_days}
          requestedHours={data.requested_hours} effectiveHours={data.effective_hours}
          refreshTitle={CHART_REFRESH_TIP} statOptions={CHART_STAT_OPTIONS}
        />
      )}
      {error && <p className="ha-error" role="alert">Could not load charts: {error}</p>}
      {data != null && !hasSeries && !error && (
        <EmptyState title="No history yet"
          body="This host has not delivered any metric readings, so there is nothing to chart." />
      )}
      {hasSeries && (
        <MetricPanelContext.Provider value={ctx}>
          <AlertedMetricsStrip meta={data.meta} />
          {groups.map(g => (
            <section key={g.key} className="ha-chart-group" aria-label={g.title}>
              <h3 className="ha-chart-group-title">{g.title}</h3>
              <div className="charts-grid">
                {g.items.map((s, i) => (
                  <div key={s.name} className={g.key === "compute" && i === 0 ? "chart-full" : undefined}>
                    <MetricChartCard
                      title={s.title} metricKey={s.name} data={s.data} color={chartColor(s.contract)}
                      unit={s.unit === "Percent" ? "%" : ""} timeRange={rangeLabel}
                      emptyReason={`No readings in the last ${rangeLabel}`}
                    />
                  </div>
                ))}
              </div>
            </section>
          ))}
        </MetricPanelContext.Provider>
      )}
      {note && <p className="ha-hint">{note}</p>}
    </Panel>
  );
}
