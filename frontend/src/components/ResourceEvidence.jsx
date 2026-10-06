// components/ResourceEvidence.jsx
// "Evidence" strip for ONE resource, shown above its metric charts on every resource page.
// Answers: is it healthy, what is firing, what changed, will it run out -- and lets the user jump
// from an alert straight to the chart that proves it (problem -> metric -> event -> evidence).
// Every number comes from an existing endpoint; a section the caller may not read says so instead
// of showing a fake zero.
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getAlertsForResource, getOpEvents, getResourceHealth, getCapacityForecast } from "../api/api";
import { useAlertSync } from "../hooks/useAlertSync";
import { useTimezone } from "../contexts/TimezoneContext";
import { Badge, StatusBeacon, AiChip } from "./ui";
import { metricAnchor } from "./MetricChartCard";
import { alertsForResource, eventsForResource, buildTimeline, healthTone, ageText, tsMs } from "../utils/evidence";
import { buildInsights } from "../utils/intelligence";
import { fmtMetricValue } from "../utils/metricFormat";
import { metricLabel, formatMetricValue } from "../utils/metricLabels";
import "./ResourceEvidence.css";
import { formatDaysLeft } from "../utils/forecastFormat";

const SEV_TONE = { CRITICAL: "crit", WARNING: "warn", INFO: "info", ERROR: "crit", RESOLVED: "ok" };

export default function ResourceEvidence({ accountId, resourceIds, resourceId, service, insights: registry, reloadKey, metricsRoute, onSummary }) {
  const navigate = useNavigate();
  const { ianaName } = useTimezone();
  const ids = useMemo(() => [...new Set((resourceIds || []).filter(Boolean).map(String))], [resourceIds]);
  const primary = resourceId || ids[0];
  const [st, setSt] = useState({ alerts: undefined, events: undefined, health: undefined, forecast: undefined });
  const [showAll, setShowAll] = useState(false);
  const [tick, setTick] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  useAlertSync(() => setTick(t => t + 1), { enabled: accountId != null });
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 30000); return () => clearInterval(t); }, []);

  useEffect(() => {
    if (accountId == null || !primary) return undefined;
    let dead = false;
    const set = patch => { if (!dead) setSt(s => ({ ...s, ...patch })); };
    // null = this caller may not read it (403/other failure); undefined = still loading
    getAlertsForResource(accountId, primary).then(r => set({ alerts: Array.isArray(r) ? r : [] })).catch(() => set({ alerts: null }));
    getOpEvents({ account_id: accountId, limit: 200 }).then(r => set({ events: Array.isArray(r) ? r : [] })).catch(() => set({ events: null }));
    getResourceHealth(accountId).then(l => set({ health: (l || []).find(h => String(h.resource_id) === String(primary)) || { health_score: 100, absent: true } })).catch(() => set({ health: null }));
    getCapacityForecast(accountId, primary).then(f => set({ forecast: Array.isArray(f) ? f : [] })).catch(() => set({ forecast: null }));
    return () => { dead = true; };
  }, [accountId, primary, tick, reloadKey]);       // reloadKey: the host changed something about this resource (e.g. an alert was acknowledged/resolved)

  const alerts = useMemo(() => (st.alerts ? alertsForResource(st.alerts, ids) : st.alerts), [st.alerts, ids]);
  const events = useMemo(() => (st.events ? eventsForResource(st.events, ids) : st.events), [st.events, ids]);
  const timeline = useMemo(() => buildTimeline(alerts || [], events || []), [alerts, events]);
  const firing = (alerts || []).filter(a => a.state === "firing");
  const crit = firing.filter(a => a.severity === "CRITICAL").length;
  const warn = firing.filter(a => a.severity === "WARNING").length;
  // Lets a host page (resource detail panel) show a one-line summary on a collapsed Evidence section.
  const earlierN = alerts ? Math.max(0, alerts.length - firing.length) : 0;
  useEffect(() => {
    if (onSummary) onSummary({ loaded: alerts !== undefined, firing: firing.length, crit, earlier: earlierN });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [alerts === undefined, firing.length, crit, earlierN]);
  const h = st.health;
  const score = h && h.health_score != null ? Number(h.health_score) : null;
  const reason = h?.score_reason || {};
  const rows = showAll ? timeline : timeline.slice(0, 6);
  // Statistical insights computed from the series the charts below are drawing (only on pages that provide a registry)
  const intel = useMemo(() => {
    if (!registry) return null;
    const series = registry.forScope(primary);
    return buildInsights({ series, alerts: alerts || [], events: events || [], tz: ianaName,
      fmt: (sr, v) => fmtMetricValue(v, sr.unit && /^[A-Z]/.test(sr.unit) ? sr.unit : undefined, sr.unit) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [registry, registry?.version, primary, alerts, events, ianaName]);

  // Jump to the metric's chart when this page has it. Hosts without charts (the Alerts drawer) pass
  // metricsRoute so the link opens the resource's metrics page instead of silently doing nothing.
  const hasChart = name => typeof document !== "undefined" && !!document.getElementById(metricAnchor(name));
  const focusMetric = name => {
    const el = document.getElementById(metricAnchor(name));
    if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" }); el.classList.add("mc-flash"); setTimeout(() => el.classList.remove("mc-flash"), 1600); return; }
    if (metricsRoute) navigate(metricsRoute);
  };
  const fmtT = ms => new Date(ms).toLocaleString("en-GB", { timeZone: ianaName, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });

  if (!primary) return null;
  return (
    <section className="rev" aria-label="Resource evidence">
      <div className="rev-cards">
        <div className={`rev-card t-${firing.length ? (crit ? "crit" : "warn") : alerts === undefined ? "mute" : "ok"}`}>
          <div className="rev-k">Alerts</div>
          {alerts === undefined ? <div className="rev-v">…</div> : alerts === null ? <div className="rev-na">Not available for your role</div> : (
            <>
              <div className="rev-v">{firing.length}<span> firing</span></div>
              <div className="rev-s">{firing.length ? `${crit} critical · ${warn} warning` : "Nothing firing"}{alerts.length > firing.length ? ` · ${alerts.length - firing.length} earlier` : ""}</div>
            </>
          )}
        </div>
        <div className={`rev-card t-${healthTone(score)}`}>
          <div className="rev-k">Health score</div>
          {h === undefined ? <div className="rev-v">…</div> : h === null ? <div className="rev-na">Not available for your role</div> : (
            <>
              <div className="rev-v">{score}<span> / 100</span></div>
              <div className="rev-s">
                {h.absent ? "No breaching alerts" : [reason.critical_alerts ? `${reason.critical_alerts} critical` : null, reason.warning_alerts ? `${reason.warning_alerts} warning` : null,
                  reason.blast_radius_fan_out ? `${reason.blast_radius_fan_out} dependents` : null].filter(Boolean).join(" · ") || "Lowered by alerts"}
                {h.computed_at && <> · {ageText(now - (tsMs(h.computed_at) || now))} ago</>}
              </div>
            </>
          )}
        </div>
        <div className="rev-card t-mute">
          <div className="rev-k">Events <span className="rev-sub">naming this resource</span></div>
          {events === undefined ? <div className="rev-v">…</div> : events === null ? <div className="rev-na">Not available for your role</div> : (
            <>
              <div className="rev-v">{events.length}</div>
              <div className="rev-s">{events.length ? `latest ${ageText(now - (tsMs(events[0].created_at) || now))} ago` : "None in the recent window"}</div>
            </>
          )}
        </div>
        <div className={`rev-card ${st.forecast && st.forecast.length ? "t-predicted" : "t-mute"}`}>
          <div className="rev-k">Capacity {st.forecast && st.forecast.length > 0 && <AiChip method="trend fit" />}</div>
          {st.forecast === undefined ? <div className="rev-v">…</div> : st.forecast === null ? <div className="rev-na">Not available for your role</div> : st.forecast.length ? (
            <>
              <div className="rev-v">{formatDaysLeft(Math.min(...st.forecast.map(f => f.days_to_exhaustion)))}</div>
              <div className="rev-s">{st.forecast[0].metric_name}{st.forecast.length > 1 ? ` +${st.forecast.length - 1} more` : ""} trending to its limit</div>
            </>
          ) : <><div className="rev-v">—</div><div className="rev-s">No metric trending to exhaustion</div></>}
        </div>
      </div>

      {intel && (
        <div className="rev-intel" aria-label="Insights">
          <div className="rev-tl-h"><span>Insights</span><AiChip method="statistical" />
            <span className="rev-sub">computed from the metrics below against each one's own normal range; not a measurement</span></div>
          {intel.insights.length === 0 ? (
            <div className="rev-intel-empty">
              {intel.coverage.total === 0
                ? "No metric data on this resource yet, so there is nothing to analyse."
                : intel.coverage.analysed === 0
                ? "Not enough history yet to judge normal behaviour (needs about 2 hours of data per metric)."
                : `No unusual behaviour in the ${intel.coverage.analysed} metric${intel.coverage.analysed === 1 ? "" : "s"} with enough history.`}
              {intel.coverage.total > intel.coverage.analysed && intel.coverage.analysed > 0 && ` ${intel.coverage.total - intel.coverage.analysed} not analysed (too little history).`}
            </div>
          ) : intel.insights.map(i => (
            <div key={i.id} className={`rev-ins lv-${i.level}${i.kind === "forecast" ? " is-forecast" : ""}`}>
              <div className="rev-ins-h">
                <Badge tone={i.level === "high" ? "crit" : "warn"} mode={i.kind === "forecast" ? "predicted" : "actual"}>{i.kind === "forecast" ? "forecast" : "anomaly"}</Badge>
                <b>{i.title}</b>
                <span className={`rev-conf c-${i.confidence}`} title="Confidence reflects how many independent signals agree (the metric itself, co-moving metrics, a nearby event, a firing alert). It is not a probability.">{i.confidence} confidence</span>
              </div>
              <p>{i.text}</p>
              <div className="rev-ins-f">
                {i.metrics.map(m => <button key={m} className="rev-chip" onClick={() => focusMetric(m)} title="Jump to this metric's chart">{registry.forScope(primary).find(x => x.key === m)?.title || m}</button>)}
                <span className="rev-act"><b>Suggested check:</b> {i.action}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {timeline.length > 0 ? (
        <div className="rev-tl">
          <div className="rev-tl-h"><span>Evidence timeline</span><span className="rev-sub">alerts and events for this resource, newest first</span></div>
          <ul>
            {rows.map(it => (
              <li key={it.key} className={`rev-it ${it.kind}`}>
                <span className="rev-when mono">{fmtT(it.at)}</span>
                <Badge tone={SEV_TONE[it.sev] || "mute"}><StatusBeacon tone={SEV_TONE[it.sev] || "mute"} />{it.kind === "alert" ? (it.label === "resolved" ? "resolved" : it.sev.toLowerCase()) : `event · ${it.sev.toLowerCase()}`}</Badge>
                {it.kind === "alert" ? (
                  <span className="rev-body">
                    {(metricsRoute || hasChart(it.alert.metric_name))
                      ? <button className="rev-link" onClick={() => focusMetric(it.alert.metric_name)} title={hasChart(it.alert.metric_name) ? "Jump to this metric's chart" : "Open this resource's metrics"}>{metricLabel(it.alert.metric_name)}</button>
                      : <b>{metricLabel(it.alert.metric_name)}</b>}
                    {it.label === "triggered" && (
                      /* Show the reading that actually breached (breach_value/breach_threshold, migration 077), never the live
                         current_value: that is overwritten by the healthy cycles an open alert needs to resolve, which printed
                         "WARNING 11.68% vs threshold 70%". Alerts raised before the migration have no snapshot, so only the
                         threshold is shown for them rather than a value that may contradict the severity. */
                      it.alert.breach_value != null && it.alert.breach_threshold != null
                        ? <span className="rev-dim"> {formatMetricValue(it.alert.metric_name, it.alert.breach_value)} vs threshold {formatMetricValue(it.alert.metric_name, it.alert.breach_threshold)}</span>
                        : <span className="rev-dim"> threshold {formatMetricValue(it.alert.metric_name, it.alert.threshold)}</span>
                    )}
                    {it.label === "triggered" && it.alert.state === "firing" && <span className="rev-live"> · still firing</span>}
                    {it.alert.resolution_reason && it.label === "resolved" && <span className="rev-dim"> · {it.alert.resolution_reason}</span>}
                  </span>
                ) : (
                  <span className="rev-body"><span className="rev-dim">{it.event.event_type}</span> {it.event.message}</span>
                )}
              </li>
            ))}
          </ul>
          <div className="rev-foot">
            {timeline.length > 6 && <button className="rev-link" onClick={() => setShowAll(v => !v)}>{showAll ? "Show fewer" : `Show all ${timeline.length}`}</button>}
            <button className="rev-link" onClick={() => navigate(`/alerts?tab=all&q=${encodeURIComponent(primary)}`)}>Open in Alerts →</button>
            {service && <button className="rev-link" onClick={() => navigate(`/accounts/${accountId}/incidents`)}>Account incidents →</button>}
          </div>
        </div>
      ) : (alerts !== undefined && events !== undefined && (
        <div className="rev-empty">No alerts or events recorded for this resource{alerts === null || events === null ? " that your role can see" : ""}. Metrics below are the evidence of record.</div>
      ))}
    </section>
  );
}
