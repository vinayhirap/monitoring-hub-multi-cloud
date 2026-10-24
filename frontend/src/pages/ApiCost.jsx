// Cloud cost page. Admins see, FIRST, what AWS actually billed (GET /api/usage/actual-cost, Cost Explorer), account by
// account. Below it, CloudOps's own metered reads (GET /api/usage/cloud-cost, op_events metric_api_usage) are a LIST-PRICE
// ESTIMATE of CloudOps's own calls only: not the invoice and not a ceiling for it. Plain SVG/CSS only.
// A source that is not metered yet is named as such; it is never drawn as a zero.
import { useEffect, useState } from "react";
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

function ActualCost({ days }) {
  const [a, setA] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let live = true;
    setA(null); setErr(null);
    apiFetch(`/api/usage/actual-cost?days=${days}`).then(d => { if (live) setA(d); }).catch(e => { if (live) setErr(e); });
    return () => { live = false; };
  }, [days, tick]);

  const refresh = async () => {
    setBusy(true); setMsg(null);
    try {
      const r = await apiFetch("/api/usage/actual-cost/refresh", { method: "POST" });
      setMsg(r.message || "Refreshed.");
      setTick(t => t + 1);
    } catch (e) {
      setMsg(e?.message || "Refresh failed.");
    } finally { setBusy(false); }
  };

  if (err) return <Panel title="Actual AWS bill: CloudWatch"><p className="ac-dim">Could not be loaded.</p></Panel>;
  if (!a) return <Panel title="Actual AWS bill: CloudWatch"><Skeleton h={64} /></Panel>;
  if (a.state === "migration_pending") return <Panel title="Actual AWS bill: CloudWatch"><p className="ac-p">{a.notes?.[0]}</p></Panel>;
  if (a.state === "disabled") return (
    <Panel title="Actual AWS bill: CloudWatch" subtitle="What AWS really billed for CloudWatch">
      <p className="ac-p">Not turned on. It needs the read-only permission ce:GetCostAndUsage on each AWS role and ACTUAL_COST_ENABLED=1 on the server. Each refresh costs $0.01 per account, at most once a day.</p>
      <p className="ac-dim">Setup steps: {a.setup_doc}</p>
    </Panel>
  );

  const hasData = a.by_usage_type?.length > 0;
  const max = Math.max(0.0001, ...(a.daily || []).map(d => d.usd));
  const accts = a.accounts_cost || [];
  return (
    <Panel title="Actual AWS bill: CloudWatch" subtitle="What AWS billed, read from Cost Explorer: the whole account, not only CloudOps's own calls"
           actions={<button className="ac-btn" disabled={busy} onClick={refresh}>{busy ? "Refreshing..." : "Refresh now"}</button>}>
      {msg && <p className="ac-dim" role="status">{msg}</p>}
      {a.reconciliation && !a.reconciliation.ok && (
        <div className="ac-warn" role="alert">The breakdowns below do not add up to the same total (difference {a.reconciliation.max_difference_usd} USD). Do not rely on them until this is checked.</div>
      )}
      <KpiStrip>
        <KpiCard label="Actual billed" value={hasData ? usd(a.total_usd) : "no data yet"}
                 sub={a.window_start ? `${a.window_start} to ${a.through_date || a.window_end} (AWS lags up to ~48 h)` : "waiting for the first Cost Explorer read"} />
        <KpiCard label="AWS accounts billed" value={n(accts.length)} sub="see each account below" />
        <KpiCard label="Cost of checking" value={usd(a.lookup_cost_usd)} sub={`${n(a.lookup_requests)} Cost Explorer requests at $0.01`} />
      </KpiStrip>

      {a.accounts.map(x => (
        <p key={x.account} className={x.status === "ok" ? "ac-dim" : "ac-p"}>
          {x.account}: {STATUS_TEXT[x.status] || x.status}{x.fetched_at ? `, read ${new Date(x.fetched_at).toLocaleString()}` : ""}
          {x.status !== "ok" && x.error ? ` (${x.error})` : ""}
        </p>
      ))}

      {hasData && (
        <>
          <h3 className="ac-h">Account-wise</h3>
          {accts.map(ac => {
            const top = Math.max(0.0001, ...ac.by_usage_type.map(u => u.usd));
            const dmax = Math.max(0.0001, ...ac.daily.map(d => d.usd));
            return (
              <section key={ac.billed_account_id} className="ac-acct" aria-label={`AWS account ${ac.name || ac.billed_account_id}`}>
                <header className="ac-acct-head">
                  <div>
                    <strong>{ac.name || "Unnamed account"}</strong>
                    <span className="ac-dim"> · AWS account {ac.billed_account_id}{!ac.name && " (not one of the monitored accounts)"}</span>
                  </div>
                  <div className="ac-acct-total">{usd(ac.usd)} <span className="ac-dim">({ac.share_pct}% of total)</span></div>
                </header>
                <p className="ac-dim">By region: {ac.by_region.map(r => `${r.region} ${usd(r.usd)}`).join(" · ")}</p>
                <ul className="ac-bars">
                  {ac.by_usage_type.map(u => (
                    <li key={u.usage_type} className="ac-bar-row">
                      <span className="ac-bar-name">{u.label}</span>
                      <svg className="ac-bar-svg" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label={usd(u.usd)}>
                        <rect className="ac-bar-bg" x="0" y="0" width="100" height="8" rx="2" />
                        <rect className="ac-bar-fill" x="0" y="0" width={Math.max(1, (100 * u.usd) / top)} height="8" rx="2" />
                      </svg>
                      <span className="ac-bar-num">{usd(u.usd)}</span>
                      <span className="ac-bar-sub">{u.region} · {u.usage_type}</span>
                    </li>
                  ))}
                </ul>
                <ul className="ac-bars" aria-label={`Daily actual charge, ${ac.name || ac.billed_account_id}`}>
                  {ac.daily.map(d => (
                    <li key={d.date} className="ac-bar-row">
                      <span className="ac-bar-name">{d.date}</span>
                      <svg className="ac-bar-svg" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label={usd(d.usd)}>
                        <rect className="ac-bar-bg" x="0" y="0" width="100" height="8" rx="2" />
                        <rect className={d.provisional ? "ac-bar-free" : "ac-bar-fill"} x="0" y="0" width={Math.max(1, (100 * d.usd) / dmax)} height="8" rx="2" />
                      </svg>
                      <span className="ac-bar-num">{usd(d.usd)}</span>
                      <span className="ac-bar-sub">{d.provisional ? "provisional, may still grow" : "settled"}</span>
                    </li>
                  ))}
                </ul>
              </section>
            );
          })}

          <h3 className="ac-h">All accounts, by day</h3>
          <ul className="ac-bars" aria-label="Daily actual charge, all accounts">
            {a.daily.map(d => (
              <li key={d.date} className="ac-bar-row">
                <span className="ac-bar-name">{d.date}</span>
                <svg className="ac-bar-svg" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label={usd(d.usd)}>
                  <rect className="ac-bar-bg" x="0" y="0" width="100" height="8" rx="2" />
                  <rect className={d.provisional ? "ac-bar-free" : "ac-bar-fill"} x="0" y="0" width={Math.max(1, (100 * d.usd) / max)} height="8" rx="2" />
                </svg>
                <span className="ac-bar-num">{usd(d.usd)}</span>
                <span className="ac-bar-sub">{d.provisional ? "provisional, may still grow" : "settled"}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      <ul className="ac-list">{a.notes.map((t, i) => <li key={i}>{t}</li>)}</ul>
    </Panel>
  );
}

export default function ApiCost() {
  const { hasPermission } = useAuth();
  const isAdmin = hasPermission("usage.manage");      // actual AWS charge is admin-only (usage.manage)
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
    <div className="page ac-page">
      <PageHeader
        title="Cloud cost"
        subtitle="What AWS actually billed, account by account, and how many paid metric reads CloudOps itself made"
        actions={<SegmentedControl label="Range" options={RANGES} value={days} onChange={setDays} />}
      />
      <div className="ac-banner" role="note">
        <strong>Two different things.</strong> "Actual AWS bill" (admins, first panel) is what AWS charged. Everything under it is an estimate of CloudOps's own metered reads only (list price, free tiers ignored): not the invoice, and not a ceiling for it. Azure and GCP have no actual-bill source yet.
      </div>

      {isAdmin && <ActualCost days={days} />}

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
