// Cloud cost page.
//  1) Admins (usage.manage) get a Cost Explorer style view of what AWS ACTUALLY billed for CloudWatch
//     (GET /api/usage/cost-explorer): date range, daily/monthly, group by, filters, stacked cost graph, usage graph,
//     breakdown table and CSV. Charges are split into "CloudOps polling" (GetMetricData, the only thing CloudOps causes)
//     and everything else already in the account (agents, alarms, logs). Nothing here is estimated or forecast.
//  2) Below it, CloudOps's own metered reads (GET /api/usage/cloud-cost) are kept as a cross-check and for Azure/GCP,
//     which have no actual-bill source yet. That is a list-price estimate of CloudOps's own calls, not the invoice.
// Plain SVG/CSS only.
import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../api/api";
import { PageHeader, KpiCard, KpiStrip, Panel, SegmentedControl, EmptyState, Skeleton, Badge } from "../components/ui";
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


// ───────────────────────── Cost Explorer view ─────────────────────────
const PRESETS = [
  { key: "7d", label: "Last 7 days" }, { key: "30d", label: "Last 30 days" }, { key: "90d", label: "Last 90 days" },
  { key: "mtd", label: "Month to date" }, { key: "lastm", label: "Last month" }, { key: "custom", label: "Custom range" },
];
const GROUPS = [
  { key: "category", label: "CloudOps vs everything else" }, { key: "account", label: "AWS account" },
  { key: "region", label: "Region" }, { key: "usage_type", label: "Usage type" },
];
const CAT_COLOR = { cloudops_reads: "#2dd4bf", agents_alarms: "#60a5fa", logs: "#f59e0b", other: "#94a3b8" };
const PALETTE = ["#2dd4bf", "#60a5fa", "#a78bfa", "#f59e0b", "#f472b6", "#34d399"];
const ELSE_COLOR = "#64748b";
const CAT_HINT = {
  cloudops_reads: "What CloudOps itself causes: its GetMetricData polling reads. This is the only CloudWatch charge CloudOps creates (it never writes metrics, alarms or log settings).",
  agents_alarms: "Custom metrics stored, alarms and API requests (including PutMetricData from agents on your servers). They exist in the account whether or not CloudOps runs.",
  logs: "Log ingestion, storage and delivery (for example WAF, VPC or other vended logs to S3). Switched on in the AWS account by something other than CloudOps; find the source in the AWS console and turn it off if unused.",
  other: "CloudWatch usage types CloudOps does not recognise yet.",
};

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
const axisUsd = v => (v >= 100 ? `$${Math.round(v)}` : v >= 1 ? `$${+v.toFixed(2)}` : `$${+v.toFixed(3)}`);

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

function MultiSelect({ label, options, value, onChange }) {
  const toggle = v => onChange(value.includes(v) ? value.filter(x => x !== v) : [...value, v]);
  return (
    <details className="ac-dd">
      <summary>{label}{value.length ? ` (${value.length})` : ": all"}</summary>
      <div className="ac-dd-menu">
        {options.length === 0 && <span className="ac-dim">Nothing to choose yet</span>}
        {options.map(o => (
          <label key={o.value} className="ac-dd-opt">
            <input type="checkbox" checked={value.includes(o.value)} onChange={() => toggle(o.value)} /> <span>{o.label}</span>
          </label>
        ))}
        {value.length > 0 && <button type="button" className="ac-link" onClick={() => onChange([])}>Clear</button>}
      </div>
    </details>
  );
}

function downloadCsv(d) {
  const q = s => `"${String(s ?? "").replace(/"/g, '""')}"`;
  const head = ["Group", "Total USD", "Share %", "Usage", ...d.periods.map(p => p.key + (p.provisional ? " (provisional)" : ""))];
  const lines = [head.map(q).join(",")];
  d.groups.forEach(g => lines.push([q(g.label), g.total, g.share_pct, q(g.quantity != null ? `${g.quantity} ${g.unit || ""}` : ""), ...g.values].join(",")));
  lines.push([q("Total"), d.totals.billed_usd, 100, "", ...d.periods.map((_, i) => +d.groups.reduce((s, g) => s + g.values[i], 0).toFixed(4))].join(","));
  const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url; a.download = `cloudwatch-cost-${d.range.start}_to_${d.range.end}.csv`;
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}

function CostExplorer() {
  const [preset, setPreset] = useState("30d");
  const [custom, setCustom] = useState({ start: rangeFor("30d").start, end: rangeFor("30d").end });
  const [gran, setGran] = useState("daily");
  const [groupBy, setGroupBy] = useState("category");
  const [accts, setAccts] = useState([]);
  const [regs, setRegs] = useState([]);
  const [cats, setCats] = useState([]);
  const [scope, setScope] = useState("onboarded");
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [active, setActive] = useState(null);
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const range = useMemo(() => rangeFor(preset, custom), [preset, custom]);
  const query = useMemo(() => {
    const q = new URLSearchParams({ start: range.start, end: range.end, granularity: gran, group_by: groupBy, scope });
    if (accts.length) q.set("accounts", accts.join(","));
    if (regs.length) q.set("regions", regs.join(","));
    if (cats.length) q.set("categories", cats.join(","));
    return q.toString();
  }, [range, gran, groupBy, accts, regs, cats, scope]);

  useEffect(() => {
    let live = true;
    setErr(null);
    apiFetch(`/api/usage/cost-explorer?${query}`).then(r => { if (live) setD(r); }).catch(e => { if (live) setErr(e); });
    return () => { live = false; };
  }, [query, tick]);

  const refresh = async () => {
    setBusy(true); setMsg(null);
    try {
      const r = await apiFetch("/api/usage/actual-cost/refresh", { method: "POST" });
      setMsg(r.message || "Refreshed from AWS.");
      setTick(t => t + 1);
    } catch (e) { setMsg(e?.message || "Refresh failed."); } finally { setBusy(false); }
  };

  const series = useMemo(() => {
    if (!d?.groups) return [];
    const top = d.groups.slice(0, 6);
    const rest = d.groups.slice(6);
    const out = top.map((g, i) => ({ key: g.key, label: g.label, values: g.values,
      color: d.group_by === "category" ? (CAT_COLOR[g.key] || PALETTE[i]) : PALETTE[i % PALETTE.length] }));
    if (rest.length) out.push({ key: "__else", label: `Everything else (${rest.length})`, color: ELSE_COLOR,
      values: d.periods.map((_, i) => rest.reduce((s, g) => s + g.values[i], 0)) });
    return out;
  }, [d]);

  if (err) return <Panel title="Actual AWS bill: CloudWatch"><p className="ac-dim">Could not be loaded{err.status === 403 ? ": you do not have access." : ". Try again in a moment."}</p></Panel>;
  if (!d) return <Panel title="Actual AWS bill: CloudWatch"><Skeleton h={120} /></Panel>;
  if (d.state === "migration_pending") return <Panel title="Actual AWS bill: CloudWatch"><p className="ac-p">{d.notes?.[0]}</p></Panel>;
  if (d.state === "disabled") return (
    <Panel title="Actual AWS bill: CloudWatch" subtitle="What AWS really billed">
      <p className="ac-p">Not turned on. It needs the read-only permission ce:GetCostAndUsage on each AWS role and ACTUAL_COST_ENABLED=1 on the server. After the first 90-day read, each refresh costs about $0.01 per account, at most once a day.</p>
    </Panel>
  );

  const acctOpts = d.options.accounts.map(a => ({ value: a.id, label: a.name ? `${a.name} (${a.id})` : `AWS account ${a.id}` }));
  const regOpts = d.options.regions.map(r => ({ value: r, label: r }));
  const catOpts = d.options.categories.map(c => ({ value: c.key, label: c.label }));
  const hov = active != null ? d.periods[active] : null;
  const usageSeries = [{ key: "gmd", label: "Metrics requested by CloudOps", color: CAT_COLOR.cloudops_reads, values: d.gmd_usage.values }];
  const hasUsage = d.gmd_usage.values.some(v => v > 0);
  const chk = d.cloudops_check;
  const total = d.totals.billed_usd;

  return (
    <Panel title="Actual AWS bill: CloudWatch"
           subtitle="What AWS billed, straight from Cost Explorer. Nothing on this panel is estimated or forecast."
           actions={<div className="ac-actions"><button className="ac-btn" onClick={() => downloadCsv(d)}>Download CSV</button>
             <button className="ac-btn" disabled={busy} onClick={refresh}>{busy ? "Refreshing..." : "Refresh from AWS"}</button></div>}>
      {msg && <p className="ac-dim" role="status">{msg}</p>}

      <div className="ac-params" role="group" aria-label="Report parameters">
        <label className="ac-field"><span>Date range</span>
          <select value={preset} onChange={e => setPreset(e.target.value)}>{PRESETS.map(p => <option key={p.key} value={p.key}>{p.label}</option>)}</select>
        </label>
        {preset === "custom" && (
          <>
            <label className="ac-field"><span>From</span><input type="date" value={custom.start} max={custom.end} onChange={e => setCustom(c => ({ ...c, start: e.target.value }))} /></label>
            <label className="ac-field"><span>To</span><input type="date" value={custom.end} min={custom.start} onChange={e => setCustom(c => ({ ...c, end: e.target.value }))} /></label>
          </>
        )}
        <div className="ac-field"><span>Granularity</span>
          <SegmentedControl label="Granularity" options={[{ key: "daily", label: "Daily" }, { key: "monthly", label: "Monthly" }]} value={gran} onChange={setGran} /></div>
        <label className="ac-field"><span>Group by</span>
          <select value={groupBy} onChange={e => setGroupBy(e.target.value)}>{GROUPS.map(g => <option key={g.key} value={g.key}>{g.label}</option>)}</select>
        </label>
        <div className="ac-field"><span>Filters</span>
          <div className="ac-filters">
            <MultiSelect label="Account" options={acctOpts} value={accts} onChange={setAccts} />
            <MultiSelect label="Region" options={regOpts} value={regs} onChange={setRegs} />
            <MultiSelect label="Cost type" options={catOpts} value={cats} onChange={setCats} />
          </div>
        </div>
      </div>
      <p className="ac-dim">{scope === "onboarded"
        ? "Showing only the regions each account is onboarded in."
        : "Showing every region AWS billed, including ones CloudOps does not monitor."} {d.scope_note || ""}
        {" "}<button type="button" className="ac-link" onClick={() => setScope(scope === "onboarded" ? "all" : "onboarded")}>
          {scope === "onboarded" ? "Include other regions" : "Only onboarded regions"}</button></p>

      {d.reconciliation && !d.reconciliation.ok && (
        <div className="ac-warn" role="alert">The breakdowns do not add up to the same total (difference {d.reconciliation.max_difference_usd} USD). Do not rely on them until this is checked.</div>
      )}

      <KpiStrip>
        <KpiCard label="CloudOps polling, billed" tone="ok" value={usd(d.totals.cloudops_usd)}
                 sub={total ? `GetMetricData · ${(100 * d.totals.cloudops_usd / total).toFixed(1)}% of what is billed here` : "GetMetricData reads"} />
        <KpiCard label="Everything else in these accounts" value={usd(d.totals.other_usd)} sub="agents, alarms, logs: not created by CloudOps" />
        <KpiCard label="Total billed (CloudWatch)" value={usd(total)} sub={`${d.range.start} to ${d.through_date || d.range.end} · AWS lags ~48 h`} />
        <KpiCard label="Average per settled day" value={d.totals.avg_per_settled_day_usd == null ? "n/a" : usd(d.totals.avg_per_settled_day_usd)}
                 sub={`${d.totals.settled_days} settled days with charges`} />
      </KpiStrip>

      {d.groups.length === 0 ? (
        <EmptyState title="No charges in this selection" body="Change the date range or filters. Days AWS has not reported yet have no data." />
      ) : (
        <>
          <h3 className="ac-h">Cost and usage graph</h3>
          <StackedBars periods={d.periods} series={series} gran={d.granularity} active={active} onActive={setActive}
                       label={`Cost by ${d.group_by}, ${d.range.start} to ${d.range.end}`} />
          <ul className="ac-legend">
            {series.map(s => <li key={s.key}><i style={{ background: s.color }} />{s.label}</li>)}
            <li><i className="ac-prov" />* provisional (last 2 days, can still grow)</li>
          </ul>
          <div className="ac-readout" aria-live="polite">
            {hov ? (
              <>
                <strong>{hov.key}{hov.provisional ? " (provisional)" : ""}</strong>
                <span>{usd(series.reduce((s, x) => s + (x.values[active] || 0), 0))} total</span>
                {series.filter(s => (s.values[active] || 0) > 0).map(s => <span key={s.key}><i style={{ background: s.color }} />{s.label}: {usd(s.values[active])}</span>)}
              </>
            ) : <span className="ac-dim">Hover or focus a bar to see its breakdown.</span>}
          </div>

          <h3 className="ac-h">What the bill is made of</h3>
          <ul className="ac-bars">
            {d.by_category.map(c => (
              <li key={c.key} className="ac-bar-row">
                <span className="ac-bar-name">{c.label}</span>
                <svg className="ac-bar-svg" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label={`${c.share_pct}%`}>
                  <rect className="ac-bar-bg" x="0" y="0" width="100" height="8" rx="2" />
                  <rect x="0" y="0" width={Math.max(1, c.share_pct)} height="8" rx="2" fill={CAT_COLOR[c.key] || ELSE_COLOR} />
                </svg>
                <span className="ac-bar-num">{usd(c.usd)}</span>
                <span className="ac-bar-sub">{c.share_pct}%</span>
              </li>
            ))}
          </ul>
          <ul className="ac-list">{d.by_category.map(c => <li key={c.key}><strong>{c.label}:</strong> {CAT_HINT[c.key] || ""}</li>)}</ul>

          {hasUsage && (
            <>
              <h3 className="ac-h">Usage: metrics CloudOps requested (AWS-billed quantity)</h3>
              <StackedBars periods={d.periods} series={usageSeries} gran={d.granularity} fmt={compact} active={active} onActive={setActive}
                           label="GetMetricData metrics requested per period" />
              {chk ? (
                <p className={Math.abs(chk.difference_pct) <= 10 ? "ac-p" : "ac-warn"} role="status">
                  <strong>Cross-check:</strong> through {chk.through}, AWS billed {n(chk.billed_metrics)} metrics and CloudOps counted {n(chk.metered_metrics)} ({chk.difference_pct > 0 ? "+" : ""}{chk.difference_pct}%). {Math.abs(chk.difference_pct) <= 10 ? "CloudOps's own meter agrees with the bill." : "These should agree: check the collector's metering."}
                </p>
              ) : <p className="ac-dim">Cross-check against CloudOps's own meter appears when the range has settled days and metering rows within 90 days.</p>}
            </>
          )}

          <h3 className="ac-h">Cost and usage breakdown ({d.groups.length})</h3>
          <div className="ac-tablewrap">
            <table className="ac-table">
              <thead>
                <tr>
                  <th className="ac-sticky">{GROUPS.find(g => g.key === d.group_by)?.label}</th>
                  <th>Total</th><th>Share</th><th>Usage</th>
                  {d.periods.map(p => <th key={p.key}>{periodLabel(p.key, d.granularity)}{p.provisional ? "*" : ""}</th>)}
                </tr>
              </thead>
              <tbody>
                {d.groups.map(g => (
                  <tr key={g.key}>
                    <td className="ac-sticky" title={g.key}>{g.label}{d.group_by === "usage_type" && <span className="ac-dim"> · {g.regions.join(", ")} · {g.key}</span>}</td>
                    <td className="ac-num">{usd(g.total)}</td>
                    <td className="ac-num">{g.share_pct}%</td>
                    <td className="ac-num">{g.quantity != null ? `${compact(g.quantity)} ${g.unit || ""}` : "–"}</td>
                    {g.values.map((v, i) => <td key={i} className="ac-num">{v ? usd(v) : "–"}</td>)}
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <td className="ac-sticky">Total</td><td className="ac-num">{usd(total)}</td><td className="ac-num">100%</td><td />
                  {d.periods.map((_, i) => <td key={i} className="ac-num">{usd(d.groups.reduce((s, g) => s + g.values[i], 0))}</td>)}
                </tr>
              </tfoot>
            </table>
          </div>
        </>
      )}

      {d.out_of_scope.usd > 0 && (
        <p className="ac-p">
          {d.out_of_scope.hidden ? "Not shown: " : "Included above: "}<strong>{usd(d.out_of_scope.usd)}</strong> billed in regions these accounts are not onboarded in
          ({d.out_of_scope.by_region.map(r => `${r.region} ${usd(r.usd)}`).join(", ")}). CloudOps does not poll those regions, so this is not CloudOps cost.
          {d.out_of_scope.hidden && <> Billed total including it: <strong>{usd(d.reconciliation.selected_usd)}</strong>.</>}
        </p>
      )}

      {d.accounts.map(x => (
        <p key={x.account} className={x.status === "ok" ? "ac-dim" : "ac-p"}>
          {x.account}: {STATUS_TEXT[x.status] || x.status}{x.fetched_at ? `, read ${new Date(x.fetched_at).toLocaleString()}` : ""}
          {x.status !== "ok" && x.error ? ` (${x.error})` : ""}
        </p>
      ))}
      <p className="ac-dim">Cost of checking AWS: {usd(d.lookup_cost_usd)} ({n(d.lookup_requests)} Cost Explorer requests at $0.01, last 31 days).</p>
      <ul className="ac-list">{d.notes.map((t, i) => <li key={i}>{t}</li>)}</ul>
    </Panel>
  );
}

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
        subtitle="What AWS actually billed for CloudWatch, what part CloudOps causes, and how many paid reads CloudOps itself made"
      />
      {isAdmin && <CostExplorer />}
      {isAdmin ? (
        <details className="ac-fold">
          <summary>CloudOps metering: reads counted by CloudOps itself (cross-check, and the only source for Azure and GCP)</summary>
          <Metering />
        </details>
      ) : <Metering />}
    </div>
  );
}
