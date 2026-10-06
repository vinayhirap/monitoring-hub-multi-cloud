// components/AlertInvestigation.jsx
// Side drawer that keeps one alert's whole investigation in one place:
//   Alert -> Incident -> Evidence -> Root cause -> Action -> Resolution
// Reuses existing endpoints only (explain, incidents, evidence panel, ack/resolve/mute handlers
// passed in by the Alerts page). There is NO assignment, notes or manual incident creation in the
// backend, so none is shown here (see RUNBOOK "required backend work").
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { explainAlert, getIncidents, getIncidentDetail, rcaReportUrl } from "../api/api";
import { useTimezone } from "../contexts/TimezoneContext";
import { Badge, StatusBeacon, AiChip } from "./ui";
import ResourceEvidence from "./ResourceEvidence";
import { tsMs, ageText, lifecycle } from "../utils/evidence";
import { metricLabel, formatMetricValue } from "../utils/metricLabels";
import "./AlertInvestigation.css";
import { plural } from "../utils/plural";
import DownloadButton from "./DownloadButton";
import { copyText } from "../utils/copyText";

const SEV_TONE = { CRITICAL: "crit", WARNING: "warn", INFO: "info" };

export default function AlertInvestigation({ alert: a, canAct, acting, onClose, onAck, onResolve, onMute, onFalsePositive, route, canConsole, onConsole }) {
  const navigate = useNavigate();
  const { ianaName } = useTimezone();
  const [ex, setEx] = useState(undefined);          // undefined loading, null failed, object ok
  const [inc, setInc] = useState(undefined);        // undefined loading, null unavailable, [] none
  const [now, setNow] = useState(() => Date.now());
  const [muteMin, setMuteMin] = useState("60");
  const [copied, setCopied] = useState(null);          // null | "ok" | "fail": feedback for Copy link
  const id = a?.id;

  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 30000); return () => clearInterval(t); }, []);
  const panel = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;                      // latest handler without re-running the focus effect
  // Modal behaviour: focus moves into the drawer, Tab/Shift+Tab stay inside it, Esc closes, focus returns on close.
  useEffect(() => {
    const prev = document.activeElement;
    panel.current?.focus();
    const h = e => {
      if (e.key === "Escape") { closeRef.current(); return; }
      if (e.key !== "Tab" || !panel.current) return;
      const f = [...panel.current.querySelectorAll("button:not([disabled]),a[href],select,input,[tabindex]:not([tabindex='-1'])")].filter(x => x.offsetParent !== null);
      if (!f.length) { e.preventDefault(); return; }
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", h);
    return () => { document.removeEventListener("keydown", h); if (prev && prev.focus && document.contains(prev)) prev.focus(); };
  }, []);

  useEffect(() => {
    if (id == null) return undefined;
    let dead = false;
    setEx(undefined); setInc(undefined);
    explainAlert(id).then(r => !dead && setEx(r || null)).catch(() => !dead && setEx(null));
    // Linked incident: only incidents whose time window overlaps this alert are opened, and an incident is
    // shown only when this alert is a confirmed member (never guessed).
    (async () => {
      try {
        const list = await getIncidents(a.account_id, { limit: 50 });
        const t0 = tsMs(a.triggered_at) || 0, t1 = tsMs(a.resolved_at) || Date.now();
        const cand = (list || []).filter(i => (tsMs(i.started_at) || 0) <= t1 && (tsMs(i.last_seen_at) || Date.now()) >= t0).slice(0, 4);
        const det = await Promise.all(cand.map(i => getIncidentDetail(a.account_id, i.id).catch(() => null)));
        const mine = det.filter(d => d && (d.alerts || []).some(x => x.id === id)).map(d => ({ id: d.id, title: d.title, severity: d.severity, status: d.status, n: d.alerts.length, cause: d.probable_cause }));
        if (!dead) setInc(mine);
      } catch { if (!dead) setInc(null); }
    })();
    return () => { dead = true; };
  }, [id, a?.account_id, a?.triggered_at, a?.resolved_at]);

  const steps = useMemo(() => (a ? lifecycle(a) : []), [a]);
  if (!a) return null;
  const sev = String(a.severity || "").toUpperCase();
  const state = a.state || a.status;
  const open = state !== "resolved";
  const t0 = tsMs(a.triggered_at), t1 = tsMs(a.resolved_at);
  const dur = t0 != null ? ageText((t1 ?? now) - t0) : "—";
  const fmt = v => (tsMs(v) != null ? new Date(tsMs(v)).toLocaleString("en-GB", { timeZone: ianaName, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "—");
  const ids = [a.resource, a.resource_name].filter(Boolean);
  const busy = acting === id;

  return (
    <>
      <div className="ai-scrim" onClick={onClose} />
      <aside className="ai" ref={panel} tabIndex={-1} role="dialog" aria-modal="true" aria-label={`Investigate alert ${metricLabel(a.metric_name)}`}>
        <header className="ai-h">
          <div className="ai-title">
            <Badge tone={SEV_TONE[sev] || "mute"}><StatusBeacon tone={SEV_TONE[sev] || "mute"} pulse={open && sev === "CRITICAL"} />{sev.toLowerCase()}</Badge>
            <h2>{metricLabel(a.metric_name)}</h2>
          </div>
          <button className="ai-x" onClick={onClose} aria-label="Close investigation">✕</button>
          <div className="ai-sub">{a.resource_name || a.resource} · {a.account_name}{a.region ? ` · ${a.region}` : ""}{a.service ? ` · ${String(a.service).toUpperCase()}` : ""}</div>
        </header>

        <div className="ai-body">
          <section className="ai-sec">
            <h3>1 · Alert</h3>
            <div className="ai-facts">
              <div><span>Value</span><b className={open ? `c-${SEV_TONE[sev] || "mute"}` : ""} title={String(a.current_value ?? "")}>{formatMetricValue(a.metric_name, a.current_value)}</b></div>
              <div><span>Threshold</span><b title={String(a.threshold ?? "")}>{formatMetricValue(a.metric_name, a.threshold)}</b></div>
              <div><span>State</span><b>{String(state).toUpperCase()}</b></div>
              <div><span>{open ? "Open for" : "Lasted"}</span><b>{dur}</b></div>
            </div>
            <ol className="ai-life">
              {steps.map(s => (
                <li key={s.key} className={s.done ? "done" : ""}>
                  <i />
                  <span><b>{s.label}</b>{s.done && s.at ? <> · {fmt(s.at)}</> : s.done ? "" : " · not yet"}{s.who ? ` · by ${s.who}` : ""}{s.why ? ` · ${s.why}` : ""}</span>
                </li>
              ))}
            </ol>
            {(a.muted_until || a.silenced_reason || a.marked_false_positive || a.auto_tuning) && (
              <div className="ai-flags">
                {a.muted_until && <Badge tone="mute">muted until {fmt(a.muted_until)}</Badge>}
                {a.silenced_reason && <Badge tone="mute">{a.silenced_reason}</Badge>}
                {a.marked_false_positive && <Badge tone="mute">marked not genuine</Badge>}
                {a.auto_tuning && <Badge tone="mute" title="Normal range already crosses the static threshold; threshold is being auto-tuned">auto-tuning</Badge>}
              </div>
            )}
          </section>

          <section className="ai-sec">
            <h3>2 · Incident</h3>
            {inc === undefined ? <p className="ai-dim">Checking correlated incidents…</p>
              : inc === null ? <p className="ai-dim">Incidents are not available for your role.</p>
              : inc.length === 0 ? <p className="ai-dim">Not part of a correlated incident{ex?.related_alert_count ? `, but ${plural(ex.related_alert_count, "other alert")} ${ex.related_alert_count === 1 ? "is" : "are"} active alongside it` : ""}.</p>
              : inc.map(i => (
                <button key={i.id} className="ai-inc" onClick={() => navigate(`/accounts/${a.account_id}/incidents`)}>
                  <Badge tone={SEV_TONE[String(i.severity).toUpperCase()] || "mute"}>{String(i.severity).toLowerCase()}</Badge>
                  <span className="ai-inc-t">{i.title}</span>
                  <span className="ai-dim">{i.status} · {i.n} alerts →</span>
                </button>
              ))}
          </section>

          <section className="ai-sec">
            <h3>3 · Evidence</h3>
            <ResourceEvidence metricsRoute={route} accountId={a.account_id} service={a.service} resourceId={a.resource} resourceIds={ids} reloadKey={`${a.state}|${a.acked_at}|${a.resolved_at}|${a.muted_until}`} />
          </section>

          <section className="ai-sec">
            <h3>4 · Probable root cause {ex?.summary_source === "llm" && <span className="ai-chip"><AiChip method="generated summary" /></span>}</h3>
            {ex === undefined ? <p className="ai-dim">Analysing…</p> : ex === null ? <p className="ai-dim">Couldn't load the analysis for this alert.</p> : (
              <>
                <div className="ai-conf">
                  <span className={`ai-c ai-c-${ex.confidence}`}>{ex.confidence} confidence</span>
                  {ex.is_likely_flapping && <Badge tone="mute">likely flapping</Badge>}
                </div>
                <p className="ai-sum">{ex.summary}</p>
                <dl className="ai-dl">
                  {ex.trend?.description && !String(ex.summary || "").toLowerCase().includes(String(ex.trend.description).toLowerCase().replace(/[.\s]+$/, "")) && <><dt>Behaviour</dt><dd>{ex.trend.description}</dd></>}      {/* the summary often already says it */}
                  {ex.probable_trigger && <><dt>Possible trigger</dt><dd><code>{ex.probable_trigger.event_name}</code> by {ex.probable_trigger.username || "unknown"}{ex.probable_trigger.event_time ? ` · ${fmt(ex.probable_trigger.event_time)}` : ""} <span className="ai-dim">(probable, not confirmed)</span></dd></>}
                  {ex.recent_deployment && <><dt>Recent deployment</dt><dd>{ex.recent_deployment.message || ex.recent_deployment.event_type || "Deployment event recorded shortly before"}</dd></>}
                  {ex.related_alert_count > 0 && <><dt>Related</dt><dd>{plural(ex.related_alert_count, "other alert")} active alongside</dd></>}
                </dl>
              </>
            )}
          </section>

          <section className="ai-sec">
            <h3>5 · Action</h3>
            <div className="ai-actions">
              {canAct && open && state !== "acknowledged" && <button className="ui-btn" disabled={busy} onClick={() => onAck(id)}>Acknowledge</button>}
              {canAct && open && <button className="ui-btn" disabled={busy} onClick={() => { if (window.confirm("Resolve this alert?\n\nIt is closed now and re-opens on its own if the condition is still present.")) onResolve(id); }}>Resolve</button>}
              {canAct && open && (a.muted_until
                ? <button className="ui-btn" disabled={busy} onClick={() => onMute(id, 0)}>Unmute</button>
                : <span className="ai-mute">
                    <select value={muteMin} onChange={e => setMuteMin(e.target.value)} aria-label="Mute duration">
                      <option value="30">30 min</option><option value="60">1 hour</option><option value="240">4 hours</option><option value="1440">24 hours</option>
                    </select>
                    <button className="ui-btn" disabled={busy} onClick={() => onMute(id, Number(muteMin))}>Mute</button>
                  </span>)}
              {canAct && <button className="ui-btn" onClick={() => onFalsePositive(id, !a.marked_false_positive)}>{a.marked_false_positive ? "Undo: not genuine" : "Mark as not genuine"}</button>}
              {!canAct && <span className="ai-dim">View-only access: ask an Admin or Editor to acknowledge or resolve.</span>}
            </div>
            <div className="ai-links">
              {route && <button className="rev-link" onClick={() => navigate(route)}>Open resource metrics →</button>}
              {canConsole && <button className="rev-link" onClick={() => onConsole(id)}>Open in cloud console →</button>}
              <DownloadButton className="rev-link" path={rcaReportUrl(id, "pdf")} fallbackName={`CloudOps-RCA-Alert-${id}.pdf`}>RCA report (PDF) ↓</DownloadButton>
              <button className="rev-link" aria-live="polite" onClick={async () => {
                const ok = await copyText(`${window.location.origin}/alerts?tab=all&q=${encodeURIComponent(a.resource || "")}&alert=${id}`);
                setCopied(ok ? "ok" : "fail"); setTimeout(() => setCopied(null), 2000);
              }}>{copied === "ok" ? "Link copied ✓" : copied === "fail" ? "Copy failed: select the address bar URL" : "Copy link"}</button>
            </div>
          </section>

          <section className="ai-sec">
            <h3>6 · Resolution</h3>
            {a.resolved_at || a.status === "resolved"
              ? <p className="ai-sum">Resolved {fmt(a.resolved_at)}{a.resolution_reason ? ` — ${a.resolution_reason}` : ""}. Lasted {dur}.</p>
              : <p className="ai-dim">Still open. Resolution is recorded when the metric recovers or when someone resolves it here.</p>}
          </section>
        </div>
      </aside>
    </>
  );
}
