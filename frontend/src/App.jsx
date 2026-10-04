// monitoring-hub/frontend/src/App.jsx
import { Suspense } from "react";
import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import { AuthProvider, useAuth }  from "./auth/AuthContext";
import { TimezoneProvider } from "./contexts/TimezoneContext";
import Layout            from "./components/Layout";
import Login             from "./pages/Login";
import Overview          from "./pages/Overview";
// Deploy Risk hidden from frontend (2026-09-17, per request) -- backend
// (app/api/deploy_risk.py, app/collector/rca.py) untouched. Uncomment
// here and in components/Layout.jsx's NAV_ITEMS to re-enable.
// import DeployRisk          from "./pages/DeployRisk";
import NotFound             from "./pages/NotFound";
import { lazyWithRetry } from "./utils/lazyWithRetry";

// Route-level code splitting (audit G1): everything except the shell, sign-in, Overview and the 404 page loads on
// first visit, so the initial download no longer includes Topology, ServiceDetail, Reports, Settings, etc.
const Alerts = lazyWithRetry(() => import("./pages/Alerts"));
const AccountDetail = lazyWithRetry(() => import("./pages/AccountDetail"));
const AccessControl = lazyWithRetry(() => import("./pages/AccessControl"));
const Compliance = lazyWithRetry(() => import("./pages/Compliance"));
const Settings = lazyWithRetry(() => import("./pages/Settings"));
const AccountOnboarding = lazyWithRetry(() => import("./pages/AccountOnboarding"));
const ServiceList = lazyWithRetry(() => import("./pages/ServiceList"));
const ServiceDetailRouter = lazyWithRetry(() => import("./pages/ServiceDetailRouter"));
const Topology = lazyWithRetry(() => import("./pages/Topology"));
const Incidents = lazyWithRetry(() => import("./pages/Incidents"));
const OpEvents = lazyWithRetry(() => import("./pages/OpEvents"));
const EscalationPolicies = lazyWithRetry(() => import("./pages/EscalationPolicies"));
const SyntheticChecks = lazyWithRetry(() => import("./pages/SyntheticChecks"));
const Slos = lazyWithRetry(() => import("./pages/Slos"));
const SecurityFindings = lazyWithRetry(() => import("./pages/SecurityFindings"));
const MaintenanceWindows = lazyWithRetry(() => import("./pages/MaintenanceWindows"));
const Search = lazyWithRetry(() => import("./pages/Search"));
const Reports = lazyWithRetry(() => import("./pages/Reports"));
const StatusPageAdmin = lazyWithRetry(() => import("./pages/StatusPageAdmin"));
const StatusPagePublic = lazyWithRetry(() => import("./pages/StatusPagePublic"));

function SessionCheckingScreen() {
  // Shown only for the brief moment while AuthContext asks the backend
  // "am I logged in" on first load — avoids flashing the login page
  // (or, worse, a protected page) before that answer comes back.
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "center",
      height: "100vh", color: "#888", fontSize: "0.95rem",
    }}>
      Checking session…
    </div>
  );
}

function RequireAuth({ children }) {
  const { isLoggedIn, loading } = useAuth();
  const location = useLocation();
  if (loading) return <SessionCheckingScreen />;
  return isLoggedIn ? children : <Navigate to="/login" replace state={{ from: location }} />;   // the sign-in page returns them here
}

// SECURITY: hiding a nav item was never authorization -- the backend's
// own require_permission() on each endpoint is the real enforcement,
// and stays exactly as strict either way. This closes the gap where a
// logged-in viewer who can't see e.g. "User Management" or "RBAC
// Administration" in the sidebar could still load that page's shell
// (its layout, buttons, any client-only text) by typing the URL
// directly, then see a wall of failed-request 403s instead of a clean
// redirect. Routes with no entry here (the /accounts/:id/* drill-down
// pages) are unaffected -- same as before, gated by login only,
// tenant/account scoping enforced backend-side.
//
// Deliberately a standalone copy of components/Layout.jsx's NAV_ITEMS
// roles/perm/feature fields rather than an import from there: this
// repo has ~39 audit sessions editing Layout.jsx and App.jsx
// concurrently, and importing shared state across those two
// frequently-touched files was producing exactly the kind of patch
// conflict this fix should not be adding to the pile. If a future nav
// item's access rule changes in Layout.jsx, mirror it here too.
const ROUTE_ACCESS = {
  "overview":            { roles: ["admin","editor","viewer"] },
  "alerts":              { roles: ["admin","editor","viewer"] },
  "onboarding":          { roles: ["admin","editor"] },
  "access":              { roles: ["admin","editor","viewer"], perm: "users.view" },
  "compliance":          { roles: ["admin","editor","viewer"] },
  "escalation-policies": { roles: ["admin","editor","viewer"], perm: "escalation.view" },
  "synthetic-checks":    { roles: ["admin","editor","viewer"], perm: "synthetic.view" },
  "slos":                { roles: ["admin","editor","viewer"], perm: "slo.view" },
  "security-findings":   { roles: ["admin","editor","viewer"], perm: "security.view" },
  "maintenance-windows": { roles: ["admin","editor","viewer"], perm: "maintenance.view" },
  "search":              { roles: ["admin","editor","viewer"], perm: "search.query" },
  "status-page-admin":   { roles: ["admin","editor"],          perm: "status_page.manage" },
  "reports":             { roles: ["admin","editor","viewer"], perm: "reports.view", feature: "reports" },
  "settings":            { roles: ["admin","editor"] },
};

function RequireAccess({ path, children }) {
  const { user, hasPermission, hasFeature } = useAuth();
  const entry = ROUTE_ACCESS[path];
  if (!entry) return children;
  const role = (user?.role || "viewer").toLowerCase();
  const allowed =
    entry.roles.includes(role) &&
    (!entry.perm || hasPermission(entry.perm)) &&
    (!entry.feature || hasFeature(entry.feature));
  return allowed ? children : <Navigate to="/overview" replace />;
}

function AppRoutes() {
  const { isLoggedIn, loading } = useAuth();
  if (loading) return <SessionCheckingScreen />;
  return (
    <Routes>
      <Route path="/login" element={isLoggedIn ? <Navigate to="/overview" replace /> : <Login />} />
      {/* Public status page (2026-09-14) -- deliberately OUTSIDE
          RequireAuth, same reasoning as /login: this must render for
          a logged-out (or account-less) visitor. See
          pages/StatusPagePublic.jsx and app/api/status_page.py's
          module docstring for the sanitization boundary that makes
          this safe to expose with no auth check at all. */}
      <Route path="/status" element={<Suspense fallback={<SessionCheckingScreen />}><StatusPagePublic /></Suspense>} />
      <Route path="/" element={<RequireAuth><Layout /></RequireAuth>}>
        <Route index element={<Navigate to="/overview" replace />} />
        <Route path="overview"                  element={<RequireAccess path="overview"><Overview /></RequireAccess>} />
        <Route path="alerts"                    element={<RequireAccess path="alerts"><Alerts /></RequireAccess>} />
        <Route path="onboarding"                element={<RequireAccess path="onboarding"><AccountOnboarding /></RequireAccess>} />
        <Route path="access"                    element={<Navigate to="/access/users" replace />} />
        <Route path="access/:tab"               element={<RequireAccess path="access"><AccessControl /></RequireAccess>} />
        {/* Old URLs (bookmarks, emails, docs) keep working */}
        <Route path="users"                     element={<Navigate to="/access/users" replace />} />
        <Route path="compliance"                element={<RequireAccess path="compliance"><Compliance /></RequireAccess>} />
        <Route path="op-events"                 element={<OpEvents />} />
        <Route path="escalation-policies"       element={<RequireAccess path="escalation-policies"><EscalationPolicies /></RequireAccess>} />
        <Route path="synthetic-checks"          element={<RequireAccess path="synthetic-checks"><SyntheticChecks /></RequireAccess>} />
        <Route path="slos"                      element={<RequireAccess path="slos"><Slos /></RequireAccess>} />
        <Route path="security-findings"         element={<RequireAccess path="security-findings"><SecurityFindings /></RequireAccess>} />
        <Route path="maintenance-windows"       element={<RequireAccess path="maintenance-windows"><MaintenanceWindows /></RequireAccess>} />
        {/* <Route path="deploy-risk"               element={<DeployRisk />} /> */}
        <Route path="search"                    element={<RequireAccess path="search"><Search /></RequireAccess>} />
        <Route path="status-page-admin"         element={<RequireAccess path="status-page-admin"><StatusPageAdmin /></RequireAccess>} />
        <Route path="reports"                   element={<RequireAccess path="reports"><Reports /></RequireAccess>} />
        <Route path="settings"                  element={<RequireAccess path="settings"><Settings /></RequireAccess>} />
        <Route path="rbac-admin"                element={<Navigate to="/access/roles" replace />} />
        <Route path="accounts/:id/services"     element={<ServiceList />} />
        <Route path="accounts/:id/topology"     element={<Topology />} />
        <Route path="accounts/:id/incidents"    element={<Incidents />} />
        <Route path="accounts/:id/:service"     element={<ServiceDetailRouter />} />
        <Route path="accounts/:id"              element={<AccountDetail />} />
        {/* Unknown URL: say so (audit B1) instead of silently landing on Overview. Signed-out visitors
            never reach this: RequireAuth sends them to /login and brings them back to the URL they asked for. */}
        <Route path="*"                         element={<NotFound />} />
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    <TimezoneProvider>
      <AuthProvider>
        <BrowserRouter>
          <AppRoutes />
        </BrowserRouter>
      </AuthProvider>
    </TimezoneProvider>
  );
}