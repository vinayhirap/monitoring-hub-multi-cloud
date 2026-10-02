// src/components/shell.jsx -- topbar pieces for the CloudOps shell (UI Phase 1).
// Pure presentation + existing handlers passed in from Layout.jsx. No new endpoints.
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { getAccount, getAlertsPreview } from "../api/api";
import { metricLabel } from "../utils/metricLabels";
import { TIMEZONE_OPTIONS } from "../contexts/TimezoneContext";
import { SunIcon, MoonIcon } from "./icons";
import { StatusBeacon, SeverityBadge, EmptyState } from "./ui";
import "./shell.css";

const acctCache = new Map();   // id -> account payload (names don't change mid-session)

/** Provider > Account > Region resolver for /accounts/:id/*. Falls back to
 *  "Account <id>" if the caller may not read /api/admin/accounts/:id (viewer). */
function useAccountCrumb(id) {
  const [acct, setAcct] = useState(() => (id ? acctCache.get(id) ?? null : null));
  useEffect(() => {
    if (!id || acctCache.has(id)) return;
    let dead = false;
    getAccount(id).then(d => { if (d) { acctCache.set(id, d); if (!dead) setAcct(d); } }).catch(() => {});
    return () => { dead = true; };
  }, [id]);
  return id ? (acctCache.get(id) ?? acct) : null;
}

const PROVIDER_LABEL = { aws: "AWS", gcp: "GCP", azure: "Azure" };
const SUB_LABEL = { services: "Services", topology: "Topology", incidents: "Incidents" };

export function Breadcrumb({ pathname, navItems }) {
  const m = pathname.match(/^\/accounts\/(\d+)(?:\/([^/]+))?/);
  const acct = useAccountCrumb(m ? m[1] : null);
  const crumbs = [];
  if (m) {
    const [, id, seg] = m;
    crumbs.push({ label: "Overview", to: "/overview" });
    if (acct?.provider || acct) crumbs.push({ label: PROVIDER_LABEL[acct?.provider || "aws"] || acct?.provider });
    crumbs.push({
      label: acct ? `${acct.account_name}${acct.default_region ? ` · ${acct.default_region}` : ""}` : `Account ${id}`,
      to: seg ? `/accounts/${id}/services` : null,
    });
    if (seg && !SUB_LABEL[seg]) crumbs.push({ label: "Services", to: `/accounts/${id}/services` });   // a service page sits under Services
    if (seg) crumbs.push({ label: SUB_LABEL[seg] || seg.replace(/_/g, " ").toUpperCase() });
  } else {
    const item = navItems.find(n => pathname.startsWith(n.to));
    if (item) { if (item.group && item.group !== item.label) crumbs.push({ label: item.group }); crumbs.push({ label: item.label }); }
  }
  if (!crumbs.length) return <div className="bc-wrap" />;
  return (
    <nav className="bc-wrap" aria-label="Breadcrumb">
      <ol className="bc-list">
        {crumbs.map((c, i) => {
          const last = i === crumbs.length - 1;
          return (
            <li key={i} className={last ? "bc-item bc-current" : "bc-item"} aria-current={last ? "page" : undefined}>
              {c.to && !last ? <Link to={c.to}>{c.label}</Link> : <span>{c.label}</span>}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

const STALE_MS = 45000;
/** Live/Polling/Stale. Driven by real signals: WS state + age of the last
 *  successful /api/alerts/counts poll (Layout polls it every 15 s). */
export function LiveChip({ connected, lastOkAt, now }) {
  let tone = "mute", text = "Connecting", title = "Waiting for first update";
  if (lastOkAt) {
    const age = now - lastOkAt;
    if (age >= STALE_MS)       { tone = "crit"; text = "Stale";   title = `No successful update for ${Math.round(age / 1000)}s`; }
    else if (!connected)       { tone = "warn"; text = "Polling"; title = "Live stream disconnected; refreshing every 15s"; }
    else                       { tone = "ok";   text = "Live";    title = "Live stream connected, data current"; }
  }
  return (
    <span className={`live-chip t-${tone}`} title={title} role="status">
      <StatusBeacon tone={tone} pulse={tone === "ok"} />{text}
    </span>
  );
}

/** Theme, timezone, logout in one menu. All handlers come from Layout. */
export function UserMenu({ username, role, dark, onToggleTheme, compact, onToggleDensity, timezone, onTimezone, onLogout }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const down = e => { if (!ref.current?.contains(e.target)) setOpen(false); };
    const key = e => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", down); document.addEventListener("keydown", key);
    return () => { document.removeEventListener("mousedown", down); document.removeEventListener("keydown", key); };
  }, [open]);
  return (
    <div className="um" ref={ref}>
      <button type="button" className="um-btn" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(o => !o)}>
        <span className="um-avatar" aria-hidden="true">{String(username).charAt(0).toUpperCase()}</span>
        <span className="um-name">{username}</span>
        <span className={`topbar-role-badge role-${role}`}>{role.toUpperCase()}</span>
      </button>
      {open && (
        <div className="um-menu" role="menu">
          <button type="button" role="menuitem" className="um-item" onClick={onToggleTheme}>
            {dark ? <SunIcon size={14} /> : <MoonIcon size={14} />}{dark ? "Light theme" : "Dark theme"}
          </button>
          <button type="button" role="menuitemcheckbox" aria-checked={compact} className="um-item" onClick={onToggleDensity}>
            <span className="um-check" aria-hidden="true">{compact ? "✓" : ""}</span>Compact density
          </button>
          <label className="um-item um-tz">Timezone
            <select value={timezone} onChange={e => onTimezone(e.target.value)} aria-label="Display timezone">
              {Object.entries(TIMEZONE_OPTIONS).map(([k, o]) => <option key={k} value={k}>{o.label}</option>)}
            </select>
          </label>
          <button type="button" role="menuitem" className="um-item um-danger" onClick={onLogout}>Log out</button>
        </div>
      )}
    </div>
  );
}

/* ── shared popover behaviour: close on outside click / Escape ── */
function usePopover() {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const down = e => { if (!ref.current?.contains(e.target)) setOpen(false); };
    const key = e => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", down); document.addEventListener("keydown", key);
    return () => { document.removeEventListener("mousedown", down); document.removeEventListener("keydown", key); };
  }, [open]);
  return { open, setOpen, ref };
}

const TONE = { critical: "crit", warning: "warn", healthy: "ok" };
const worst = rows => rows.some(r => r.status === "critical") ? "critical" : rows.some(r => r.status === "warning") ? "warning" : "healthy";

/** Group account x region rows by AWS account id. */
function groupAccounts(rows) {
  const m = new Map();
  for (const r of rows) {
    if (!m.has(r.account_id)) m.set(r.account_id, { account_id: r.account_id, account_name: r.account_name, rows: [] });
    m.get(r.account_id).rows.push(r);
  }
  return [...m.values()];
}

/** Scope switcher: Account -> Region. Navigation-based, so it is honest on every page:
 *  region -> that region's services, account -> Overview filtered to it (?a=), All -> Overview. */
export function ScopeSwitcher({ rows }) {
  const { open, setOpen, ref } = usePopover();
  const [q, setQ] = useState("");
  const navigate = useNavigate();
  const loc = useLocation();
  const groups = useMemo(() => groupAccounts(rows), [rows]);
  if (!rows.length) return null;

  const m = loc.pathname.match(/^\/accounts\/(\d+)/);
  const cur = m ? rows.find(r => String(r.id) === m[1]) : null;
  const filterAcct = !m && loc.pathname === "/overview" ? new URLSearchParams(loc.search).get("a") : null;
  const curGroup = filterAcct ? groups.find(g => g.account_id === filterAcct) : null;
  const label = cur ? `${cur.account_name} · ${cur.region}` : curGroup ? curGroup.account_name : "All accounts";
  const go = to => { setOpen(false); setQ(""); navigate(to); };
  const ql = q.trim().toLowerCase();
  const shown = groups.filter(g => !ql || g.account_name.toLowerCase().includes(ql) || String(g.account_id).includes(ql) || g.rows.some(r => r.region.includes(ql)));

  return (
    <div className="scope" ref={ref}>
      <button type="button" className="scope-btn" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(o => !o)} title="Switch account / region">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="M17.5 19H9a7 7 0 1 1 6.7-9h1.8a4.5 4.5 0 0 1 0 9Z"/></svg>
        <span className="scope-cap">Scope</span><span className="scope-val">{label}</span>
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>
      </button>
      {open && (
        <div className="scope-pop" role="dialog" aria-label="Switch scope">
          <input className="ui-input scope-q" autoFocus value={q} onChange={e => setQ(e.target.value)} placeholder="Find account or region" aria-label="Find account or region" />
          <button type="button" className={`scope-all${!cur && !curGroup ? " is-on" : ""}`} onClick={() => go("/overview")}>All accounts <span>{groups.length}</span></button>
          <div className="scope-list">
            {shown.map(g => (
              <div key={g.account_id} className="scope-acct">
                <button type="button" className="scope-acct-name" onClick={() => go(`/overview?a=${encodeURIComponent(g.account_id)}`)} title="Filter Overview to this account">
                  <StatusBeacon tone={TONE[worst(g.rows)]} />{g.account_name}<span className="scope-id">{g.account_id}</span>
                </button>
                <div className="scope-regions">
                  {g.rows.map(r => (
                    <button key={r.id} type="button" className={`scope-rg${cur && cur.id === r.id ? " is-on" : ""}`} onClick={() => go(`/accounts/${r.id}/services`)}>
                      <StatusBeacon tone={TONE[r.status] || "mute"} />{r.region}
                    </button>
                  ))}
                </div>
              </div>
            ))}
            {!shown.length && <EmptyState title="No match" />}
          </div>
        </div>
      )}
    </div>
  );
}

/** Notification bell. Count comes from Layout's existing /alerts/counts poll;
 *  the list is fetched on open from the same endpoint the Alerts page uses. */
export function NotificationBell({ count }) {
  const { open, setOpen, ref } = usePopover();
  const navigate = useNavigate();
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    if (!open) return;
    let dead = false;
    getAlertsPreview(8).then(d => { if (!dead) { setErr(false); setRows(Array.isArray(d) ? d : (d?.alerts ?? [])); } }).catch(() => { if (!dead) setErr(true); });
    return () => { dead = true; };
  }, [open]);
  const go = to => { setOpen(false); navigate(to); };
  return (
    <div className="bell" ref={ref}>
      <button type="button" className="bell-btn" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(o => !o)}
        aria-label={`Notifications, ${count} active alert${count === 1 ? "" : "s"}`} title="Active alerts">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
        {count > 0 && <span className="bell-n">{count > 99 ? "99+" : count}</span>}
      </button>
      {open && (
        <div className="bell-pop" role="dialog" aria-label="Active alerts">
          <div className="bell-head"><span>Active alerts</span><span className="bell-total">{count}</span></div>
          <div className="bell-list">
            {!rows && !err && <EmptyState title="Loading…" />}
            {err && <EmptyState title="Could not load alerts" />}
            {rows && rows.length === 0 && <EmptyState title="No active alerts" />}
            {rows && rows.map(a => (
              <button key={a.id} type="button" className="bell-row" onClick={() => go(`/alerts?tab=active&q=${encodeURIComponent(a.resource || "")}`)}>
                <SeverityBadge severity={a.severity} />
                <span className="bell-main"><span className="bell-res">{a.resource_name || a.resource || "—"}</span>
                  <span className="bell-sub">{metricLabel(a.metric_name)}{a.account_name ? ` · ${a.account_name}` : ""}</span></span>
              </button>
            ))}
          </div>
          <button type="button" className="bell-foot" onClick={() => go("/alerts")}>Open Alerts</button>
        </div>
      )}
    </div>
  );
}

/** Command palette (Ctrl/Cmd+K, "/"). Pages (already RBAC-filtered by Layout),
 *  accounts/regions, and a free-text jump into Alerts search. */
export function CommandPalette({ open, onClose, pages, rows, canSmartSearch }) {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [idx, setIdx] = useState(0);
  const items = useMemo(() => {
    const ql = q.trim().toLowerCase();
    const hit = t => !ql || t.toLowerCase().includes(ql);
    const out = [];
    pages.filter(p => hit(p.label) || hit(p.group || "")).forEach(p => out.push({ k: `p${p.to}`, kind: "Page", label: p.label, hint: p.group, to: p.to }));
    rows.filter(r => hit(`${r.account_name} ${r.region} ${r.account_id}`)).slice(0, 12)
      .forEach(r => out.push({ k: `a${r.id}`, kind: "Account", label: `${r.account_name} · ${r.region}`, hint: r.account_id, to: `/accounts/${r.id}/services`, tone: TONE[r.status] }));
    if (ql) {
      out.push({ k: "qa", kind: "Search", label: `Search alerts for “${q.trim()}”`, to: `/alerts?tab=all&q=${encodeURIComponent(q.trim())}` });
      if (canSmartSearch) out.push({ k: "qs", kind: "Search", label: "Ask in natural language (Smart Search)", to: "/search" });
    }
    return out;
  }, [q, pages, rows, canSmartSearch]);
  useEffect(() => {
    if (!open) return;
    const k = e => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [open, onClose]);
  if (!open) return null;
  const pick = it => { if (!it) return; onClose(); setQ(""); setIdx(0); navigate(it.to); };
  const onKey = e => {
    if (e.key === "ArrowDown") { e.preventDefault(); setIdx(i => Math.min(i + 1, items.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setIdx(i => Math.max(i - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); pick(items[idx]); }
  };
  return (
    <div className="ui-scrim cmdk-scrim" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="cmdk" role="dialog" aria-modal="true" aria-label="Command palette">
        <input className="cmdk-input" autoFocus value={q} onChange={e => { setQ(e.target.value); setIdx(0); }} onKeyDown={onKey}
          placeholder="Jump to a page, account or region, or search alerts…" role="combobox" aria-expanded="true" aria-controls="cmdk-list" aria-activedescendant={items[idx] ? `cmdk-${items[idx].k}` : undefined} />
        <ul className="cmdk-list" id="cmdk-list" role="listbox">
          {items.map((it, i) => (
            <li key={it.k} id={`cmdk-${it.k}`} role="option" aria-selected={i === idx} className={i === idx ? "is-on" : undefined}
              onMouseEnter={() => setIdx(i)} onClick={() => pick(it)}>
              <span className="cmdk-kind">{it.kind}</span>
              <span className="cmdk-label">{it.tone && <StatusBeacon tone={it.tone} />}{it.label}</span>
              {it.hint && <span className="cmdk-hint">{it.hint}</span>}
            </li>
          ))}
          {!items.length && <li className="cmdk-empty">No matches</li>}
        </ul>
        <div className="cmdk-foot"><kbd>↑↓</kbd> move <kbd>Enter</kbd> open <kbd>Esc</kbd> close</div>
      </div>
    </div>
  );
}
