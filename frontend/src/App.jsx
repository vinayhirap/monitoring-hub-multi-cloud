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
import ServiceDetail     from "./pages/ServiceDetail";

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
      <Route path="/" element={<RequireAuth><Layout /></RequireAuth>}>
        <Route index element={<Navigate to="/overview" replace />} />
        <Route path="overview"                  element={<Overview />} />
        <Route path="alerts"                    element={<Alerts />} />
        <Route path="onboarding"                element={<AccountOnboarding />} />
        <Route path="users"                     element={<UserManagement />} />
        <Route path="compliance"                element={<Compliance />} />
        <Route path="settings"                  element={<Settings />} />
        <Route path="accounts/:id/services"     element={<ServiceList />} />
        <Route path="accounts/:id/:service"     element={<ServiceDetail />} />
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