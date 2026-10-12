// src/pages/synthetic/CheckDetail.jsx
// Per-check detail drawer for the Synthetic Checks page: current state, last error/notice, response-time and
// cert-days-left trends, current TLS facts and a table of recent probes (error text wrapped, never truncated).
//
// Data: ONE request on open to GET /api/synthetic-checks/{id}/results?hours=N (no polling -- the page itself
// does not poll). The endpoint has no row limit or pagination (every probe in the window, oldest first, N up to
// 720 h), so the default window is 24 h and 7 d is only fetched when the person asks for it. Charts are
// thinned to <= MAX_POINTS in the browser; the table pages client-side.
//
// Wiring in SyntheticChecks.jsx is three tiny pieces: <tr {...CheckDetail.rowProps(c.id, setDetailId)}>, a
// detailId state, and <CheckDetail checks={checks} id={detailId} onClose={...} /> (renders nothing when id is null).
import { useEffect, useMemo, useState } from "react";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, ReferenceLine } from "recharts";
import ResourceDrawer from "../../components/ResourceDrawer";
import { XIcon } from "../../components/icons";
import { getSyntheticResults } from "../../api/api";
import { useTimezone } from "../../contexts/TimezoneContext";
import { formatStamp, zoneLabel } from "../../utils/timeFormat";
import "./CheckDetail.css";

const RANGES = [{ hours: 24, label: "24 h" }, { hours: 168, label: "7 d" }];
const MAX_POINTS = 240;     // chart points; more than this is thinned
const PAGE = 50;            // probe rows per page in the table
// Same thresholds as SyntheticChecks.jsx / the backend cert alert (WARNING <= 30d, CRITICAL <= 7d). Display only.
const CERT_WARN_DAYS = 30, CERT_CRIT_DAYS = 7;
const TYPE_LABEL = { http: "HTTP", https: "HTTPS", tcp: "TCP", dns: "DNS" };
const AXIS = { fontSize: 9, fill: "#3d5070" };
const TIP = { background: "#0b1220", border: "1px solid rgba(99,130,190,0.2)", borderRadius: 6, fontSize: 11 };

// DB datetimes arrive as zone-less strings that are UTC by project convention (see utils/timeFormat).
function toMs(v) {
  if (v == null || v === "") return null;
  if (typeof v === "number") return v;
  let s = String(v).trim();
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(s) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)) s = s.replace(" ", "T") + "Z";
  const t = Date.parse(s);
  return Number.isNaN(t) ? null : t;
}

// Keeps a long series drawable: at most `max` points, each the reduction of a consecutive chunk.
function thin(arr, max, reduce) {
  if (arr.length <= max) return arr;
  const size = Math.ceil(arr.length / max), out = [];
  for (let i = 0; i < arr.length; i += size) out.push(reduce(arr.slice(i, i + size)));
  return out;
}

function percentile(sorted, p) {
  if (!sorted.length) return null;
  return sorted[Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1)];
}

// Same rule as the list's badge: a failing probe only flips the stored status after `threshold` in a row.
function stateOf(c) {
  const n = Number(c.consecutive_failures) || 0, t = Number(c.consecutive_failure_threshold) || 0;
  if (c.enabled && c.current_status !== "down" && n > 0 && t > 0) {
    return { cls: "failing", label: `Failing ${Math.min(n, t)}/${t}`,
             hint: `${n} failed probe${n === 1 ? "" : "s"} in a row; marked Down after ${t} in a row.` };
  }
  if (c.current_status === "up") return { cls: "up", label: "Up" };
  if (c.current_status === "down") return { cls: "down", label: "Down" };
  return { cls: "unknown", label: "Unknown" };
}

function Trend({ data, dataKey, ianaName, unit, yDomain, color, refs = [], dotFor }) {
  const [t0, t1] = [data[0].t, data[data.length - 1].t];
  const dayish = t1 - t0 > 36 * 3600e3;
  const tick = (ms) => {
    const d = new Date(ms);
    const o = { timeZone: ianaName, hourCycle: "h23" };
    return new Intl.DateTimeFormat("en-GB", dayish ? { ...o, day: "2-digit", month: "short" } : { ...o, hour: "2-digit", minute: "2-digit" }).format(d);
  };
  return (
    <div className="cd-chart">
      <ResponsiveContainer width="100%" height={150}>
        <LineChart data={data} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="rgba(99,130,190,0.08)" strokeDasharray="3 3" />
          <XAxis dataKey="t" type="number" scale="time" domain={[t0, t1 === t0 ? t0 + 1 : t1]} tickFormatter={tick}
                 tick={AXIS} tickLine={false} axisLine={false} minTickGap={40} />
          <YAxis domain={yDomain} tick={AXIS} tickLine={false} axisLine={false} width={44} allowDecimals={false} />
          <Tooltip contentStyle={TIP} labelStyle={{ color: "#7a90b8" }} itemStyle={{ color }}
                   labelFormatter={(ms) => formatStamp(new Date(ms), ianaName, zoneLabel(ianaName))}
                   formatter={(v, _n, item) => [`${v} ${unit}${item?.payload?.fail ? " (failed probe)" : ""}`, ""]} />
          {refs.map(r => <ReferenceLine key={r.y} y={r.y} stroke={r.color} strokeDasharray={r.dash} strokeWidth={1} ifOverflow="extendDomain" />)}
          <Line type="linear" dataKey={dataKey} stroke={color} strokeWidth={2} connectNulls={false}
                dot={dotFor || false} activeDot={{ r: 3, fill: color }} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

// red dot only on buckets that contain a failed probe
const failDot = (p) => (p.payload?.fail && p.cx != null
  ? <circle key={`f${p.index}`} cx={p.cx} cy={p.cy} r={3} fill="#f26868" />
  : <g key={`f${p.index}`} />);

function ProbeTable({ rows, https, ianaName }) {
  const [shown, setShown] = useState(PAGE);
  const newest = useMemo(() => [...rows].reverse(), [rows]);
  if (!newest.length) return <div className="cd-empty">No probes recorded in this window.</div>;
  return (
    <>
      <div className="tbl-scroll">
        <table className="cd-probes">
          <thead>
            <tr><th>Time</th><th>Result</th><th>Resp</th><th>HTTP</th>{https && <th>Cert</th>}<th>Message</th></tr>
          </thead>
          <tbody>
            {newest.slice(0, shown).map((r, i) => {
              const res = !r.success ? ["fail", "Failed"] : r.error_message ? ["notice", "Notice"] : ["ok", "OK"];
              return (
                <tr key={`${r.t}-${i}`}>
                  <td data-label="Time" className="mono">{formatStamp(new Date(r.t), ianaName, zoneLabel(ianaName))}</td>
                  <td data-label="Result"><span className={`cd-res cd-res-${res[0]}`}>{res[1]}</span></td>
                  <td data-label="Resp" className="mono">{r.response_time_ms != null ? `${r.response_time_ms} ms` : "—"}</td>
                  <td data-label="HTTP" className="mono">{r.status_code ?? "—"}</td>
                  {https && <td data-label="Cert" className="mono">{r.cert_days_left != null ? `${r.cert_days_left} d` : "—"}</td>}
                  <td data-label="Message" className={`cd-msg ${r.success ? "cd-msg-note" : "cd-msg-err"}`}>{r.error_message || ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {newest.length > shown && (
        <button type="button" className="cd-more" onClick={() => setShown(s => s + PAGE)}>
          Show {Math.min(PAGE, newest.length - shown)} more ({newest.length - shown} older)
        </button>
      )}
    </>
  );
}

function Fact({ k, v, mono = true }) {
  return <div className="cd-fact"><dt>{k}</dt><dd className={mono ? "mono" : undefined}>{v ?? "—"}</dd></div>;
}

function Detail({ check: c, onClose }) {
  const { ianaName } = useTimezone();
  const [hours, setHours] = useState(24);
  const [res, setRes] = useState(null);                      // { key, data } | { key, error }
  const key = `${c.id}:${hours}`;
  useEffect(() => {
    let live = true;
    getSyntheticResults(c.id, hours)
      .then(data => live && setRes({ key, data }))
      .catch(e => live && setRes({ key, error: e.detail || e.message || "Could not load results" }));
    return () => { live = false; };
  }, [c.id, hours, key]);
  const cur = res?.key === key ? res : null;                 // ignore a response for a window we've left
  const https = c.check_type === "https";
  const fmt = (v) => (v == null ? null : formatStamp(new Date(toMs(v)), ianaName, zoneLabel(ianaName)));

  const rows = useMemo(() => (cur?.data?.results || [])
    .map(r => ({ ...r, t: toMs(r.checked_at) })).filter(r => r.t != null), [cur]);

  const rt = useMemo(() => {
    const pts = rows.map(r => ({ t: r.t, ms: r.response_time_ms, fail: !r.success }));
    const series = thin(pts, MAX_POINTS, ch => {
      const withMs = ch.filter(p => p.ms != null);
      return { t: ch[0].t, ms: withMs.length ? Math.max(...withMs.map(p => p.ms)) : null, fail: ch.some(p => p.fail) };
    });
    const sorted = rows.filter(r => r.response_time_ms != null).map(r => r.response_time_ms).sort((a, b) => a - b);
    const avg = sorted.length ? Math.round(sorted.reduce((a, b) => a + b, 0) / sorted.length) : null;
    return { series, avg, p95: percentile(sorted, 95), max: sorted.length ? sorted[sorted.length - 1] : null, thinned: pts.length > MAX_POINTS };
  }, [rows]);

  const certSeries = useMemo(() => thin(
    rows.filter(r => r.cert_days_left != null).map(r => ({ t: r.t, d: r.cert_days_left })), MAX_POINTS, ch => ch[ch.length - 1]), [rows]);

  // freshest TLS facts: last probe in the window that reached a TLS session, else what the list row carries
  const tls = useMemo(() => [...rows].reverse().find(r => r.tls_version) || c, [rows, c]);

  const st = stateOf(c);
  const last = rows.length ? rows[rows.length - 1] : null;
  const days = tls.cert_days_left;

  return (
    <div className="cd">
      <div className="cd-head">
        <div className="cd-title">
          <h2>{c.name}</h2>
          <button type="button" className="cd-close" onClick={onClose} aria-label="Close details"><XIcon size={14} /></button>
        </div>
        <div className="cd-sub">
          <span className="cd-type mono">{TYPE_LABEL[c.check_type] || c.check_type}</span>
          <span className="cd-target mono">{c.target}</span>
        </div>
      </div>

      <section className="cd-sec">
        <h3>Current state</h3>
        <div className="cd-state">
          <span className={`syn-status cd-badge cd-badge-${st.cls}`} title={st.hint}>● {st.label}</span>
          {!c.enabled && <span className="cd-paused">Disabled — not being probed</span>}
        </div>
        <dl className="cd-facts">
          <Fact k={`Uptime (${hours === 24 ? "24 h" : "7 d"})`} v={cur?.data ? (cur.data.uptime_pct != null ? `${cur.data.uptime_pct}%` : "—") : "…"} />
          <Fact k="Last probe" v={last ? fmt(last.t) : (cur?.data ? "—" : "…")} />
          <Fact k="Interval" v={`${c.interval_seconds}s`} />
          <Fact k="Fail after" v={`${c.consecutive_failure_threshold} in a row`} />
        </dl>
        {c.last_error
          ? <div className={c.last_ok ? "cd-note" : "cd-err"}><b>{c.last_ok ? "Notice" : "Last error"}:</b> {c.last_error}</div>
          : <div className="cd-ok">{c.last_ok == null ? "Not probed yet." : "Latest probe succeeded with no errors or notices."}</div>}
      </section>

      <div className="cd-range" role="group" aria-label="Time window">
        {RANGES.map(r => (
          <button key={r.hours} type="button" aria-pressed={hours === r.hours}
                  className={hours === r.hours ? "on" : ""} onClick={() => setHours(r.hours)}>{r.label}</button>
        ))}
      </div>

      {cur?.error && <div className="syn-error cd-load-err" role="alert"><span>{cur.error}</span></div>}
      {!cur && <div className="cd-empty">Loading…</div>}

      {cur?.data && (
        <>
          <section className="cd-sec">
            <h3>Response time</h3>
            {rt.series.some(p => p.ms != null)
              ? <>
                  <Trend data={rt.series} dataKey="ms" ianaName={ianaName} unit="ms" yDomain={[0, "auto"]} color="#2bb3ac" dotFor={failDot} />
                  <div className="cd-legend">
                    avg {rt.avg} ms · p95 {rt.p95} ms · max {rt.max} ms · {rows.length} probes
                    {rt.thinned && " · chart shows the peak of each group of probes"}
                    {rows.some(r => !r.success) && <> · <i className="cd-dot-fail" /> failed probe</>}
                  </div>
                </>
              : <div className="cd-empty">No response times recorded in this window.</div>}
          </section>

          {https && (
            <section className="cd-sec">
              <h3>Certificate</h3>
              {certSeries.length
                ? <>
                    <Trend data={certSeries} dataKey="d" ianaName={ianaName} unit="days left" yDomain={["auto", "auto"]} color="#7c6ee0"
                           refs={[{ y: CERT_WARN_DAYS, color: "#f59e0b", dash: "4 4" }, { y: CERT_CRIT_DAYS, color: "#ef4444", dash: "2 3" }]} />
                    <div className="cd-legend">days left on the certificate; dashed lines at {CERT_WARN_DAYS} d (warning) and {CERT_CRIT_DAYS} d (critical)</div>
                  </>
                : <div className="cd-empty">No successful TLS handshake in this window.</div>}
              {tls.tls_version
                ? <dl className="cd-facts cd-facts-tls">
                    <Fact k="Subject" v={tls.cert_subject} />
                    <Fact k="Issuer" v={tls.cert_issuer} />
                    <Fact k="Expires" v={tls.cert_not_after ? `${fmt(tls.cert_not_after)}${days != null ? ` (${days < 0 ? "expired" : `${days} day${days === 1 ? "" : "s"} left`})` : ""}` : null} />
                    <Fact k="TLS version" v={tls.tls_version} />
                    <Fact k="Cipher" v={tls.tls_cipher} />
                    <Fact k="Handshake" v={tls.handshake_ms != null ? `${tls.handshake_ms} ms` : null} />
                    <Fact k="Chain + hostname" v={tls.cert_valid == null ? null : tls.cert_valid ? "verified" : "failed verification"} />
                  </dl>
                : <div className="cd-empty">No TLS session recorded yet.</div>}
            </section>
          )}

          <section className="cd-sec">
            <h3>Recent probes</h3>
            <ProbeTable key={key} rows={rows} https={https} ianaName={ianaName} />
          </section>
        </>
      )}
    </div>
  );
}

export default function CheckDetail({ checks, id, onClose }) {
  const c = id == null ? null : checks.find(x => x.id === id);
  if (!c) return null;
  return (
    <ResourceDrawer onClose={onClose} label={`Check details: ${c.name}`}>
      <Detail key={c.id} check={c} onClose={onClose} />
    </ResourceDrawer>
  );
}

// Spread onto the table row: opens the panel on click / Enter, except when the click is on a control inside the row
// (toggle, edit, delete, links).
CheckDetail.rowProps = (id, open) => ({
  className: "syn-row-click",
  tabIndex: 0,
  onClick: (e) => { if (!e.target.closest("button, a, input, label, select, textarea")) open(id); },
  onKeyDown: (e) => { if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) { e.preventDefault(); open(id); } },
  title: "Click for details",
});
