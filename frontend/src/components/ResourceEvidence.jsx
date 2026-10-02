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
import "./ResourceEvidence.css";

const SEV_TONE = { CRITICAL: "crit", WARNING: "warn", INFO: "info", ERROR: "crit", RESOLVED: "ok" };
const fmtVal = v => (v == null || v === "" ? "—" : Number.isFinite(Number(v)) ? String(Number(Number(v).toFixed(2))) : String(v));

export default function ResourceEvidence({ accountId, resourceIds, resourceId, service, reloadKey }) {
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
  const h = st.health;
  const score = h && h.health_score != null ? Number(h.health_score) : null;
  const reason = h?.score_reason || {};
  const rows = showAll ? timeline : timeline.slice(0, 6);

  const focusMetric = name => {
    const el = document.getElementById(metricAnchor(name));
    if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" }); el.classList.add("mc-flash"); setTimeout(() => el.classList.remove("mc-flash"), 1600); }
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
              <div className="rev-v">~{Math.min(...st.forecast.map(f => f.days_to_exhaustion))}<span> days</span></div>
              <div className="rev-s">{st.forecast[0].metric_name}{st.forecast.length > 1 ? ` +${st.forecast.length - 1} more` : ""} trending to its limit</div>
            </>
          ) : <><div className="rev-v">—</div><div className="rev-s">No metric trending to exhaustion</div></>}
        </div>
      </div>

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
                    <button className="rev-link" onClick={() => focusMetric(it.alert.metric_name)} title="Jump to this metric's chart">{it.alert.metric_name}</button>
                    {it.label === "triggered" && <span className="rev-dim"> {fmtVal(it.alert.current_value ?? it.alert.value)} vs threshold {fmtVal(it.alert.threshold)}</span>}
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
