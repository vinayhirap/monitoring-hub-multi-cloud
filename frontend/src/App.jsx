// monitoring-hub/frontend/src/App.jsx
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth }  from "./auth/AuthContext";
import { TimezoneProvider } from "./contexts/TimezoneContext";
import Layout            from "./components/Layout";
import Login             from "./pages/Login";
import Overview          from "./pages/Overview";
import Alerts            from "./pages/Alerts";
import AccountDetail     from "./pages/AccountDetail";
import UserManagement    from "./pages/UserManagement";
import Compliance        from "./pages/Compliance";
import Settings          from "./pages/Settings";
import AccountOnboarding from "./pages/AccountOnboarding";
import ServiceList       from "./pages/ServiceList";
import ServiceDetailRouter from "./pages/ServiceDetailRouter";
import Topology          from "./pages/Topology";
import Incidents         from "./pages/Incidents";
import OpEvents          from "./pages/OpEvents";
import EscalationPolicies from "./pages/EscalationPolicies";
import SyntheticChecks    from "./pages/SyntheticChecks";
import Slos                from "./pages/Slos";
import SecurityFindings    from "./pages/SecurityFindings";
import MaintenanceWindows  from "./pages/MaintenanceWindows";
// Deploy Risk hidden from frontend (2026-09-17, per request) -- backend
// (app/api/deploy_risk.py, app/collector/rca.py) untouched. Uncomment
// here and in components/Layout.jsx's NAV_ITEMS to re-enable.
// import DeployRisk          from "./pages/DeployRisk";
import Search              from "./pages/Search";
import Reports              from "./pages/Reports";
import StatusPageAdmin     from "./pages/StatusPageAdmin";
import StatusPagePublic    from "./pages/StatusPagePublic";
import RbacAdmin           from "./pages/RbacAdmin";

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
  if (loading) return <SessionCheckingScreen />;
  return isLoggedIn ? children : <Navigate to="/login" replace />;
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
  "users":               { roles: ["admin","editor","viewer"], perm: "users.view" },
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
  "rbac-admin":          { roles: ["admin"],                   perm: "roles.view" },
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
      <Route path="/status" element={<StatusPagePublic />} />
      <Route path="/" element={<RequireAuth><Layout /></RequireAuth>}>
        <Route index element={<Navigate to="/overview" replace />} />
        <Route path="overview"                  element={<RequireAccess path="overview"><Overview /></RequireAccess>} />
        <Route path="alerts"                    element={<RequireAccess path="alerts"><Alerts /></RequireAccess>} />
        <Route path="onboarding"                element={<RequireAccess path="onboarding"><AccountOnboarding /></RequireAccess>} />
        <Route path="users"                     element={<RequireAccess path="users"><UserManagement /></RequireAccess>} />
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
        <Route path="rbac-admin"                element={<RequireAccess path="rbac-admin"><RbacAdmin /></RequireAccess>} />
        <Route path="accounts/:id/services"     element={<ServiceList />} />
        <Route path="accounts/:id/topology"     element={<Topology />} />
        <Route path="accounts/:id/incidents"    element={<Incidents />} />
        <Route path="accounts/:id/:service"     element={<ServiceDetailRouter />} />
        <Route path="accounts/:id"              element={<AccountDetail />} />
      </Route>
      <Route path="*" element={<Navigate to="/overview" replace />} />
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