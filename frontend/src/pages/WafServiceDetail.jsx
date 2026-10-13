// monitoring-hub/frontend/src/pages/WafServiceDetail.jsx
//
// AWS WAFv2 detail page (Phase 3). Routed from ServiceDetailRouter for service "wafv2".
// It is GenericServiceDetail (resource list, search/filter, time range, auto-refresh, evidence, alert badge,
// per-metric charts with threshold lines) plus a WAF panel at the top of every expanded web ACL:
//   - KPI tiles for the selected range: allowed, blocked, block ratio, CAPTCHA, challenge, counted
//   - CAPTCHA / challenge trend
//   - top rules by blocked (and counted) requests, from GET /api/live/waf/{id}/rules/{acl}
// Everything is read from our own metric_history; nothing here calls AWS. No mock data: a figure with no
// underlying metric shows "—", and per-rule data that is not collected yet says so (it needs
// WAF_PER_RULE_ENABLED on the server).
import { useEffect, useState } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend } from "recharts";
import GenericServiceDetail from "./GenericServiceDetail";
import { getWafRules } from "../api/api";
import { formatShort } from "../utils/timeFormat";

// A metric the server had to widen (no point in the selected range) covers a DIFFERENT window than the tile label, so it
// must not be summed under that label: null -> shown as "—".
const sumOf = (m) => (m && !m.widened && Array.isArray(m.series) ? m.series.reduce((a, p) => a + (Number(p.v) || 0), 0) : null);
const NO_POINTS = "no points in this range (polled less often)";
const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 }));

function Kpi({ label, value, sub, tone }) {
  return (
    <div style={{ flex: "1 1 140px", minWidth: 130, padding: "10px 12px", border: "1px solid var(--border)", borderRadius: 8,
                  background: "var(--bg-card, rgba(255,255,255,.02))" }}>
      <div style={{ fontSize: 10, letterSpacing: ".06em", textTransform: "uppercase", color: "var(--text-muted)" }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 700, fontFamily: "var(--font-mono)", color: tone || "var(--text-primary)" }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{sub}</div>}
    </div>
  );
}

function mergeSeries(metrics, names) {
  const byT = {};
  names.filter((n) => !metrics?.[n]?.widened).forEach((n) => (metrics?.[n]?.series || []).forEach((p) => { (byT[p.t] = byT[p.t] || { t: p.t })[n] = p.v; }));
  return Object.values(byT).sort((a, b) => (a.t < b.t ? -1 : 1));
}

const TREND = [["CaptchaRequests", "#fbbf24"], ["ChallengeRequests", "#7c6ee0"],
               ["RequestsWithValidCaptchaToken", "#34d399"], ["RequestsWithValidChallengeToken", "#38bdf8"]];

function TopRules({ accountId, r, timeRange, refreshTick }) {
  const [state, setState] = useState({ loading: true });
  useEffect(() => {
    let dead = false;
    setState((s) => ({ ...s, loading: !s.data }));
    getWafRules(accountId, r.resource_id, timeRange)
      .then((data) => !dead && setState({ data }))
      .catch((e) => !dead && setState({ error: e.message || "Failed to load rule data" }));
    return () => { dead = true; };
  }, [accountId, r.resource_id, timeRange, refreshTick]);

  const { data, error, loading } = state;
  const box = { border: "1px solid var(--border)", borderRadius: 8, padding: 12 };
  if (loading) return <div style={{ ...box, fontSize: 12, color: "var(--text-muted)" }}>Loading rule data…</div>;
  if (error) return <div style={{ ...box, fontSize: 12, color: "var(--red)" }}>{error}</div>;
  if (data.status === "not_collected") {
    return <div style={{ ...box, fontSize: 12, color: "var(--text-muted)" }}>
      Per-rule data is not being collected for this web ACL. It is switched on server-side (WAF_PER_RULE_ENABLED) and
      appears after the next discovery and collection cycle.
    </div>;
  }
  const rules = data.rules || [];
  if (!rules.length) {
    return <div style={{ ...box, fontSize: 12, color: "var(--text-muted)" }}>
      No rule reported blocked or counted requests in this range
      {data.rules_tracked ? ` (${data.rules_tracked} rules with CloudWatch metrics are being watched).` : " (this ACL has no rules with CloudWatch metrics enabled)."}
    </div>;
  }
  const max = Math.max(...rules.map((x) => x.total), 1);
  return (
    <div style={box}>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>Top rules by requests in range</div>
      {rules.map((x) => (
        <div key={`${x.kind}:${x.metric}`} style={{ marginBottom: 7 }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8, fontSize: 12 }}>
            <span title={x.group ? `Rule group: ${x.group}` : undefined}>
              {x.name} <span className="mono small muted">{x.kind === "blocked" ? "blocked" : "counted"}{x.group ? ` · ${x.group}` : ""}</span>
            </span>
            <span className="mono">{fmt(x.total)}</span>
          </div>
          <div style={{ height: 5, borderRadius: 3, background: "var(--border)" }}>
            <div style={{ width: `${Math.max(2, (x.total / max) * 100)}%`, height: "100%", borderRadius: 3,
                          background: x.kind === "blocked" ? "var(--red, #ef4444)" : "var(--amber, #fbbf24)" }} />
          </div>
        </div>
      ))}
      <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 6 }}>
        {data.rules_with_data} of {data.rules_tracked} tracked rules had data. Rule-group rows are the group total.
      </div>
    </div>
  );
}

function WafResourcePanel({ accountId, r, metrics, timeRange, timeRangeLabel, refreshTick, ianaName }) {
  const allowed = sumOf(metrics?.AllowedRequests), blocked = sumOf(metrics?.BlockedRequests);
  const counted = sumOf(metrics?.CountedRequests);
  const captcha = sumOf(metrics?.CaptchaRequests), challenge = sumOf(metrics?.ChallengeRequests);
  // Blocked / (Allowed + Blocked): Counted and Passed overlap with those two, so they are not part of the denominator.
  const total = allowed != null && blocked != null ? allowed + blocked : null;
  const ratio = total ? (100 * blocked) / total : null;
  const trend = mergeSeries(metrics, TREND.map((t) => t[0]));
  const trendKeys = TREND.filter(([n]) => metrics?.[n] && !metrics[n].widened);

  return (
    <div style={{ display: "grid", gap: 10, padding: "10px 0" }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        <Kpi label={`Allowed · ${timeRangeLabel}`} value={fmt(allowed)} sub={metrics?.AllowedRequests?.widened ? NO_POINTS : undefined} />
        <Kpi label={`Blocked · ${timeRangeLabel}`} value={fmt(blocked)} tone={blocked ? "var(--red, #ef4444)" : undefined}
             sub={metrics?.BlockedRequests?.widened ? NO_POINTS : undefined} />
        <Kpi label="Block ratio" value={ratio == null ? "—" : `${ratio.toFixed(1)}%`}
             sub={total == null ? "needs Allowed and Blocked in this range" : `of ${fmt(total)} allowed + blocked`} />
        <Kpi label="CAPTCHA" value={fmt(captcha)} />
        <Kpi label="Challenge" value={fmt(challenge)} />
        <Kpi label="Counted" value={fmt(counted)} sub="matched in count mode" />
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 10 }}>
        <div style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>CAPTCHA and challenge trend</div>
          {trendKeys.length === 0 || trend.length === 0 ? (
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>No CAPTCHA or challenge activity recorded in this range.</div>
          ) : (
            <div style={{ height: 160 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={trend}>
                  <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
                  <XAxis dataKey="t" tick={{ fontSize: 10 }} tickFormatter={(t) => formatShort(t, ianaName)} minTickGap={40} />
                  <YAxis tick={{ fontSize: 10 }} width={36} />
                  <Tooltip labelFormatter={(t) => formatShort(t, ianaName)} />
                  <Legend wrapperStyle={{ fontSize: 10 }} />
                  {trendKeys.map(([n, c]) => <Line key={n} type="monotone" dataKey={n} stroke={c} dot={false} strokeWidth={1.5} connectNulls />)}
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>
        <TopRules accountId={accountId} r={r} timeRange={timeRange} refreshTick={refreshTick} />
      </div>
    </div>
  );
}

export default function WafServiceDetail({ accountId }) {
  return <GenericServiceDetail accountId={accountId} service="wafv2" ResourcePanel={WafResourcePanel} />;
}
