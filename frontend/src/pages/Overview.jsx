// monitoring-hub/frontend/src/pages/Overview.jsx
import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useWebSocket } from "../hooks/useWebSocket";
import { getLiveAccounts, getAlerts } from "../api/api";
import "./Overview.css";
import { useTimezone } from "../contexts/TimezoneContext";
import { getCached, setCached } from "../utils/dataCache";

const BASE = "";

/** Group flat account rows by account_id */
function groupByAccount(accounts) {
  const map = new Map();
  for (const acc of accounts) {
    const key = acc.account_id;
    if (!map.has(key)) {
      map.set(key, {
        account_id:   acc.account_id,
        account_name: acc.account_name,
        environment:  acc.environment,
        owner_team:   acc.owner_team,
        regions:      [],
      });
    }
    map.get(key).regions.push(acc);
  }
  return Array.from(map.values());
}

/** Aggregate health across all regions of an account */
function aggregateStatus(regions) {
  if (regions.some(r => r.status === "critical")) return "critical";
  if (regions.some(r => r.status === "warning"))  return "warning";
  return "healthy";
}

function aggregateStats(regions) {
  return regions.reduce(
    (acc, r) => ({
      ec2_total:       acc.ec2_total       + (r.ec2_total       || 0),
      ec2_running:     acc.ec2_running     + (r.ec2_running     || 0),
      ebs_total:       acc.ebs_total       + (r.ebs_total       || 0),
      s3_total:        acc.s3_total        + (r.s3_total        || 0),
      lambda_total:    acc.lambda_total    + (r.lambda_total    || 0),
      rds_total:       acc.rds_total       + (r.rds_total       || 0),
      // Authoritative per-account alert counts computed server-side in
      // app/api/live_data.py (resolved via resources.aws_account_id
      // across every resource type, not just EC2) -- summed across
      // this account's regions the same way every other stat here is.
      critical_alerts: acc.critical_alerts + (r.critical_alerts || 0),
      warning_alerts:  acc.warning_alerts  + (r.warning_alerts  || 0),
    }),
    {
      ec2_total: 0, ec2_running: 0, ebs_total: 0, s3_total: 0, lambda_total: 0, rds_total: 0,
      critical_alerts: 0, warning_alerts: 0,
    }
  );
}

export default function Overview() {
  const navigate = useNavigate();
  const { ianaName } = useTimezone();
  const OVERVIEW_CACHE_KEY = "overview:accounts_alerts";

  const [accounts,    setAccounts]    = useState([]);
  const [alerts,      setAlerts]      = useState([]);
  const [loading,     setLoading]     = useState(true);
  const [revalidating, setRevalidating] = useState(false);
  // True only when the MOST RECENT fetch attempt failed outright (network
  // error, backend unreachable) -- lets the empty state say "couldn't
  // reach the server" instead of the misleading "no accounts found" when
  // accounts genuinely exist but the last request just didn't succeed.
  const [loadError,   setLoadError]   = useState(false);
  const [filter,      setFilter]      = useState("All");
  const [lastSync,    setLastSync]    = useState(null);
  const [expandedIds, setExpandedIds] = useState(new Set());

  const deletedIds = useRef(new Set());
  const { lastMessage: alertMsg } = useWebSocket("alerts");

  const loadAll = useCallback(async () => {
    setRevalidating(true);
    // Promise.allSettled (not Promise.all + .catch(() => [])) is the whole
    // fix here: a failed request must NEVER be indistinguishable from a
    // successful one that happens to return zero rows. Each of
    // accounts/alerts only updates state (and the cache) for the specific
    // one that actually succeeded -- a transient failure changes nothing
    // on screen and leaves the cache untouched, rather than overwriting
    // good data with an empty snapshot that then sticks around on every
    // reload until the next successful poll happens to fix it.
    const [accResult, alertResult] = await Promise.allSettled([getLiveAccounts(), getAlerts()]);

    if (accResult.status === "fulfilled") {
      const filtered = (Array.isArray(accResult.value) ? accResult.value : [])
        .filter(a => !deletedIds.current.has(a.id));
      setAccounts(filtered);
      setLoadError(false);
      const cachedAlerts = getCached(OVERVIEW_CACHE_KEY)?.data?.alerts || [];
      setCached(OVERVIEW_CACHE_KEY, {
        accounts: filtered,
        alerts: alertResult.status === "fulfilled" && Array.isArray(alertResult.value)
          ? alertResult.value
          : cachedAlerts,
      });
    } else {
      console.error("Overview: accounts fetch failed, keeping last known data:", accResult.reason);
      setLoadError(true);
    }

    if (alertResult.status === "fulfilled") {
      setAlerts(Array.isArray(alertResult.value) ? alertResult.value : []);
    } else {
      console.error("Overview: alerts fetch failed, keeping last known data:", alertResult.reason);
    }

    setLastSync(new Date());
    setLoading(false);
    setRevalidating(false);
  }, []);

  // Hydrate instantly from whatever was cached last time this page
  // loaded -- lets the dashboard render immediately on open/reload
  // instead of sitting on "Fetching live AWS data..." every time,
  // while loadAll() below still fetches fresh data in the background
  // and replaces it (and the cache) as soon as it arrives.
  useEffect(() => {
    const cached = getCached(OVERVIEW_CACHE_KEY);
    if (!cached) return;
    const filtered = (cached.data.accounts || []).filter(a => !deletedIds.current.has(a.id));
    setAccounts(filtered);
    setAlerts(cached.data.alerts || []);
    setLastSync(new Date(cached.ts));
    setLoading(false);
  }, []);

  useEffect(() => {
    loadAll();
    const t = setInterval(loadAll, 60000);
    return () => clearInterval(t);
  }, [loadAll]);

  useEffect(() => {
    if (!alertMsg || alertMsg.type !== "new_alert") return;
    setAlerts(prev => [alertMsg, ...prev].slice(0, 100));
  }, [alertMsg]);

  async function handleDelete(e, acc) {
    e.stopPropagation();
    if (!window.confirm(`Remove "${acc.account_name}" (${acc.region}) from monitoring?`)) return;
    deletedIds.current.add(acc.id);
    setAccounts(prev => prev.filter(a => a.id !== acc.id));
    try {
      const res = await fetch(`${BASE}/api/admin/accounts/${acc.id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error("Delete failed:", err);
      deletedIds.current.delete(acc.id);
      setAccounts(prev => [...prev, acc].sort((a, b) => a.id - b.id));
      alert("Failed to remove account. Please try again.");
    }
  }

  function toggleExpand(accountId) {
    setExpandedIds(prev => {
      const next = new Set(prev);
      next.has(accountId) ? next.delete(accountId) : next.add(accountId);
      return next;
    });
  }

  // Group into logical accounts
  const grouped = groupByAccount(accounts);

  const healthyCount  = grouped.filter(g => aggregateStatus(g.regions) === "healthy").length;
  const warningCount  = grouped.filter(g => aggregateStatus(g.regions) === "warning").length;
  const criticalCount = grouped.filter(g => aggregateStatus(g.regions) === "critical").length;

  const activeAlerts   = alerts.filter(a => (a.status || "").toLowerCase() === "active");
  const criticalAlerts = activeAlerts.filter(a => (a.severity || "").toUpperCase() === "CRITICAL").length;
  const warningAlerts  = activeAlerts.filter(a => (a.severity || "").toUpperCase() === "WARNING").length;

  const filteredGroups = grouped.filter(g => {
    const s = aggregateStatus(g.regions);
    if (filter === "Healthy")  return s === "healthy";
    if (filter === "Warning")  return s === "warning";
    if (filter === "Critical") return s === "critical";
    return true;
  });

  return (
    <div className="overview">
      <div className="ov-header">
        <div>
          <h1 style={{ fontSize: "var(--fs-page-title)", fontWeight: 700, letterSpacing: "-0.01em" }}>
            Infrastructure <span className="hl">Overview</span>
          </h1>
          <p style={{ color: "var(--text-muted)", fontSize: 14, marginTop: 4 }}>
            Live AWS infrastructure monitoring across all accounts
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          {lastSync && (
            <span style={{ fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
              Synced {lastSync.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: ianaName })}
              {revalidating && <span style={{ marginLeft: 6, opacity: 0.7 }}>· updating…</span>}
            </span>
          )}
          <button className="btn-refresh" onClick={loadAll} title="Refresh now">↻ Refresh</button>
        </div>
      </div>

      <div className="ov-summary">
        {loading ? (
          <>
            <SkeletonTile /><SkeletonTile /><SkeletonTile /><SkeletonTile />
          </>
        ) : (
          <>
            <SummaryTile icon={<IconAccounts />} label="Total Accounts" value={grouped.length} />
            <SummaryTile icon={<IconHealthy />}  label="Healthy"  value={healthyCount}  color="green" />
            <SummaryTile icon={<IconWarning />}  label="Warning"  value={warningCount}  color={warningCount  > 0 ? "yellow" : "default"} pulse={warningCount  > 0} />
            <SummaryTile icon={<IconCritical />} label="Critical" value={criticalCount} color={criticalCount > 0 ? "red"    : "default"} pulse={criticalCount > 0} />
          </>
        )}
      </div>

      {(criticalAlerts > 0 || warningAlerts > 0) && (
        <div className="alert-strip">
          <span className="as-dot critical" />
          {criticalAlerts > 0 && (
            <span style={{ fontWeight: 700, color: "var(--red)", marginRight: 8 }}>
              {criticalAlerts} CRITICAL
            </span>
          )}
          {warningAlerts > 0 && (
            <>
              <span style={{ color: "var(--text-muted)", marginRight: 8 }}>·</span>
              <span style={{ fontWeight: 600, color: "var(--yellow)", marginRight: 8 }}>
                {warningAlerts} WARNING
              </span>
            </>
          )}
          <span style={{ color: "var(--text-muted)", fontSize: 13 }}>
            — Active alerts require attention
          </span>
          <button onClick={() => navigate("/alerts")} className="as-btn">View Alerts →</button>
        </div>
      )}

      <div className="ov-section-bar">
        <h2 style={{ fontSize: 17, fontWeight: 700 }}>
          AWS Accounts
          <span style={{ fontWeight: 400, fontSize: 13, color: "var(--text-muted)", marginLeft: 8 }}>
            ({filteredGroups.length})
          </span>
        </h2>
        <div className="filter-row">
          <span style={{ fontSize: 13, color: "var(--text-muted)", marginRight: 6 }}>Filter:</span>
          {["All", "Healthy", "Warning", "Critical"].map(f => (
            <button
              key={f}
              className={`f-btn ${filter === f ? "f-active" : ""}`}
              onClick={() => setFilter(f)}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="accounts-grid">
          <SkeletonAccountCard /><SkeletonAccountCard /><SkeletonAccountCard />
        </div>
      ) : filteredGroups.length === 0 && loadError ? (
        <div className="ov-empty">
          Couldn't reach the server just now — showing the last known state.{" "}
          <span className="ov-link" onClick={loadAll}>Retry →</span>
        </div>
      ) : filteredGroups.length === 0 ? (
        <div className="ov-empty">
          No accounts found.{" "}
          <span className="ov-link" onClick={() => navigate("/onboarding")}>Onboard an account →</span>
        </div>
      ) : (
        <div className="accounts-grid">
          {filteredGroups.map(group => (
            <AccountGroupCard
              key={group.account_id}
              group={group}
              expanded={expandedIds.has(group.account_id)}
              onToggle={() => toggleExpand(group.account_id)}
              onRegionClick={(regionRow) => navigate(`/accounts/${regionRow.id}/services`)}
              onDelete={handleDelete}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Account Group Card ──────────────────────────────────────────────────────

function AccountGroupCard({ group, expanded, onToggle, onRegionClick, onDelete }) {
  const status = aggregateStatus(group.regions);
  const stats  = aggregateStats(group.regions);
  const regionCount = group.regions.length;

  // Same server-computed counts that drove this account's status pill
  // above (via aggregateStatus/aggregateStats) -- the badge below can
  // never disagree with the ring/pill the way the old
  // alerts.filter(a => a.resource.includes(account_id)) substring
  // match sometimes did (most resource ids never literally contain
  // the AWS account id string).
  const acctCritical = stats.critical_alerts;
  const acctWarning  = stats.warning_alerts;

  // Donut based on aggregated ec2_running
  const total         = stats.ec2_running || 0;
  const criticalCount = Math.min(acctCritical, total);
  const warningCount  = Math.min(acctWarning,  Math.max(0, total - criticalCount));
  const healthyCount  = Math.max(0, total - criticalCount - warningCount);

  return (
    <div className={`account-card ac-${status} ${expanded ? "ac-expanded" : ""}`}>
      {/* ── Card header — click to expand/collapse ── */}
      <div
        style={{ cursor: "pointer" }}
        onClick={onToggle}
      >
        <div className="acc-card-header">
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 800, fontSize: 15, marginBottom: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {group.account_name}
            </div>
            <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-muted)" }}>
              {group.account_id}
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <StatusPill status={status} />
            <span className="region-count-badge">
              {regionCount} region{regionCount !== 1 ? "s" : ""}
            </span>
            <span style={{
              fontSize: 13,
              color: "var(--text-muted)",
              transform: expanded ? "rotate(180deg)" : "rotate(0deg)",
              transition: "transform 0.2s ease",
              display: "inline-block",
              lineHeight: 1,
            }}>▾</span>
          </div>
        </div>

        <div style={{ display: "flex", gap: 5, margin: "8px 0", flexWrap: "wrap" }}>
          <Tag text={group.environment} color="purple" />
          {group.owner_team && <Tag text={group.owner_team} color="muted" />}
          {/* Show all unique regions as tags */}
          {group.regions.map(r => (
            <Tag key={r.id} text={r.region} color="blue" />
          ))}
        </div>

        {(acctCritical > 0 || acctWarning > 0) && (
          <div style={{ display: "flex", gap: 6, marginBottom: 6 }}>
            {acctCritical > 0 && (
              <span style={{ fontSize: 10, color: "#ef4444", background: "rgba(239,68,68,0.1)", border: "1px solid rgba(239,68,68,0.2)", borderRadius: 4, padding: "1px 6px" }}>
                ● {acctCritical} critical
              </span>
            )}
            {acctWarning > 0 && (
              <span style={{ fontSize: 10, color: "#f59e0b", background: "rgba(245,158,11,0.1)", border: "1px solid rgba(245,158,11,0.2)", borderRadius: 4, padding: "1px 6px" }}>
                ⚠ {acctWarning} warning
              </span>
            )}
          </div>
        )}

        <div className="acc-body">
          <HealthRing
            total={total}
            healthy={healthyCount}
            warning={warningCount}
            critical={criticalCount}
          />
          <div className="acc-chips">
            <ResChip icon="🖥"  label="EC2"    value={stats.ec2_total}    sub={`${stats.ec2_running} running`} />
            <ResChip icon="💾"  label="EBS"    value={stats.ebs_total}    />
            <ResChip icon="🪣"  label="S3"     value={stats.s3_total}     />
            <ResChip icon="λ"   label="Lambda" value={stats.lambda_total} />
          </div>
        </div>

        <div className="acc-footer" style={{ borderTop: "1px solid var(--border)", paddingTop: 8, marginTop: 4 }}>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {expanded ? "▴ Collapse regions" : `▾ View ${regionCount} region${regionCount !== 1 ? "s" : ""}`}
          </span>
        </div>
      </div>

      {/* ── Region drilldown panel ── */}
      {expanded && (
        <div style={{
          marginTop: 12,
          borderTop: "1px solid var(--border)",
          paddingTop: 12,
          display: "flex",
          flexDirection: "column",
          gap: 8,
        }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 2 }}>
            Regions
          </div>
          {group.regions.map(regionRow => (
            <RegionRow
              key={regionRow.id}
              regionRow={regionRow}
              onClick={() => onRegionClick(regionRow)}
              onDelete={(e) => onDelete(e, regionRow)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Region Row (drilldown) ───────────────────────────────────────────────────

function RegionRow({ regionRow, onClick, onDelete }) {
  const status = regionRow.status || "healthy";

  // Same authoritative per-region counts the backend used to set
  // regionRow.status -- no more independent (and fragile) re-derivation
  // of severity from a raw alerts list here.
  const critical = regionRow.critical_alerts || 0;
  const warning  = regionRow.warning_alerts  || 0;

  const statusClass = {
    healthy:  "region-row-healthy",
    warning:  "region-row-warning",
    critical: "region-row-critical",
  }[status] || "region-row-healthy";

  const dotColor = { healthy: "#22c55e", warning: "#f59e0b", critical: "#ef4444" }[status] || "#22c55e";

  return (
    <div
      onClick={onClick}
      className={`region-row ${statusClass}`}
    >
      {/* Left group: dot + name + resource chips -- allowed to wrap.
          Right group (alert badges + delete) is a separate flex item
          pinned to the end via margin-left:auto on region-row-actions;
          if the row is too narrow for both, the WHOLE actions group
          wraps to its own line below instead of the delete button
          silently losing the fight for space and disappearing off the
          edge, which is what happened when everything shared one
          nowrap-by-default flex row. */}
      <div className="region-row-main">
        <span style={{
          width: 8, height: 8, borderRadius: "50%",
          background: dotColor, flexShrink: 0,
          boxShadow: `0 0 6px ${dotColor}80`,
        }} />
        <span className="region-row-name">
          {regionRow.region}
        </span>
        <div className="region-row-chips">
          <MiniChip label="EC2"    value={regionRow.ec2_total}    sub={`${regionRow.ec2_running}▶`} />
          <MiniChip label="EBS"    value={regionRow.ebs_total}    />
          <MiniChip label="S3"     value={regionRow.s3_total}     />
          <MiniChip label="λ"      value={regionRow.lambda_total} />
        </div>
      </div>

      <div className="region-row-actions">
        {critical > 0 && (
          <span className="region-alert-badge region-alert-critical">● {critical}</span>
        )}
        {warning > 0 && (
          <span className="region-alert-badge region-alert-warning">⚠ {warning}</span>
        )}
        <span className="region-row-goto">Services →</span>
        <button
          className="btn-delete-sm"
          onClick={onDelete}
          title="Remove region"
          aria-label="Remove region"
        >✕</button>
      </div>
    </div>
  );
}

function MiniChip({ label, value, sub }) {
  if (!value && value !== 0) return null;
  return (
    <span className="mini-chip">
      <span className="mini-chip-label">{label}</span>
      <span className="mini-chip-value">{value}</span>
      {sub && <span className="mini-chip-sub">{sub}</span>}
    </span>
  );
}

// ─── Shared sub-components (unchanged from original) ─────────────────────────

function HealthRing({ total, healthy, warning, critical }) {
  const r    = 28;
  const circ = 2 * Math.PI * r;
  const sw   = 7;
  const [tooltip, setTooltip] = useState(null);

  if (total === 0) {
    return (
      <div className="h-ring-wrap">
        <svg width="70" height="70" viewBox="0 0 70 70">
          <circle cx="35" cy="35" r={r} fill="none" stroke="rgba(99,130,190,0.12)" strokeWidth={sw} />
        </svg>
        <div className="h-ring-label">
          <div className="h-ring-num" style={{ color: "var(--text-muted)" }}>0</div>
          <div className="h-ring-sub">running</div>
        </div>
      </div>
    );
  }

  const rawSegs = [
    { count: healthy,  color: "#22c55e", label: "Healthy"  },
    { count: warning,  color: "#f59e0b", label: "Warning"  },
    { count: critical, color: "#ef4444", label: "Critical" },
  ].filter(s => s.count > 0);

  let offsetAngle = 0;
  const paths = rawSegs.map((seg, i) => {
    const pct   = seg.count / total;
    const angle = pct * 360;
    const dash  = pct * circ;
    const gap   = circ - dash;
    const rotate = offsetAngle - 90;
    offsetAngle += angle;
    return (
      <circle
        key={i}
        cx="35" cy="35" r={r}
        fill="none"
        stroke={seg.color}
        strokeWidth={sw}
        strokeDasharray={`${dash} ${gap}`}
        strokeDashoffset={0}
        strokeLinecap="butt"
        transform={`rotate(${rotate} 35 35)`}
        style={{ cursor: "pointer" }}
        onMouseEnter={() => setTooltip({ label: seg.label, count: seg.count, color: seg.color })}
        onMouseLeave={() => setTooltip(null)}
      />
    );
  });

  const centreColor = critical > 0 ? "#ef4444" : warning > 0 ? "#f59e0b" : "#22c55e";

  return (
    <div className="h-ring-wrap" style={{ position: "relative" }}>
      <svg width="70" height="70" viewBox="0 0 70 70">
        <circle cx="35" cy="35" r={r} fill="none" stroke="rgba(99,130,190,0.08)" strokeWidth={sw} />
        {paths}
      </svg>
      <div className="h-ring-label">
        <div className="h-ring-num" style={{ color: centreColor }}>{total}</div>
        <div className="h-ring-sub">running</div>
      </div>
      {tooltip && (
        <div style={{
          position: "absolute", bottom: "100%", left: "50%", transform: "translateX(-50%)",
          background: "var(--bg-elevated, #1e293b)",
          border: `1px solid ${tooltip.color}50`,
          borderRadius: 6, padding: "5px 10px",
          fontSize: 11, fontWeight: 600, whiteSpace: "nowrap",
          color: tooltip.color, pointerEvents: "none", zIndex: 10,
          marginBottom: 4, boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
        }}>
          {tooltip.label}: {tooltip.count} instance{tooltip.count !== 1 ? "s" : ""}
        </div>
      )}
    </div>
  );
}

function ResChip({ icon, label, value, sub }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4, background: "var(--bg-elevated)", border: "1px solid var(--border)", borderRadius: 6, padding: "4px 8px", fontSize: 11 }}>
      <span style={{ fontSize: 12 }}>{icon}</span>
      <span style={{ color: "var(--text-muted)" }}>{label}</span>
      <span style={{ fontWeight: 700, fontFamily: "var(--font-mono)", color: "var(--text-primary)" }}>{value}</span>
      {sub && <span style={{ color: "var(--text-muted)", fontSize: 10 }}>· {sub}</span>}
    </div>
  );
}

function IconAccounts() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M3 21h18"/><path d="M6 21V7l6-4 6 4v14"/><path d="M10 21v-6h4v6"/><path d="M9 10h.01M15 10h.01M9 14h.01M15 14h.01"/></svg>;
}
function IconHealthy() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>;
}
function IconWarning() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>;
}
function IconCritical() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>;
}

// Skeleton placeholders shown ONLY on a genuine first-ever load (no
// cache to hydrate from yet) -- a shimmering approximation of the real
// layout communicates "this is actively loading" far more convincingly
// than a spinner + line of text, and avoids the summary tiles briefly
// showing a bare "0" that reads as a confirmed (rather than pending)
// count.
function SkeletonTile() {
  return (
    <div className="sum-tile sum-default">
      <span className="sum-icon sk-block" style={{ width: 32, height: 32 }} />
      <div className="sum-body">
        <div className="sk-block" style={{ width: 70, height: 10, marginBottom: 6 }} />
        <div className="sk-block" style={{ width: 40, height: 20 }} />
      </div>
    </div>
  );
}

function SkeletonAccountCard() {
  return (
    <div className="account-card" style={{ cursor: "default" }}>
      <div className="sk-block" style={{ width: "60%", height: 15, marginBottom: 8 }} />
      <div className="sk-block" style={{ width: "40%", height: 11, marginBottom: 12 }} />
      <div style={{ display: "flex", gap: 5, marginBottom: 14 }}>
        <div className="sk-block" style={{ width: 50, height: 16, borderRadius: 10 }} />
        <div className="sk-block" style={{ width: 64, height: 16, borderRadius: 10 }} />
      </div>
      <div className="acc-body">
        <div className="sk-block" style={{ width: 76, height: 76, borderRadius: "50%", flexShrink: 0 }} />
        <div className="acc-chips" style={{ flex: 1 }}>
          <div className="sk-block" style={{ width: "100%", height: 26, marginBottom: 6 }} />
          <div className="sk-block" style={{ width: "100%", height: 26, marginBottom: 6 }} />
          <div className="sk-block" style={{ width: "100%", height: 26, marginBottom: 6 }} />
          <div className="sk-block" style={{ width: "100%", height: 26 }} />
        </div>
      </div>
    </div>
  );
}

function SummaryTile({ icon, label, value, color, pulse }) {
  return (
    <div className={`sum-tile sum-${color || "default"}`} style={{ position: "relative" }}>
      {pulse && <span className="pulse-ring" />}
      <span className="sum-icon">{icon}</span>
      <div className="sum-body">
        <div className="sum-label">{label}</div>
        <div className="sum-value">{value}</div>
      </div>
    </div>
  );
}

function StatusPill({ status }) {
  const m = {
    healthy:  { label: "Healthy",  cls: "pill-green"  },
    warning:  { label: "Warning",  cls: "pill-yellow" },
    critical: { label: "Critical", cls: "pill-red"    },
  };
  const { label, cls } = m[status] || m.healthy;
  return <span className={`status-pill ${cls}`}>{label}</span>;
}

function Tag({ text, color }) {
  return text ? <span className={`tag tag-${color || "blue"}`}>{text}</span> : null;
}
