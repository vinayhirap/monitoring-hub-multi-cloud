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
import StatusPageAdmin     from "./pages/StatusPageAdmin";
import StatusPagePublic    from "./pages/StatusPagePublic";

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
        <Route path="overview"                  element={<Overview />} />
        <Route path="alerts"                    element={<Alerts />} />
        <Route path="onboarding"                element={<AccountOnboarding />} />
        <Route path="users"                     element={<UserManagement />} />
        <Route path="compliance"                element={<Compliance />} />
        <Route path="op-events"                 element={<OpEvents />} />
        <Route path="escalation-policies"       element={<EscalationPolicies />} />
        <Route path="synthetic-checks"          element={<SyntheticChecks />} />
        <Route path="slos"                      element={<Slos />} />
        <Route path="security-findings"         element={<SecurityFindings />} />
        <Route path="maintenance-windows"       element={<MaintenanceWindows />} />
        {/* <Route path="deploy-risk"               element={<DeployRisk />} /> */}
        <Route path="search"                    element={<Search />} />
        <Route path="status-page-admin"         element={<StatusPageAdmin />} />
        <Route path="settings"                  element={<Settings />} />
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