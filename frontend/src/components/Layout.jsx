// src/components/Layout.jsx
import { Outlet, NavLink, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useState, useEffect } from "react";
import AlertToast from "./AlertToast";
import { useTimezone, TIMEZONE_OPTIONS } from "../contexts/TimezoneContext";
import "./Layout.css";

// Role-based nav visibility:
// admin   → all items
// editor  → overview, alerts, compliance, settings (NO onboarding, NO user mgmt)
// viewer  → overview, alerts, compliance (NO onboarding, NO user mgmt, NO settings)
const NAV_ITEMS = [
  { to: "/overview",   label: "Overview",           icon: OverviewIcon,   roles: ["admin","editor","viewer"] },
  { to: "/alerts",     label: "Alerts",             icon: AlertIcon,      roles: ["admin","editor","viewer"], badge: true },
  { to: "/onboarding", label: "Account Onboarding", icon: OnboardIcon,    roles: ["admin","editor"] },
  { to: "/users",      label: "User Management",    icon: UsersIcon,      roles: ["admin"] },
  { to: "/compliance", label: "Compliance",         icon: ComplianceIcon, roles: ["admin","editor","viewer"] },
  { to: "/settings",   label: "Settings",           icon: SettingsIcon,   roles: ["admin","editor"] },
];

export default function Layout() {
  const { user, logout } = useAuth();
  const navigate          = useNavigate();
  const { timezone, setTimezone, ianaName } = useTimezone();
  const role              = (user?.role || "viewer").toLowerCase();
  const [now, setNow]     = useState(new Date());
  const [alertCount, setAlertCount] = useState(0);
  const [dark, setDark]   = useState(() => localStorage.getItem("theme") !== "light");
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    localStorage.setItem("theme", dark ? "dark" : "light");
  }, [dark]);

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    async function fetchCount() {
      try {
        const res = await fetch("/api/alerts/open");
        if (!res.ok) return;
        const data = await res.json();
        const arr = Array.isArray(data) ? data : (data.alerts ?? []);
        setAlertCount(arr.filter(a => (a.status || "").toLowerCase() === "active").length);
      } catch {}
    }
    fetchCount();
    const t = setInterval(fetchCount, 30000);
    return () => clearInterval(t);
  }, []);

  function handleLogout() {
    logout();
    navigate("/login", { replace: true });
  }

  // Respects the IST/UTC selector in the topbar (TimezoneContext) —
  // previously hardcoded to Asia/Kolkata regardless of any preference.
  const timeStr = now.toLocaleTimeString("en-US", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    timeZone: ianaName,
  });

  const visibleNav = NAV_ITEMS.filter(item => item.roles.includes(role));

  return (
    <div className={`layout ${navOpen ? "nav-open" : ""}`}>
      <div className="sidebar-scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />
      <aside className="sidebar">
        <nav className="sidebar-nav">
          {visibleNav.map(({ to, label, icon: Icon, badge }) => (
            <NavLink
              key={to}
              to={to}
              title={label}
              onClick={() => setNavOpen(false)}
              className={({ isActive }) => `nav-item ${isActive ? "nav-active" : ""}`}
            >
              <span className="nav-icon"><Icon /></span>
              <span className="nav-label">{label}</span>
              {badge && alertCount > 0 && (
                <span className="nav-badge">{alertCount}</span>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="sidebar-last-updated">
            <span className="lup-label">Last updated</span>
            <span className="lup-time">
              {now.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: ianaName })},{" "}
              {timeStr.split(":").slice(0, 3).join(":")}
            </span>
          </div>
        </div>
      </aside>

      <div className="main-wrap">
        <header className="topbar">
          <button
            className="btn-nav-toggle"
            onClick={() => setNavOpen(o => !o)}
            aria-label={navOpen ? "Close navigation menu" : "Open navigation menu"}
            aria-expanded={navOpen}
            title={navOpen ? "Collapse sidebar" : "Expand sidebar"}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="3" y1="6" x2="21" y2="6"/>
              <line x1="3" y1="12" x2="21" y2="12"/>
              <line x1="3" y1="18" x2="21" y2="18"/>
            </svg>
          </button>
          <div className="topbar-brand">
            <div className="sidebar-logo">
              <svg width="24" height="24" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg" aria-label="CloudOps">
                <rect width="512" height="512" rx="112" fill="#0b1220" />
                <g transform="translate(-891.82,2.79) scale(0.8064)">
                  <path fill="#2bb3ac" d="M1331.98,222.58c41.39-41.42,103.91-48.88,152.93-22.35l53.65-53.65c-79.15-54.58-188.41-46.67-258.82,23.77-70.44,70.4-78.35,179.67-23.77,258.82l53.65-53.65c-26.53-49.02-19.07-111.55,22.35-152.93Z" />
                  <path fill="#2bb3ac" d="M1567.06,457.66c70.44-70.44,78.35-179.7,23.73-258.85l-53.65,53.65c26.53,49.02,19.07,111.55-22.32,152.97-41.42,41.39-103.95,48.85-152.93,22.32l-53.65,53.65c79.15,54.62,188.38,46.71,258.82-23.73Z" />
                </g>
              </svg>
            </div>
            <div className="sidebar-brand-text">
              <div className="sidebar-brand-sub">AURIONPRO</div>
              <div className="sidebar-brand-name">CloudOps</div>
            </div>
          </div>
          <div className="topbar-page-label" id="page-label" />
          <div className="topbar-right">
            <div className="live-pill">
              <span className="live-dot" />
              LIVE
            </div>
            <button
              className="btn-theme-toggle"
              onClick={() => setDark(d => !d)}
              title={dark ? "Switch to light theme" : "Switch to dark theme"}
              aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
            >
              {dark ? "☀" : "🌙"}
            </button>
            <select
              className="tz-select"
              value={timezone}
              onChange={e => setTimezone(e.target.value)}
              title="Display timezone — applies to every clock and chart"
              aria-label="Display timezone"
            >
              {Object.entries(TIMEZONE_OPTIONS).map(([key, opt]) => (
                <option key={key} value={key}>{opt.label}</option>
              ))}
            </select>
            <div className="topbar-clock">
              {timeStr} <span className="topbar-tz">{timezone}</span>
            </div>
            <div className="topbar-user">
              <span className="topbar-user-icon">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                  <circle cx="12" cy="7" r="4"/>
                </svg>
              </span>
              <span className="topbar-username">{user?.username ?? "admin"}</span>
              <span className={`topbar-role-badge role-${role}`}>
                {role.toUpperCase()}
              </span>
            </div>
            <button className="btn-logout" onClick={handleLogout} title="Logout" aria-label="Log out">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
                <polyline points="16 17 21 12 16 7"/>
                <line x1="21" y1="12" x2="9" y2="12"/>
              </svg>
              <span>Logout</span>
            </button>
          </div>
        </header>
        <main className="main-content">
          <Outlet />
          <AlertToast />
        </main>
      </div>
    </div>
  );
}

function OverviewIcon()    { return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>; }
function AlertIcon()       { return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>; }
function OnboardIcon()     { return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>; }
function UsersIcon()       { return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>; }
function ComplianceIcon()  { return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>; }
function SettingsIcon()    { return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>; }
