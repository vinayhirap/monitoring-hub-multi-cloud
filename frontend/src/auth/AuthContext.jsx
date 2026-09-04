// src/auth/AuthContext.jsx
import { createContext, useContext, useEffect, useState } from "react";

const AuthContext = createContext(null);
const BASE = "";

export function AuthProvider({ children }) {
  const [user, setUser]               = useState(null);
  const [loading, setLoading]         = useState(true);
  // Every permission code the current user's role grants (GET
  // /api/permissions/me) -- fetched once per session (login, or page
  // load with an existing cookie) and cached here, NOT re-derived from
  // role client-side. hasPermission() below is the one place the
  // frontend should ever ask "can this user do X" -- see
  // app/auth/permissions.py for the backend enforcement this mirrors.
  const [permissions, setPermissions] = useState(new Set());

  async function loadPermissions() {
    try {
      const res = await fetch(`${BASE}/api/permissions/me`, { credentials: "include" });
      if (res.ok) {
        const data = await res.json();
        setPermissions(new Set(data.permissions || []));
        return;
      }
    } catch {
      // fall through to clearing below
    }
    setPermissions(new Set());
  }

  // Source of truth for "who is logged in" is always the backend, not
  // anything cached client-side — the session lives in an httpOnly
  // cookie the browser attaches automatically, so on load we just ask.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${BASE}/api/auth/me`, { credentials: "include" });
        if (!cancelled) {
          const ok = res.ok;
          setUser(ok ? await res.json() : null);
          if (ok) await loadPermissions();
        }
      } catch {
        if (!cancelled) setUser(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  async function login(username, password) {
    try {
      const res = await fetch(`${BASE}/api/auth/login`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) return false;
      const data = await res.json();
      setUser({ id: data.id, username: data.username, role: data.role });
      await loadPermissions();
      return true;
    } catch {
      return false;
    }
  }

  async function logout() {
    try {
      await fetch(`${BASE}/api/auth/logout`, { method: "POST", credentials: "include" });
    } catch {
      // Even if the network call fails, still clear local state below —
      // worst case the cookie just sits there until it expires (12h).
    }
    setUser(null);
    setPermissions(new Set());
  }

  // hasPermission/hasAnyPermission/hasAllPermissions -- the ONLY
  // authorization helpers frontend code should use, instead of
  // scattering `role === "admin"` checks. These decide what to SHOW;
  // the backend (require_permission in app/auth/permissions.py)
  // independently decides what to ALLOW -- hiding a button here never
  // substitutes for that.
  function hasPermission(code) {
    return permissions.has(code);
  }
  function hasAnyPermission(codes) {
    return codes.some(c => permissions.has(c));
  }
  function hasAllPermissions(codes) {
    return codes.every(c => permissions.has(c));
  }

  return (
    <AuthContext.Provider value={{
      user, login, logout, isLoggedIn: !!user, loading,
      hasPermission, hasAnyPermission, hasAllPermissions,
    }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
