// src/pages/overview/panels.jsx -- Overview dashboard sections. Presentation only: every value
// arrives pre-derived from utils/dashboardModel.js (real endpoint data, nothing estimated).
import { Panel, Badge, StatusBeacon, KpiStrip, KpiCard, DataTable, EmptyState, AiChip, SeverityBadge } from "../../components/ui";
import { ageLabel, serviceLabel, FRESH_MIN, STALE_MIN } from "../../utils/dashboardModel";
import { metricLabel } from "../../utils/metricLabels";

const TONE = { crit: "crit", warn: "warn", info: "info", ok: "ok", critical: "crit", warning: "warn", healthy: "ok", unknown: "mute" };
const LEVEL_TEXT = { critical: "Critical", warning: "Warning", healthy: "Healthy", unknown: "No data" };
const hourLabel = (t, tz) => new Date(t).toLocaleTimeString("en-US", { hour: "2-digit", hour12: false, timeZone: tz });
const hhmm = (t, tz) => new Date(t).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: tz });
const Dim = ({ children }) => <span className="dash-dim">{children}</span>;

/* ── 1. What is the state of the platform, and why ─────────────────────────── */
export function StatusHero({ verdict, kpi, fetchedAt, now, revalidating }) {
  const t = TONE[verdict.level];
  const age = fetchedAt ? now - fetchedAt : null;
  return (
    <section className={`dash-hero t-${t}`} aria-label="Platform status">
      <div className="dash-hero-verdict">
        <span className="dash-hero-cap">Platform status</span>
        <span className="dash-hero-level"><StatusBeacon tone={t} pulse={verdict.level === "critical"} label={LEVEL_TEXT[verdict.level]} />{LEVEL_TEXT[verdict.level]}</span>
        {verdict.total > 0 && <span className="dash-hero-sub">{verdict.affected} of {verdict.total} region{verdict.total === 1 ? "" : "s"} affected</span>}
      </div>
      <ul className="dash-hero-reasons">
        {verdict.reasons.map(r => <li key={r}>{r}</li>)}
        {verdict.level === "unknown" && <li>No accounts are reporting yet.</li>}
      </ul>
      <div className="dash-hero-meta">
        <span className="dash-hero-cap">Data</span>
        <span className="dash-hero-fresh">{age == null ? "loading…" : age < 45000 ? "just now" : `${ageLabel(age)} ago`}{revalidating ? " · updating" : ""}</span>
        <span className="dash-hero-sub">{kpi.fresh}/{kpi.regions} regions synced ≤{FRESH_MIN} min</span>
      </div>
    </section>
  );
}

/* ── 2. How severe, how much, what changed: one row of answerable numbers ──── */
export function KpiRow({ kpi, onGo }) {
  const n = v => (v == null ? "—" : v);
  return (
    <div className="dash-kpis"><KpiStrip>
      <KpiCard label="Critical resources" value={kpi.critical} tone={kpi.critical ? "crit" : undefined} pulse={kpi.critical > 0}
        sub={kpi.critical ? `${kpi.critAlerts} critical alert${kpi.critAlerts === 1 ? "" : "s"} firing` : "none firing"} onClick={() => onGo("/alerts?tab=critical")} />
      <KpiCard label="Warning resources" value={kpi.warning} tone={kpi.warning ? "warn" : undefined} sub={kpi.warning ? `${kpi.warnAlerts} warning alert${kpi.warnAlerts === 1 ? "" : "s"} firing` : "none firing"} onClick={() => onGo("/alerts?tab=active")} />
      <KpiCard label="Active incidents" value={n(kpi.incidents)} tone={kpi.incidents ? "crit" : undefined}
        sub={kpi.incidents == null ? "not available for this role" : "correlated, system-detected"} onClick={() => onGo("#dash-incidents")} />
      <KpiCard label="Needs attention" value={kpi.attention} tone={kpi.attention ? "warn" : undefined}
        sub={kpi.attention ? `${kpi.attentionAlerts} alert${kpi.attentionAlerts === 1 ? "" : "s"} on resources with health < 70` : "no resource below health 70"} onClick={() => onGo("/alerts?tab=attention")} />
      <KpiCard label="Metric anomalies" value={kpi.anomalies} tone={kpi.anomalies ? "warn" : undefined} sub="multivariate, firing" onClick={() => onGo("#dash-intel")} />
      <KpiCard label="New · last hour" value={kpi.new1h} sub={`${kpi.new6h} in 6h · ${kpi.new24h} in 24h`} onClick={() => onGo("#dash-activity")} />
      <KpiCard label="Resolved · 24h" value={kpi.res24h} tone={kpi.res24h ? "ok" : undefined} sub={`${kpi.res1h} in the last hour`} onClick={() => onGo("/alerts?tab=resolved")} />
      <KpiCard label="Regions synced" value={`${kpi.fresh}/${kpi.regions}`} tone={kpi.stale ? "warn" : kpi.regions && kpi.fresh === kpi.regions ? "ok" : undefined}
        sub={kpi.stale ? `${kpi.stale} stale` : `≤${FRESH_MIN} min`} onClick={() => onGo("#dash-coverage")} />
    </KpiStrip></div>
  );
}

/* ── 3. What changed: 24h alert activity ───────────────────────────────────── */
export function ActivityPanel({ activity, tz }) {
  const { buckets, windows, capped } = activity;
  const max = Math.max(1, ...buckets.map(b => b.crit + b.warn + b.info));
  const maxR = Math.max(1, ...buckets.map(b => b.resolved));
  const empty = buckets.every(b => b.crit + b.warn + b.info + b.resolved === 0);
  return (
    <Panel title="Alert activity, last 24 hours" subtitle="Alerts triggered per hour (top) and resolved per hour (bottom)"
      actions={<span className="dash-legend"><i className="lg-crit" />Critical<i className="lg-warn" />Warning<i className="lg-info" />Info<i className="lg-ok" />Resolved</span>}
      footer={<>Triggered {windows.new1h} · 1h, {windows.new6h} · 6h, {windows.new24h} · 24h &nbsp;|&nbsp; Resolved {windows.res1h} · 1h, {windows.res6h} · 6h, {windows.res24h} · 24h
        {capped.resolved && " (resolved list capped at the 500 most recent, older hours may under-count)"}{capped.firing && " (firing list capped at 1000)"}</>}>
      <div id="dash-activity" className="dash-anchor" />
      {empty ? <EmptyState title="No alerts triggered or resolved in the last 24 hours" /> : (
        <div className="act" role="img" aria-label={`Alerts triggered per hour over 24 hours: ${windows.new24h} total`}>
          <div className="act-cols">
            {buckets.map(b => {
              const tot = b.crit + b.warn + b.info;
              return (
                <div key={b.t} className="act-col" title={`${hhmm(b.t, tz)} · ${tot} triggered (${b.crit} critical, ${b.warn} warning, ${b.info} info) · ${b.resolved} resolved`}>
                  <div className="act-up">
                    <div className="act-stack" style={{ height: `${(tot / max) * 100}%` }}>
                      {b.crit > 0 && <span className="s-crit" style={{ flex: b.crit }} />}
                      {b.warn > 0 && <span className="s-warn" style={{ flex: b.warn }} />}
                      {b.info > 0 && <span className="s-info" style={{ flex: b.info }} />}
                    </div>
                  </div>
                  <div className="act-down"><span className="s-ok" style={{ height: `${(b.resolved / maxR) * 100}%` }} /></div>
                </div>
              );
            })}
          </div>
          <div className="act-axis">{buckets.map((b, i) => <span key={b.t}>{i % 4 === 0 ? hourLabel(b.t, tz) : ""}</span>)}</div>
        </div>
      )}
    </Panel>
  );
}

/* ── 4. What changed, as a feed ────────────────────────────────────────────── */
export function FeedPanel({ feed, total, now, onGo }) {
  return (
    <Panel title="Recent changes" subtitle="Triggered, resolved, incidents and system events, last 24 hours" flush
      footer={total > feed.length ? `Showing ${feed.length} of ${total}` : `${total} event${total === 1 ? "" : "s"} in 24h`}>
      {feed.length === 0 ? <EmptyState title="Nothing has changed in the last 24 hours" /> : (
        <ul className="feed">
          {feed.map((f, i) => (
            <li key={`${f.kind}-${f.t}-${i}`}>
              <button type="button" className="feed-row" onClick={() => onGo(f.to)}>
                <span className="feed-age">{ageLabel(now - f.t)}</span>
                <Badge tone={TONE[f.sev] || "mute"}>{f.kind}</Badge>
                <span className="feed-main"><span className="feed-text">{f.text}</span><span className="feed-sub">{f.sub}</span></span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

/* ── 5. Where: region x service ───────────────────────────────────────────── */
export function MatrixPanel({ matrix, onGo }) {
  const { cols, rows } = matrix;
  return (
    <Panel title="Where it is happening" subtitle="Firing alerts by region and service. Click a cell to open that service." flush
      footer={<><i className="lg-ok dot" /> monitored, clear &nbsp; <i className="lg-warn dot" /> warning &nbsp; <i className="lg-crit dot" /> critical (+n warning) &nbsp; — not monitored</>}>
      {rows.length === 0 ? <EmptyState title="No regions to show" /> : (
        <div className="tbl-scroll">
          <table className="mx">
            <thead><tr><th>Account · region</th>{cols.map(c => <th key={c} className="mx-c">{serviceLabel(c)}</th>)}</tr></thead>
            <tbody>
              {rows.map(({ row, cells }) => (
                <tr key={row.id}>
                  <th scope="row"><button type="button" className="mx-name" onClick={() => onGo(`/accounts/${row.id}/services`)}>
                    <StatusBeacon tone={TONE[row.status] || "mute"} /><span>{row.account_name}</span><span className="mx-region">{row.region}</span></button></th>
                  {cols.map(c => {
                    const x = cells[c];
                    if (!x) return <td key={c} className="mx-c"><Dim>—</Dim></td>;
                    const tone = x.crit ? "crit" : x.warn ? "warn" : x.total ? "info" : "ok";
                    return (
                      <td key={c} className="mx-c">
                        <button type="button" className={`mx-cell t-${tone}`} onClick={() => onGo(`/accounts/${row.id}/${c}`)}
                          title={x.total ? `${x.crit} critical, ${x.warn} warning${x.total - x.crit - x.warn ? `, ${x.total - x.crit - x.warn} info` : ""}` : "No firing alerts"} aria-label={`${row.account_name} ${row.region} ${serviceLabel(c)}: ${x.total ? `${x.crit} critical, ${x.warn} warning` : "clear"}`}>
                          {!x.total ? <StatusBeacon tone="ok" /> : x.crit ? <>{x.crit}{x.warn > 0 && <small className="mx-more">+{x.warn}</small>}</> : x.warn ? x.warn : x.total}
                        </button>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

/* ── 6. Which resources ───────────────────────────────────────────────────── */
export function TopResourcesPanel({ rows, onGo }) {
  const cols = [
    { key: "name", header: "Resource", render: r => (
      <span className="dash-res"><span className="dash-res-name" title={r.resource}>{r.name}</span>
        <span className="dash-res-sub">{serviceLabel(r.service)} · {r.account_name}{r.region ? ` · ${r.region}` : ""}</span></span>) },
    { key: "sev", header: "Alerts", sort: r => r.crit * 1000 + r.warn, render: r => (
      <span className="dash-sevs">{r.crit > 0 && <Badge tone="crit">{r.crit} crit</Badge>}{r.warn > 0 && <Badge tone="warn">{r.warn} warn</Badge>}{r.info > 0 && <Badge tone="info">{r.info}</Badge>}</span>) },
    { key: "metric", header: "Metric", render: r => <span title={r.metrics.map(metricLabel).join(", ")}>{metricLabel(r.metrics[0])}{r.metrics.length > 1 ? ` +${r.metrics.length - 1}` : ""}</span> },
    { key: "age", header: "Open", num: true, sort: r => r.age ?? 0, render: r => ageLabel(r.age) },
    { key: "score", header: "Health", num: true, sort: r => r.score ?? 101, render: r => (r.score == null ? <Dim>—</Dim> : <span className={`ov-n t-${r.score < 40 ? "crit" : "warn"}`}>{Math.round(r.score)}</span>) },
  ];
  return (
    <Panel title="Top problematic resources" subtitle="Ranked by critical, then warning alerts, then how long they have been firing" flush
      footer="Health is the AIOps score and is shown only for resources scoring below 70.">
      <DataTable dense columns={cols} rows={rows} rowKey={r => r.key} onRowClick={r => onGo(`/alerts?tab=active&q=${encodeURIComponent(r.resource || "")}`)}
        empty={<EmptyState title="No firing alerts on any resource" />} />
    </Panel>
  );
}

/* ── 7. Incidents ─────────────────────────────────────────────────────────── */
export function IncidentsPanel({ incidents, capped, now, onGo }) {
  return (
    <Panel title="Active incidents" subtitle="Alerts correlated into one problem by the platform" flush
      footer={capped ? "Showing the first 24 account-regions." : "Opens the account's incident view."}>
      <div id="dash-incidents" className="dash-anchor" />
      {incidents == null ? <EmptyState title="Incident data isn't available" body="Your role may not include incident access." />
        : incidents.length === 0 ? <EmptyState title="No active incidents" /> : (
        <ul className="inc">
          {incidents.slice(0, 6).map(i => (
            <li key={`${i.account_row_id}-${i.id}`}>
              <button type="button" className="inc-row" onClick={() => onGo(`/accounts/${i.account_row_id}/incidents`)}>
                <SeverityBadge severity={i.severity} />
                <span className="inc-main"><span className="inc-title">{i.title}</span>
                  <span className="inc-sub">{i.probable_cause ? `${String(i.probable_cause).slice(0, 90)} · ` : ""}{i.alert_count} alert{i.alert_count === 1 ? "" : "s"} · {i.startedMs != null ? `${ageLabel(now - i.startedMs)} ago` : ""}</span></span>
              </button>
            </li>
          ))}
          {incidents.length > 6 && <li className="dash-more">+{incidents.length - 6} more</li>}
        </ul>
      )}
    </Panel>
  );
}

/* ── 8. Intelligence: anomalies, capacity forecast, self-tuning ───────────── */
export function IntelligencePanel({ anomalies, fleet, kpi, now, onGo }) {
  const cap = fleet?.detail?.capacity_risks || [];
  return (
    <Panel title="Anomalies and forecasts" subtitle="Model-derived signals, kept visually distinct from measurements" flush
      footer={<button type="button" className="ui-btn" onClick={() => onGo("/alerts?tab=attention")}>Alerts needing attention</button>}>
      <div id="dash-intel" className="dash-anchor" />
      <h3 className="dash-sec">Metric anomalies <AiChip method="Multivariate" /><span>{anomalies.length}</span></h3>
      {anomalies.length === 0 ? <div className="dash-none">No multivariate anomalies firing</div> : (
        <ul className="mini">{anomalies.slice(0, 4).map(a => (
          <li key={a.id}><button type="button" className="mini-row" onClick={() => onGo(`/alerts?tab=active&q=${encodeURIComponent(a.resource || "")}`)}>
            <span className="mini-main">{a.resource_name || a.resource}</span><span className="mini-sub">{a.account_name} · {ageLabel(now - (Date.parse(a.triggered_at) || now))}</span></button></li>))}</ul>
      )}
      <h3 className="dash-sec">Capacity forecast <AiChip method="Linear trend" /><span>{fleet ? cap.length : "—"}</span></h3>
      {!fleet ? <div className="dash-none">Not available for this role</div> : cap.length === 0 ? <div className="dash-none">No resource is trending toward a limit</div> : (
        <ul className="mini">{cap.slice(0, 4).map(c => (
          <li key={`${c.aws_account_id}-${c.resource_id}-${c.metric_name}`}><button type="button" className="mini-row" onClick={() => onGo(`/alerts?tab=active&q=${encodeURIComponent(c.resource_id || "")}`)}>
            <span className="mini-main">{c.resource_name || c.resource_id}</span>
            <span className="mini-sub">{metricLabel(c.metric_name)}{Number.isFinite(Number(c.current_value)) && c.current_value !== null ? ` ${Number(c.current_value).toFixed(0)}%` : ""}</span>
            <Badge mode="predicted" title="Linear-trend forecast, not a measurement">{Math.round(Number(c.days_to_exhaustion))}d left</Badge></button></li>))}</ul>
      )}
      {kpi.flapping > 0 && <div className="dash-note"><button type="button" className="mx-name" onClick={() => onGo("/alerts?tab=tuning")}>{kpi.flapping} alert{kpi.flapping === 1 ? " is" : "s are"} flapping and being auto-tuned</button></div>}
    </Panel>
  );
}

/* ── 9. Coverage and data freshness ───────────────────────────────────────── */
export function CoveragePanel({ freshness, kpi, onGo }) {
  const counts = { fresh: 0, aging: 0, stale: 0, unknown: 0 };
  freshness.forEach(f => { counts[f.level]++; });
  const total = freshness.length || 1;
  const total_res = r => ["ec2_total", "ebs_total", "rds_total", "lambda_total", "s3_total", "elb_total", "ecs_total"].reduce((n, k) => n + (Number(r[k]) || 0), 0);
  return (
    <Panel title="Coverage and data freshness" subtitle="When each account-region last reported, and what is monitored" flush
      footer={`Fresh ≤ ${FRESH_MIN} min, aging ≤ ${STALE_MIN} min, stale beyond. “Unknown” means no sync timestamp is recorded.`}>
      <div id="dash-coverage" className="dash-anchor" />
      <div className="fresh-bar" role="img" aria-label={`${counts.fresh} fresh, ${counts.aging} aging, ${counts.stale} stale, ${counts.unknown} unknown of ${freshness.length} regions`}>
        {["fresh", "aging", "stale", "unknown"].map(k => counts[k] > 0 && <span key={k} className={`fb-${k}`} style={{ flex: counts[k] }} title={`${counts[k]} ${k}`} />)}
      </div>
      <div className="fresh-legend">{kpi.fresh} fresh · {counts.aging} aging · {counts.stale} stale · {counts.unknown} unknown <Dim>({total === 0 ? 0 : freshness.length} regions)</Dim></div>
      <div className="tbl-scroll">
        <table className="ui-table is-dense">
          <thead><tr><th>Account · region</th><th className="is-num">Last sync</th><th className="is-num">Services</th><th className="is-num">Resources</th></tr></thead>
          <tbody>
            {freshness.slice(0, 7).map(({ row, age, level, services }) => (
              <tr key={row.id} className="ui-clickable" onClick={() => onGo(`/accounts/${row.id}/services`)}>
                <td><StatusBeacon tone={{ fresh: "ok", aging: "warn", stale: "crit", unknown: "mute" }[level]} /> {row.account_name} <Dim>{row.region}</Dim></td>
                <td className="is-num">{level === "unknown" ? <Dim>never</Dim> : `${ageLabel(age)} ago`}</td>
                <td className="is-num">{services}</td><td className="is-num">{total_res(row)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {freshness.length > 7 && <div className="dash-more">+{freshness.length - 7} more regions</div>}
      </div>
    </Panel>
  );
}
