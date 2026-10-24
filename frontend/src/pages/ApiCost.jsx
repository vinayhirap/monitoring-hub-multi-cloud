// Cloud cost page.
// Admins (usage.manage) see what CloudOps's own metric polling actually cost on AWS: the GetMetricData charge, as billed
// (GET /api/usage/cost-explorer, Cost Explorer), by day/month, account, region and usage type. Nothing is estimated or
// forecast, and charges CloudOps does not cause (agents, alarms, logs) are not shown, only footnoted as a total.
// Everyone else (usage.view) keeps the CloudOps metering view (list-price estimate of its own reads). Plain SVG/CSS only.
import { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "../api/api";
import { PageHeader, KpiCard, KpiStrip, Panel, SegmentedControl, EmptyState, Skeleton } from "../components/ui";
import { useAuth } from "../auth/AuthContext";
import "./ApiCost.css";

const RANGES = [{ key: 1, label: "24 hours" }, { key: 7, label: "7 days" }, { key: 30, label: "30 days" }];
const CLOUD = { aws: "AWS", azure: "Azure", gcp: "GCP" };
const UNIT_LABEL = { metrics_requested: "metrics requested", api_calls: "API calls", time_series_returned: "series returned", units: "units" };
const TIER_LABEL = { critical: "Critical (2 min)", standard: "Standard (5 min)", low: "Low (15 min)", extended: "Extended", slow_extended: "Daily", tag_sync: "Tag sync", ui: "Pages opened by users", discovery: "Metric discovery" };

const STATUS_TEXT = {
  ok: "up to date",
  permission_missing: "permission missing: add ce:GetCostAndUsage to the role",
  not_enabled: "Cost Explorer is not enabled in this account yet",
  error: "could not read",
};

const n = v => Number(v || 0).toLocaleString();
const usd = v => `$${Number(v || 0).toFixed(Number(v) >= 1 ? 2 : 4)}`;

function ago(minutes) {
  if (minutes == null) return "never";
  if (minutes < 1) return "just now";
  if (minutes < 120) return `${minutes} min ago`;
  return `${Math.round(minutes / 60)} h ago`;
}

function TierBars({ tiers }) {
  const max = Math.max(1, ...tiers.map(t => t.units));
  return (
    <ul className="ac-bars">
      {tiers.map((t, i) => (
        <li key={`${t.tier}-${t.source || ""}-${t.operation || ""}-${i}`} className="ac-bar-row">
          <span className="ac-bar-name">{TIER_LABEL[t.tier] || t.tier}{t.source && t.source !== t.tier && t.source !== "collector" ? ` · ${t.source}` : ""}{t.operation ? ` · ${t.operation}` : ""}{t.unit === "requests" ? " (requests)" : ""}</span>
          <svg className="ac-bar-svg" viewBox="0 0 100 8" preserveAspectRatio="none" role="img"
               aria-label={`${n(t.units)} units, ${t.share_of_units_pct}% of this cloud`}>
            <rect className="ac-bar-bg" x="0" y="0" width="100" height="8" rx="2" />
            {t.units > 0 && <rect className={t.free ? "ac-bar-free" : "ac-bar-fill"} x="0" y="0" width={Math.max(1, (100 * t.units) / max)} height="8" rx="2" />}
          </svg>
          <span className="ac-bar-num">
            {t.free ? <Badge tone="ok" title={t.free_reason}>free</Badge> : <>{n(t.units)} <span className="ac-dim">({t.share_of_units_pct}%)</span></>}
          </span>
          <span className="ac-bar-sub">{n(t.calls)} calls{!t.free && (t.priced === false ? " · no price set" : ` · ${usd(t.list_price_usd)}`)}</span>
        </li>
      ))}
    </ul>
  );
}

function Allowance({ a }) {
  if (!a) return null;
  const pct = Math.min(100, a.percent_used);
  return (
    <div className="ac-allow">
      <div className="ac-allow-head">
        <strong>Free allowance this month</strong>
        <span>{n(a.month_to_date)} of {n(a.limit)} {UNIT_LABEL[a.unit] || a.unit} ({a.percent_used}%)</span>
      </div>
      <svg className="ac-bar-svg" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label={`${a.percent_used}% of one free allowance used`}>
        <rect className="ac-bar-bg" x="0" y="0" width="100" height="8" rx="2" />
        <rect className={a.over_single_allowance ? "ac-bar-over" : "ac-bar-fill"} x="0" y="0" width={Math.max(a.month_to_date > 0 ? 1 : 0, pct)} height="8" rx="2" />
      </svg>
      <p className="ac-dim">Projected for the month: {n(a.projected_month_end)} ({a.projected_percent}%). {a.caveat}</p>
    </div>
  );
}



// ───────────────────────── CloudOps cost (billed) ─────────────────────────
const PRESETS = [
  { key: "7d", label: "7 days" }, { key: "30d", label: "30 days" }, { key: "90d", label: "90 days" },
  { key: "mtd", label: "This month" }, { key: "lastm", label: "Last month" }, { key: "custom", label: "Custom" },
];
const GROUPS = [{ key: "account", label: "AWS account", noun: "account" }, { key: "region", label: "Region", noun: "region" }, { key: "usage_type", label: "Usage type", noun: "usage type" }];
const PALETTE = ["#2dd4bf", "#60a5fa", "#a78bfa", "#f59e0b", "#f472b6", "#34d399"];
const ELSE_COLOR = "#64748b";

const isoDay = d => d.toISOString().slice(0, 10);
const addDays = (d, k) => new Date(d.getTime() + k * 86400000);
function rangeFor(preset, custom) {
  const now = new Date();
  const today = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  if (preset === "7d") return { start: isoDay(addDays(today, -6)), end: isoDay(today) };
  if (preset === "30d") return { start: isoDay(addDays(today, -29)), end: isoDay(today) };
  if (preset === "90d") return { start: isoDay(addDays(today, -89)), end: isoDay(today) };
  if (preset === "mtd") return { start: isoDay(new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1))), end: isoDay(today) };
  if (preset === "lastm") {
    const first = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth() - 1, 1));
    const last = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 0));
    return { start: isoDay(first), end: isoDay(last) };
  }
  return custom;
}
const compact = v => new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(v || 0);
const periodLabel = (k, gran) => (gran === "monthly" ? k : k.slice(5));

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  const f = v / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}
const axisUsd = v => (v === 0 ? "$0" : v >= 100 ? `$${Math.round(v)}` : v >= 10 ? `$${v.toFixed(0)}` : v >= 0.1 ? `$${v.toFixed(2)}` : `$${v.toFixed(3)}`);

// Stacked bars. series: [{key,label,color,values[]}], periods: [{key,provisional}]. Provisional periods are drawn lighter.
function StackedBars({ periods, series, gran, fmt = axisUsd, active, onActive, label }) {
  const W = 1000, H = 300, L = 58, R = 8, T = 10, B = 30;
  const n = Math.max(1, periods.length);
  const totals = periods.map((_, i) => series.reduce((s, x) => s + (x.values[i] || 0), 0));
  const top = niceMax(Math.max(0, ...totals));
  const pw = (W - L - R) / n, bw = Math.max(2, pw * 0.72);
  const y = v => T + (H - T - B) * (1 - v / top);
  const step = Math.max(1, Math.ceil(n / 12));
  return (
    <svg className="ac-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label} onMouseLeave={() => onActive(null)}>
      {[0, 1, 2, 3, 4].map(k => {
        const v = (top * k) / 4;
        return (
          <g key={k}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} className="ac-grid" />
            <text x={L - 6} y={y(v) + 4} textAnchor="end" className="ac-axis">{fmt(v)}</text>
          </g>
        );
      })}
      {periods.map((p, i) => {
        let acc = 0;
        const x = L + i * pw + (pw - bw) / 2;
        return (
          <g key={p.key} tabIndex={0} onMouseEnter={() => onActive(i)} onFocus={() => onActive(i)} className={active === i ? "ac-bar-on" : undefined}>
            <rect x={L + i * pw} y={T} width={pw} height={H - T - B} fill="transparent" />
            {series.map(s => {
              const v = s.values[i] || 0;
              if (v <= 0) return null;
              const y1 = y(acc + v), y0 = y(acc);
              acc += v;
              return <rect key={s.key} x={x} y={y1} width={bw} height={Math.max(0.5, y0 - y1)} fill={s.color} fillOpacity={p.provisional ? 0.45 : 1} />;
            })}
            {i % step === 0 && <text x={x + bw / 2} y={H - 10} textAnchor="middle" className="ac-axis">{periodLabel(p.key, gran)}{p.provisional ? "*" : ""}</text>}
          </g>
        );
      })}
    </svg>
  );
}

// One filter button + popover. Only one is open at a time (parent owns `open`), it closes on outside click or Escape,
// has search when the list is long, select all / clear, and shows the cost next to every choice.
function FilterPopover({ id, label, options, value, onChange, open, setOpen }) {
  const [q, setQ] = useState("");
  const ref = useRef(null);
  const isOpen = open === id;
  useEffect(() => {
    if (!isOpen) { setQ(""); return undefined; }
    const away = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(null); };
    const esc = e => { if (e.key === "Escape") setOpen(null); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [isOpen, setOpen]);
  const shown = options.filter(o => o.label.toLowerCase().includes(q.trim().toLowerCase()));
  const toggle = v => onChange(value.includes(v) ? value.filter(x => x !== v) : [...value, v]);
  const names = options.filter(o => value.includes(o.value)).map(o => o.short || o.label);
  const summary = value.length === 0 ? "All" : names.length <= 2 ? names.join(", ") : `${names.length} selected`;
  return (
    <div className="ac-pop" ref={ref}>
      <button type="button" className={`ac-chipbtn${value.length ? " is-set" : ""}`} aria-haspopup="listbox" aria-expanded={isOpen}
              onClick={() => setOpen(isOpen ? null : id)}>
        <span className="ac-chipbtn-k">{label}</span><span className="ac-chipbtn-v">{summary}</span><span aria-hidden="true">▾</span>
      </button>
      {isOpen && (
        <div className="ac-pop-menu" role="listbox" aria-label={label} aria-multiselectable="true">
          {options.length > 6 && <input className="ac-pop-search" autoFocus placeholder={`Search ${label.toLowerCase()}`} value={q} onChange={e => setQ(e.target.value)} />}
          <div className="ac-pop-tools">
            <button type="button" className="ac-link" onClick={() => onChange(options.map(o => o.value))}>Select all</button>
            <button type="button" className="ac-link" onClick={() => onChange([])}>Clear</button>
          </div>
          {options.length === 0 && <span className="ac-dim">Nothing billed to choose from in this range</span>}
          {shown.map(o => (
            <label key={o.value} className="ac-pop-opt">
              <input type="checkbox" checked={value.includes(o.value)} onChange={() => toggle(o.value)} />
              <span className="ac-pop-name">{o.label}</span>
              {o.hint && <span className="ac-pop-hint">{o.hint}</span>}
            </label>
          ))}
          {shown.length === 0 && options.length > 0 && <span className="ac-dim">No match</span>}
        </div>
      )}
    </div>
  );
}

function downloadCsv(d) {
  const q = s => `"${String(s ?? "").replace(/"/g, '""')}"`;
  const head = ["Group", "Total USD", "Share %", "Metrics requested", ...d.periods.map(p => p.key + (p.provisional ? " (provisional)" : ""))];
  const lines = [head.map(q).join(",")];
  d.groups.forEach(g => lines.push([q(g.label), g.total, g.share_pct, g.quantity ?? "", ...g.values].join(",")));
  lines.push([q("Total"), d.totals.billed_usd, 100, d.totals.metrics_requested ?? "", ...d.periods.map((_, i) => +d.groups.reduce((s, g) => s + g.values[i], 0).toFixed(4))].join(","));
  const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url; a.download = `cloudops-polling-cost-${d.range.start}_to_${d.range.end}.csv`;
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}

function CostExplorer() {
  const [preset, setPreset] = useState("30d");
  const [custom, setCustom] = useState({ start: rangeFor("30d").start, end: rangeFor("30d").end });
  const [gran, setGran] = useState("daily");
  const [groupBy, setGroupBy] = useState("account");
  const [accts, setAccts] = useState([]);
  const [regs, setRegs] = useState([]);
  const [open, setOpen] = useState(null);
  const [hidden, setHidden] = useState([]);
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);
  const [active, setActive] = useState(null);
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const range = useMemo(() => rangeFor(preset, custom), [preset, custom]);
  const query = useMemo(() => {
    const q = new URLSearchParams({ start: range.start, end: range.end, granularity: gran, group_by: groupBy, cloudops_only: "true" });
    if (accts.length) q.set("accounts", accts.join(","));
    if (regs.length) q.set("regions", regs.join(","));
    return q.toString();
  }, [range, gran, groupBy, accts, regs]);

  useEffect(() => {
    let live = true;
    setErr(null); setLoading(true);
    apiFetch(`/api/usage/cost-explorer?${query}`)
      .then(r => { if (live) { setD(r); setHidden([]); } })
      .catch(e => { if (live) setErr(e); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [query, tick]);

  const refresh = async () => {
    setBusy(true); setMsg(null);
    try {
      const r = await apiFetch("/api/usage/actual-cost/refresh", { method: "POST" });
      setMsg(r.message || "Read the latest figures from AWS.");
      setTick(t => t + 1);
    } catch (e) { setMsg(e?.detail || e?.message || "Refresh failed."); } finally { setBusy(false); }
  };

  const allSeries = useMemo(() => {
    if (!d?.groups) return [];
    const top = d.groups.slice(0, 6), rest = d.groups.slice(6);
    const out = top.map((g, i) => ({ key: g.key, label: g.label, values: g.values, color: PALETTE[i % PALETTE.length] }));
    if (rest.length) out.push({ key: "__else", label: `Everything else (${rest.length})`, color: ELSE_COLOR,
      values: d.periods.map((_, i) => rest.reduce((s, g) => s + g.values[i], 0)) });
    return out;
  }, [d]);
  const series = allSeries.filter(s => !hidden.includes(s.key));

  if (err && !d) return <Panel title="CloudOps polling cost"><p className="ac-dim">Could not be loaded{err.status === 403 ? ": you do not have access." : ". Try again in a moment."}</p></Panel>;
  if (!d) return <Panel title="CloudOps polling cost"><Skeleton h={120} /></Panel>;
  if (d.state === "migration_pending") return <Panel title="CloudOps polling cost"><p className="ac-p">{d.notes?.[0]}</p></Panel>;
  if (d.state === "disabled") return (
    <Panel title="CloudOps polling cost" subtitle="What AWS really billed for CloudOps's reads">
      <p className="ac-p">Not turned on. It needs the read-only permission ce:GetCostAndUsage on each AWS role and ACTUAL_COST_ENABLED=1 on the server. After the first 90-day read, each refresh costs about $0.01 per account, at most once a day.</p>
    </Panel>
  );

  const acctOpts = d.options.accounts.map(a => ({ value: a.id, label: a.name ? `${a.name} (${a.id})` : `AWS account ${a.id}`, short: a.name || a.id, hint: usd(a.usd) }));
  const regOpts = d.options.regions.map(r => ({ value: r, label: r, hint: usd(d.options.region_usd?.[r]) }));
  const chips = [
    ...accts.map(v => ({ k: `a:${v}`, label: acctOpts.find(o => o.value === v)?.short || v, off: () => setAccts(accts.filter(x => x !== v)) })),
    ...regs.map(v => ({ k: `r:${v}`, label: v, off: () => setRegs(regs.filter(x => x !== v)) })),
  ];
  const hov = active != null ? d.periods[active] : null;
  const total = d.totals.billed_usd;
  const hasUsage = d.gmd_usage.values.some(v => v > 0);
  const chk = d.cloudops_check;
  const sel = (id, v) => (id === "a" ? setAccts(v) : setRegs(v));
  const ok = x => x.status === "ok";
  const newest = d.accounts.map(x => x.fetched_at).filter(Boolean).sort().pop();
  const bad = d.accounts.filter(x => !ok(x));

  return (
    <Panel title="CloudOps polling cost, billed by AWS"
           subtitle="What CloudOps's own GetMetricData reads cost, straight from Cost Explorer. Nothing here is estimated or forecast."
           actions={<div className="ac-actions"><button className="ac-btn" onClick={() => downloadCsv(d)}>Download CSV</button>
             <button className="ac-btn" disabled={busy} onClick={refresh}>{busy ? "Reading from AWS..." : "Refresh from AWS"}</button></div>}>
      {msg && <p className="ac-dim" role="status">{msg}</p>}

      <div className="ac-bar" role="group" aria-label="Report parameters">
        <SegmentedControl label="Date range" options={PRESETS} value={preset} onChange={setPreset} />
        {preset === "custom" && (
          <span className="ac-dates">
            <input type="date" aria-label="From" value={custom.start} max={custom.end} onChange={e => setCustom(c => ({ ...c, start: e.target.value }))} />
            <span className="ac-dim">to</span>
            <input type="date" aria-label="To" value={custom.end} min={custom.start} onChange={e => setCustom(c => ({ ...c, end: e.target.value }))} />
          </span>
        )}
        <SegmentedControl label="Granularity" options={[{ key: "daily", label: "Daily" }, { key: "monthly", label: "Monthly" }]} value={gran} onChange={setGran} />
        <SegmentedControl label="Group by" options={GROUPS} value={groupBy} onChange={setGroupBy} />
        <FilterPopover id="a" label="Account" options={acctOpts} value={accts} onChange={v => sel("a", v)} open={open} setOpen={setOpen} />
        <FilterPopover id="r" label="Region" options={regOpts} value={regs} onChange={v => sel("r", v)} open={open} setOpen={setOpen} />
        {loading && <span className="ac-dim" role="status">Updating...</span>}
      </div>
      {chips.length > 0 && (
        <div className="ac-chips" aria-label="Active filters">
          {chips.map(c => <button key={c.k} type="button" className="ac-chip" onClick={c.off} aria-label={`Remove filter ${c.label}`}>{c.label} <span aria-hidden="true">×</span></button>)}
          <button type="button" className="ac-link" onClick={() => { setAccts([]); setRegs([]); }}>Clear all</button>
        </div>
      )}

      <div className={loading ? "ac-dimmed" : undefined}>
        <KpiStrip>
          <KpiCard label="CloudOps polling, billed" tone="ok" value={usd(total)} sub={`${d.range.start} to ${d.through_date || d.range.end} · AWS lags ~48 h`} />
          <KpiCard label="Average per settled day" value={d.totals.avg_per_settled_day_usd == null ? "n/a" : usd(d.totals.avg_per_settled_day_usd)}
                   sub={`${d.totals.settled_days} settled days`} />
          <KpiCard label="Metrics requested" value={d.totals.metrics_requested == null ? "n/a" : n(d.totals.metrics_requested)}
                   sub={d.totals.usd_per_1000_metrics == null ? "fills in after the next refresh from AWS" : `AWS rate: ${usd(d.totals.usd_per_1000_metrics)} per 1,000 metrics`} />
          <KpiCard label="CloudOps counter vs AWS bill" tone={chk && Math.abs(chk.difference_pct) > 10 ? "warn" : undefined}
                   value={chk ? `${chk.difference_pct > 0 ? "+" : ""}${chk.difference_pct}%` : "n/a"}
                   sub={chk ? `counted ${n(chk.metered_metrics)} · AWS billed ${n(chk.billed_metrics)} (to ${chk.through})` : "needs settled days and refreshed usage"} />
        </KpiStrip>

        {d.groups.length === 0 ? (
          <EmptyState title="No CloudOps polling charges in this selection" body="Change the date range or filters. Days AWS has not reported yet have no data." />
        ) : (
          <>
            <h3 className="ac-h">Cost by {GROUPS.find(g => g.key === d.group_by)?.noun}</h3>
            <StackedBars periods={d.periods} series={series} gran={d.granularity} active={active} onActive={setActive}
                         label={`CloudOps polling cost by ${d.group_by}, ${d.range.start} to ${d.range.end}`} />
            <ul className="ac-legend">
              {allSeries.map(s => (
                <li key={s.key}>
                  <button type="button" className={`ac-legbtn${hidden.includes(s.key) ? " is-off" : ""}`} aria-pressed={!hidden.includes(s.key)}
                          title="Click to show or hide" onClick={() => setHidden(h => (h.includes(s.key) ? h.filter(x => x !== s.key) : [...h, s.key]))}>
                    <i style={{ background: s.color }} />{s.label}
                  </button>
                </li>
              ))}
              <li><i className="ac-prov" />* provisional (last 2 days, can still grow)</li>
            </ul>
            <div className="ac-readout" aria-live="polite">
              {hov ? (
                <>
                  <strong>{hov.key}{hov.provisional ? " (provisional)" : ""}</strong>
                  <span>{usd(series.reduce((s, x) => s + (x.values[active] || 0), 0))} total</span>
                  {series.filter(s => (s.values[active] || 0) > 0).map(s => <span key={s.key}><i style={{ background: s.color }} />{s.label}: {usd(s.values[active])}</span>)}
                </>
              ) : <span className="ac-dim">Hover or focus a bar to see its breakdown. Click a legend item to hide it.</span>}
            </div>

            {hasUsage && (
              <>
                <h3 className="ac-h">Metrics CloudOps requested (quantity AWS billed)</h3>
                <StackedBars periods={d.periods} series={[{ key: "gmd", label: "Metrics requested", color: PALETTE[0], values: d.gmd_usage.values }]}
                             gran={d.granularity} fmt={compact} active={active} onActive={setActive} label="Metrics requested per period" />
              </>
            )}

            <h3 className="ac-h">Breakdown ({d.groups.length})</h3>
            <div className="ac-tablewrap">
              <table className="ac-table">
                <thead>
                  <tr>
                    <th className="ac-sticky">{GROUPS.find(g => g.key === d.group_by)?.label}</th>
                    <th>Total</th><th>Share</th><th>Metrics</th>
                    {d.periods.map(p => <th key={p.key}>{periodLabel(p.key, d.granularity)}{p.provisional ? "*" : ""}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {d.groups.map(g => (
                    <tr key={g.key}>
                      <td className="ac-sticky" title={g.key}>{g.label}{d.group_by === "usage_type" && <span className="ac-dim"> · {g.regions.join(", ")} · {g.key}</span>}</td>
                      <td className="ac-num">{usd(g.total)}</td>
                      <td className="ac-num">{g.share_pct}%</td>
                      <td className="ac-num">{g.quantity != null ? n(Math.round(g.quantity)) : "–"}</td>
                      {g.values.map((v, i) => <td key={i} className="ac-num">{v ? usd(v) : "–"}</td>)}
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <td className="ac-sticky">Total</td><td className="ac-num">{usd(total)}</td><td className="ac-num">100%</td>
                    <td className="ac-num">{d.totals.metrics_requested != null ? n(d.totals.metrics_requested) : "–"}</td>
                    {d.periods.map((_, i) => <td key={i} className="ac-num">{usd(d.groups.reduce((s, g) => s + g.values[i], 0))}</td>)}
                  </tr>
                </tfoot>
              </table>
            </div>
          </>
        )}
      </div>

      {bad.map(x => <p key={x.account} className="ac-p">{x.account}: {STATUS_TEXT[x.status] || x.status}{x.error ? ` (${x.error})` : ""}</p>)}
      <p className="ac-dim ac-foot">
        Last read from AWS: {newest ? new Date(newest).toLocaleString() : "not yet"} · cost of checking {usd(d.lookup_cost_usd)} ({n(d.lookup_requests)} Cost Explorer requests at $0.01, last 31 days).
        {d.excluded?.usd > 0 && <> Not shown: {usd(d.excluded.usd)} of other CloudWatch charges in {accts.length ? "the selected" : "these"} accounts that CloudOps does not create (custom metrics, alarms, logs, API requests from agents).</>}
        {" "}Today and yesterday are provisional. Tax (for example GST) is billed separately and is not included.
        {chk && <> <strong>Counter check:</strong> CloudOps keeps its own count of the metrics it asks for and compares it with the metrics AWS billed on settled days. AWS is the truth. A negative gap means CloudOps's counter missed some reads (it is held in memory and was lost on a restart or crash); a positive gap would mean it double counted. Within 10% the bill and the counter agree.</>}
      </p>
    </Panel>
  );
}

// ───────────────────────── CloudOps metering (estimate, non-admin view) ─────────────────────────
// ───────────────────────── CloudOps metering (estimate) ─────────────────────────
function Metering() {
  const [days, setDays] = useState(7);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let live = true;
    setData(null); setError(null);
    apiFetch(`/api/usage/cloud-cost?days=${days}`)
      .then(d => { if (live) setData(d); })
      .catch(e => { if (live) setError(e); });
    return () => { live = false; };
  }, [days]);

  const fr = data?.freshness;
  return (
    <div className="ac-metering">
      <div className="ac-metering-head"><SegmentedControl label="Range" options={RANGES} value={days} onChange={setDays} /></div>
      <div className="ac-banner" role="note">
        <strong>List-price estimate of CloudOps's own reads, not the invoice.</strong> It does not include custom metrics, logs, alarms or other callers, so it is not a ceiling for the AWS bill. Azure and GCP have no actual-bill source yet, so for them this is the only figure.
      </div>

      {error && <EmptyState title="Usage could not be loaded" body={error.status === 403 ? "You do not have access to this page." : "Try again in a moment."} />}
      {!error && !data && <div className="ac-loading"><Skeleton h={64} /><Skeleton h={120} /></div>}

      {data && (
        <>
          {fr.status === "delayed" && <div className="ac-warn" role="alert">No usage flush for {ago(fr.minutes_since)}. Figures after that are missing, so the newest numbers are too low.</div>}

          <KpiStrip>
            <KpiCard label="CloudOps reads, est. list price" value={usd(data.totals.list_price_usd)} sub={`last ${days === 1 ? "24 hours" : `${days} days`}, its own calls only`} />
            <KpiCard label="Metric read calls" value={n(data.totals.calls)} sub="all clouds, incl. free tag sync" />
            <KpiCard label="Last usage flush" value={ago(fr.minutes_since)} tone={fr.status === "ok" ? "ok" : "warn"}
                     sub={fr.status === "none" ? "no usage recorded in this range" : `${fr.hours_with_flush} of ${fr.hours_in_window} hours had a flush`} />
          </KpiStrip>

          {data.providers.length === 0 && (
            <EmptyState title="No paid metric reads recorded in this range"
                        body="Usage is written once an hour by the collector that makes the calls. A server that makes no paid calls (for example DEV) shows nothing here." />
          )}

          {data.providers.map(p => (
            <Panel key={p.provider} title={CLOUD[p.provider] || p.provider}
                   subtitle={`${n(p.units)} ${UNIT_LABEL[p.unit] || p.unit} · ${n(p.calls)} calls · ${usd(p.list_price_usd)} list price`}>
              {!p.known_provider && <p className="ac-dim">Unrecognised cloud: counts are shown, no price is estimated.</p>}
              <TierBars tiers={p.tiers} />
              <Allowance a={p.free_allowance} />
            </Panel>
          ))}

                    {data.not_metered.length > 0 && (
            <Panel title="Not metered yet" subtitle="These paid reads are not counted anywhere, so the totals above exclude them">
              <ul className="ac-list">{data.not_metered.map(s => <li key={s}>{s}</li>)}</ul>
            </Panel>
          )}

          <Panel title="Free and paid">
            <ul className="ac-list">
              <li><strong>Free:</strong> resource Describe and List calls, the Resource Groups Tagging API, STS and CloudTrail LookupEvents. They can run as often as needed.</li>
              <li><strong>Paid:</strong> CloudWatch GetMetricData (per metric requested), ListMetrics and GetMetricStatistics; Azure Monitor (per 1,000 calls after 1M free per subscription per month); GCP ListTimeSeries (per series after 1M free per billing account per month).</li>
              <li><strong>Polling follows the metric, not the alert priority:</strong> each metric is read on its own tier (2, 5, 15, 60 minutes or daily).</li>
            </ul>
          </Panel>

          <Panel title={`Environment polling is ${data.env_polling.enabled ? "ON" : "OFF"}`}>
            <p className="ac-p">{data.env_polling.note}</p>
            <p className="ac-dim">{data.env_polling.scope}</p>
          </Panel>

          <Panel title="How to read these numbers">
            <ul className="ac-list">{data.notes.map((t, i) => <li key={i}>{t}</li>)}</ul>
            {fr.last_flush_at && <p className="ac-dim">Newest flush: {new Date(fr.last_flush_at).toLocaleString()}</p>}
          </Panel>
        </>
      )}
    </div>
  );
}


export default function ApiCost() {
  const { hasPermission } = useAuth();
  const isAdmin = hasPermission("usage.manage");      // the actual AWS bill is admin-only (usage.manage)
  return (
    <div className="page ac-page">
      <PageHeader
        title="Cloud cost"
        subtitle={isAdmin ? "What CloudOps's own metric polling actually costs on AWS, as billed" : "How many paid metric reads CloudOps made, from the usage it measured itself"}
      />
      {isAdmin ? <CostExplorer /> : <Metering />}
    </div>
  );
}
