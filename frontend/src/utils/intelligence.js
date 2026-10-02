// utils/intelligence.js
// View-derived operational intelligence. Pure functions, no network, no model calls, no paid APIs.
// Everything here is computed from the series the user is already looking at (and the alerts/events
// already loaded for that resource), so it can never disagree with the charts. It is statistical
// (robust z-score on a median/MAD baseline, first-difference correlation, OLS projection) and is always
// labelled as such in the UI. It complements, and does not replace, the backend's persisted models
// (STL baselines, multivariate anomaly alerts, capacity forecasts, RCA).

export const MIN_POINTS = 24;       // below this a baseline is not trustworthy -> say so, never guess
const TAIL = 8;                     // newest points excluded from the baseline so an ongoing anomaly cannot hide itself
const Z_FLAG = 4, Z_RUN = 3;

const clean = pts => (pts || []).filter(p => p && p.v != null && Number.isFinite(Number(p.v)) && Number.isFinite(Number(p.t)))
  .map(p => ({ t: Number(p.t), v: Number(p.v) })).sort((a, b) => a.t - b.t);
export const median = a => { if (!a.length) return NaN; const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
export const mad = (a, med = median(a)) => median(a.map(x => Math.abs(x - med)));

/** Sustained deviation of the newest points from this metric's own robust baseline. */
export function detectAnomaly(points) {
  const v = clean(points);
  if (v.length < MIN_POINTS) return { status: "insufficient", n: v.length };
  const base = v.slice(0, v.length - TAIL).map(p => p.v);
  if (base.length < MIN_POINTS - TAIL) return { status: "insufficient", n: v.length };
  const med = median(base);
  const scale = Math.max(1.4826 * mad(base, med), 1e-9);
  const z = x => (x - med) / scale;
  const tail = v.slice(-3);
  // A deviation only counts if it also leaves the whole range this metric has shown in its own baseline.
  // Robust z alone over-fires on sparse/bursty data (Lambda invocations, error counts: baseline MAD is 0) and
  // heavy-tailed traffic; a burst the metric has produced before is not an anomaly.
  const bmin = Math.min(...base), bmax = Math.max(...base);
  const margin = Math.max(0.05 * (bmax - bmin), 0.02 * Math.abs(med), 1e-9);
  const outside = x => x > bmax + margin || x < bmin - margin;
  const hot = tail.filter(p => Math.abs(z(p.v)) >= Z_FLAG && outside(p.v));
  const dir = z(tail[tail.length - 1].v) >= 0 ? "up" : "down";
  const sameDir = hot.filter(p => (z(p.v) >= 0) === (dir === "up"));
  const latest = v[v.length - 1].v;
  const dev = Math.abs(latest - med);
  const material = Math.abs(med) < 1e-9 ? dev > 0 : dev >= 0.1 * Math.abs(med);   // a statistically tight but tiny wobble is not news
  if (sameDir.length < 2 || !material || Math.abs(z(latest)) < Z_FLAG || !outside(latest)) return { status: "normal", n: v.length, median: med, scale };
  let i = v.length - 1;
  while (i > 0 && Math.abs(z(v[i - 1].v)) >= Z_RUN && (z(v[i - 1].v) >= 0) === (dir === "up")) i--;
  const run = v.slice(i);
  return { status: "anomalous", dir, n: v.length, median: med, scale, latest, z: z(latest), onset: run[0].t, runLength: run.length, peak: dir === "up" ? Math.max(...run.map(p => p.v)) : Math.min(...run.map(p => p.v)) };
}

/** Do two metrics move together? Pearson on first differences (so a shared slow trend is not "correlation"),
 *  searched over +-2 samples of lead/lag. lag>0 means `a` moves first. */
export function coMovement(a, b) {
  const A = clean(a), B = clean(b);
  if (A.length < 12 || B.length < 12) return null;
  const step = median(A.slice(1).map((p, i) => p.t - A[i].t).filter(d => d > 0));
  if (!(step > 0)) return null;
  const key = t => Math.round(t / step);
  const mb = new Map(B.map(p => [key(p.t), p.v])), ma = new Map(A.map(p => [key(p.t), p.v]));
  let best = null;
  for (let lag = -2; lag <= 2; lag++) {
    const xs = [], ys = [];
    for (const [k, x] of ma) {
      const x0 = ma.get(k - 1), y = mb.get(k + lag), y0 = mb.get(k - 1 + lag);
      if (x0 == null || y == null || y0 == null) continue;
      xs.push(x - x0); ys.push(y - y0);
    }
    if (xs.length < 12) continue;
    const mx = xs.reduce((s, q) => s + q, 0) / xs.length, my = ys.reduce((s, q) => s + q, 0) / ys.length;
    let sxy = 0, sxx = 0, syy = 0;
    xs.forEach((q, i) => { sxy += (q - mx) * (ys[i] - my); sxx += (q - mx) ** 2; syy += (ys[i] - my) ** 2; });
    if (sxx < 1e-12 || syy < 1e-12) continue;
    const r = sxy / Math.sqrt(sxx * syy);
    if (!best || Math.abs(r) > Math.abs(best.r)) best = { r, lag, n: xs.length };
  }
  return best;
}

/** When will a threshold be reached if the recent trend continues? OLS over the newest <=36 points. */
export function projectCrossing(points, threshold, comparison = ">") {
  const v = clean(points);
  if (threshold == null || v.length < 12) return null;
  const w = v.slice(-36), t0 = w[0].t;
  const xs = w.map(p => (p.t - t0) / 3600e3), ys = w.map(p => p.v);
  const n = w.length, mx = xs.reduce((s, q) => s + q, 0) / n, my = ys.reduce((s, q) => s + q, 0) / n;
  let sxy = 0, sxx = 0, syy = 0;
  xs.forEach((q, i) => { sxy += (q - mx) * (ys[i] - my); sxx += (q - mx) ** 2; syy += (ys[i] - my) ** 2; });
  if (sxx < 1e-12 || syy < 1e-12) return null;
  const slope = sxy / sxx, r2 = (sxy * sxy) / (sxx * syy);
  const fitted = my + slope * (xs[n - 1] - mx);
  const lt = comparison === "<" || comparison === "<=";
  if (lt ? fitted <= threshold : fitted >= threshold) return null;          // already past it: that is an alert, not a forecast
  if (lt ? slope >= 0 : slope <= 0) return null;                            // heading away from it
  const hours = (threshold - fitted) / slope;
  if (r2 < 0.6 || hours > 168 || hours <= 0) return null;                   // weak fit or too far out to mean anything
  return { hours, r2, slope, n };
}

const ACTIONS = [
  [/cpu/i, "Check which process or workload is driving CPU and whether a deployment or scaling change landed at the onset; if sustained, scale up/out."],
  [/mem|swap/i, "Look for a leak or a newly loaded workload; compare against the last deployment and consider more memory or a restart window."],
  [/disk|volume|storage|filesystem|free_?space|bytes_?used/i, "Find what is growing (logs, snapshots, temp data) and clean up or expand before it fills."],
  [/network|bytes_?(in|out)|packets|throughput|bandwidth/i, "Identify the traffic source and direction; compare with recent security-group, routing or deployment changes."],
  [/latency|duration|response|time/i, "Check downstream dependencies and recent deployments; compare against error and connection metrics at the same time."],
  [/connection|conn_/i, "Check for connection leaks or a client retry storm; compare with application restarts or pool settings."],
  [/error|5xx|4xx|fail|throttl|reject/i, "Read the application logs around the onset time and correlate with the deployment or configuration change that preceded it."],
  [/iops|queue|read|write|burst/i, "Check for a batch job or backup window and whether the volume type's throughput or burst limits are being hit."],
];
export const actionFor = name => (ACTIONS.find(([re]) => re.test(String(name || ""))) || [null, "Compare this metric against the evidence timeline and any change made shortly before the onset."])[1];

const fmtClock = (ms, tz) => new Date(ms).toLocaleTimeString("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false });
const confOf = n => (n >= 3 ? "high" : n === 2 ? "medium" : "low");

/**
 * series: [{key,title,unit,pts,warn,crit,cmp,sev}]  alerts: rows of THIS resource  events: events naming THIS resource
 * fmt(seriesEntry, value) -> string.  Returns { insights, coverage } ; coverage says how many metrics were analysable.
 */
export function buildInsights({ series, alerts = [], events = [], fmt = (s, v) => String(v), tz = "UTC" }) {
  const per = (series || []).map(s => ({ s, a: detectAnomaly(s.pts) }));
  const analysable = per.filter(x => x.a.status !== "insufficient").length;
  const firing = new Set((alerts || []).filter(a => a.state === "firing").map(a => String(a.metric_name)));
  // cluster leader card = the metric that has a firing alert, else the strongest deviation
  const hot = per.filter(x => x.a.status === "anomalous").sort((x, y) => (firing.has(String(y.s.key)) - firing.has(String(x.s.key))) || Math.abs(y.a.z) - Math.abs(x.a.z));
  const out = [];

  const covered = new Set();                                         // metrics already explained as part of another insight
  for (const h of hot) {
    const { s, a } = h;
    if (covered.has(s.key)) continue;     // one cluster = one card, led by the strongest metric
    const step = median((clean(s.pts)).slice(1).map((p, i) => p.t - clean(s.pts)[i].t).filter(d => d > 0)) || 300e3;
    const co = [];
    for (const o of hot) {
      if (o === h) continue;
      const near = Math.abs(o.a.onset - a.onset) <= 4 * step;
      const cm = coMovement(s.pts, o.s.pts);
      const together = cm && Math.abs(cm.r) >= 0.7;
      if (near || together) co.push({ s: o.s, a: o.a, r: cm ? cm.r : null, lag: cm ? cm.lag : null });
    }
    const evs = (events || []).filter(e => { const t = Date.parse(e.created_at); return Number.isFinite(t) && t >= a.onset - 30 * 60e3 && t <= a.onset + 10 * 60e3; });
    const leader = [h, ...co.map(c => ({ s: c.s, a: c.a }))].sort((x, y) => x.a.onset - y.a.onset)[0];
    const alertOn = [s, ...co.map(c => c.s)].some(x => firing.has(String(x.key)));
    const signals = 1 + (co.length ? 1 : 0) + (evs.length ? 1 : 0) + (alertOn ? 1 : 0) - 1;   // corroboration beyond the anomaly itself
    co.forEach(c => covered.add(c.s.key));
    const mult = Math.abs(a.z);
    const parts = [`${s.title} is ${a.dir === "up" ? "above" : "below"} its own normal range (typically ${fmt(s, a.median)}, now ${fmt(s, a.latest)}, about ${mult >= 100 ? ">100" : mult.toFixed(0)}x the usual variation) since ${fmtClock(a.onset, tz)}.`];
    if (co.length) parts.push(`${co.map(c => c.s.title).join(", ")} also moved at the same time${leader.s.key !== s.key ? `; ${leader.s.title} moved first` : ""}.`);
    if (evs.length) parts.push(`${evs[0].event_type || "An event"} was recorded ${Math.round(Math.abs(Date.parse(evs[0].created_at) - a.onset) / 60000)} min ${Date.parse(evs[0].created_at) <= a.onset ? "before" : "after"} the onset.`);
    out.push({
      id: `an:${s.key}`, kind: "anomaly", level: alertOn || (s.crit != null && (s.cmp === "<" ? a.latest <= s.crit : a.latest >= s.crit)) ? "high" : "medium",
      title: `${s.title} anomaly`, text: parts.join(" "), metrics: [s.key, ...co.map(c => c.s.key)],
      confidence: confOf(signals + 1), signals: { coMoving: co.length, events: evs.length, alertFiring: alertOn },
      leader: leader.s.title, action: leader.s.key !== s.key ? `Start with ${leader.s.title}, which moved first: ${actionFor(leader.s.key)}` : actionFor(s.key), onset: a.onset,
    });
  }

  for (const { s } of per) {
    // every reachable threshold, earliest first: the warning line is usually crossed before the critical one and is the actionable one
    const reach = [["warning", s.warn], ["critical", s.crit]]
      .filter(([l, t]) => t != null && !(l === "warning" && s.crit != null && s.warn === s.crit))
      .map(([label, thr]) => ({ label, thr, p: projectCrossing(s.pts, thr, s.cmp || ">") })).filter(x => x.p)
      .sort((a, b) => a.p.hours - b.p.hours);
    if (!reach.length) continue;
    const [f, g] = reach, hh = h => (h < 1 ? `${Math.round(h * 60)} min` : `${h.toFixed(h < 10 ? 1 : 0)} h`);
    out.push({ id: `fc:${s.key}:${f.label}`, kind: "forecast", level: f.p.hours <= 24 ? "high" : "medium", title: `${s.title} trending to ${f.label}`,
      text: `If the recent trend continues, ${s.title} reaches its ${f.label} threshold (${fmt(s, f.thr)}) in about ${hh(f.p.hours)}${g ? `, and its ${g.label} threshold (${fmt(s, g.thr)}) in about ${hh(g.p.hours)}` : ""} (linear fit R\u00b2 ${f.p.r2.toFixed(2)}).`,
      metrics: [s.key], confidence: f.p.r2 >= 0.85 ? "medium" : "low", action: actionFor(s.key), hours: f.p.hours });
  }
  const rank = { high: 0, medium: 1 };
  out.sort((a, b) => (rank[a.level] - rank[b.level]) || (a.kind === b.kind ? 0 : a.kind === "anomaly" ? -1 : 1));
  return { insights: out, coverage: { analysed: analysable, total: (series || []).length } };
}
