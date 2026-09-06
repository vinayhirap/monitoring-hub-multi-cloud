#!/usr/bin/env python3
"""
apply_phase0b_frontend_session_auth.py — Phase 0b: frontend catches up
to the backend's real session auth (Phase 0 / apply_phase0_jwt_auth.py).

BEFORE this patch, the frontend:
  - Treated its own localStorage copy of {id, username, role} as the
    source of truth for "am I logged in" / "what's my role" — exactly
    the pattern Phase 0 made irrelevant server-side, since the backend
    no longer trusts anything the client claims about itself.
  - Never sent `credentials: 'include'` on any fetch call, so even
    after logging in, the httpOnly session cookie would never actually
    be attached to requests — every protected endpoint would 401.
  - api.js additionally read a `localStorage.getItem("token")` that
    nothing ever set, and sent it as `Authorization: Bearer null-ish`
    — dead code left over from an abandoned approach, same story as
    the dead app/auth/security.py we found and fixed server-side.

AFTER this patch:
  - A new frontend/src/api/httpDefaults.js patches window.fetch ONCE,
    globally, to always send `credentials: 'include'`. There are 30+
    fetch() call sites across 12 files; centralizing this in one place
    means no page can silently end up unauthenticated because someone
    forgot the option on one call site.
  - AuthContext no longer trusts localStorage for identity. On load it
    asks the backend directly (GET /api/auth/me) and uses whatever the
    server says. login()/logout() call the real endpoints; the cookie
    itself is httpOnly so JS never touches it directly.
  - App.jsx gets a "checking session" state so routes don't flash the
    login page (or the app) before that initial /me check resolves.
  - api.js drops the dead Authorization/token logic and now treats a
    401 as "session expired" — redirects to /login instead of leaving
    the caller to interpret a raw fetch error.
  - Settings.jsx's YACE-config download drops the same dead token
    logic (the global fetch patch covers it now).

Usage:
    python apply_phase0b_frontend_session_auth.py --dry-run
    python apply_phase0b_frontend_session_auth.py
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-phase0b-frontend-session-auth"


class PatchError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────
# New file
# ─────────────────────────────────────────────────────────────────────────
HTTP_DEFAULTS_JS = '''// src/api/httpDefaults.js
/**
 * The entire frontend talks to one backend over relative paths — Vite
 * proxies /api in dev, Nginx proxies /api in prod. That backend
 * authenticates via an httpOnly session cookie (see Phase 0 on the
 * backend), which only gets attached to a request if it opts in with
 * `credentials: "include"`.
 *
 * There are 30+ fetch() call sites spread across a dozen page files.
 * Rather than trust every one of them to remember that option
 * individually — exactly the kind of thing that's easy to miss on one
 * page and silently leave just that page unauthenticated — this
 * patches fetch once, globally, at app startup. Import this file for
 * its side effect only, before anything else runs (see main.jsx).
 */
const nativeFetch = window.fetch.bind(window);

window.fetch = (input, init = {}) => {
  return nativeFetch(input, { credentials: "include", ...init });
};
'''

NEW_FILES = [
    ("frontend/src/api/httpDefaults.js", HTTP_DEFAULTS_JS),
]

# ─────────────────────────────────────────────────────────────────────────
# Full-file rewrites
# ─────────────────────────────────────────────────────────────────────────
AUTHCONTEXT_OLD_ANCHOR = "cloudops_auth"
AUTHCONTEXT_NEW = '''// src/auth/AuthContext.jsx
import { createContext, useContext, useEffect, useState } from "react";

const AuthContext = createContext(null);
const BASE = "";

export function AuthProvider({ children }) {
  const [user, setUser]       = useState(null);
  const [loading, setLoading] = useState(true);

  // Source of truth for "who is logged in" is always the backend, not
  // anything cached client-side — the session lives in an httpOnly
  // cookie the browser attaches automatically, so on load we just ask.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${BASE}/api/auth/me`, { credentials: "include" });
        if (!cancelled) {
          setUser(res.ok ? await res.json() : null);
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
  }

  return (
    <AuthContext.Provider value={{ user, login, logout, isLoggedIn: !!user, loading }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
'''

API_JS_OLD_ANCHOR = 'localStorage.getItem("token")'
API_JS_NEW = '''// src/api/api.js
const BASE = "";

async function apiFetch(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  if (res.status === 401) {
    // Session expired (or never existed) — bounce to login rather
    // than leaving the caller to interpret a raw fetch failure.
    if (window.location.pathname !== "/login") {
      window.location.href = "/login";
    }
    throw new Error(`API ${path} \\u2192 401 (session expired)`);
  }
  if (!res.ok) throw new Error(`API ${path} \\u2192 ${res.status}`);
  return res.json();
}

// \u2500\u2500 Live real AWS data \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
export const getLiveAccounts  = ()   => apiFetch("/api/live/accounts");
export const getLiveEC2       = (id) => apiFetch(`/api/live/ec2/${id}`);
export const getLiveRDS       = (id) => apiFetch(`/api/live/rds/${id}`);
export const getLiveLambda    = (id) => apiFetch(`/api/live/lambda/${id}`);
export const getLiveEC2Metrics= (instanceId, region) =>
  apiFetch(`/api/live/metrics/ec2/${instanceId}${region ? `?region=${region}` : ""}`);

// \u2500\u2500 Admin \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
export const getAccounts      = ()   => apiFetch("/api/admin/accounts");
export const addAccount       = (data) => apiFetch("/api/admin/accounts", { method:"POST", body: JSON.stringify(data) });
export const discoverAccount  = (id)   => apiFetch(`/api/admin/accounts/${id}/discover`, { method:"POST" });
export const testRole         = (data) => apiFetch("/api/admin/accounts/test-role", { method:"POST", body: JSON.stringify(data) });
export const testAzureCredentials = (data) => apiFetch("/api/admin/accounts/test-azure-credentials", { method:"POST", body: JSON.stringify(data) });
export const testGcpCredentials   = (data) => apiFetch("/api/admin/accounts/test-gcp-credentials",   { method:"POST", body: JSON.stringify(data) });

// \u2500\u2500 Alerts \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
export const getAlerts = () => apiFetch("/api/alerts/open");
export const acknowledgeAlert = (id) => apiFetch(`/api/alerts/${id}/ack`,     { method: "PATCH" });
export const resolveAlert     = (id) => apiFetch(`/api/alerts/${id}/resolve`,  { method: "PATCH" });
export const muteAlert        = (id) => apiFetch(`/api/alerts/${id}/mute`,     { method: "PATCH" });

// \u2500\u2500 Audit logs \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
export const getAuditLogs     = (limit=100) => apiFetch(`/api/audit-logs?limit=${limit}`);

// \u2500\u2500 Auth \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
export const login = (username, password) =>
  apiFetch("/api/auth/login", { method:"POST", body: JSON.stringify({ username, password }) });

// \u2500\u2500 Metric catalog \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
export const getMetricCatalog        = (params = {}) => {
  const qs = new URLSearchParams(params).toString();
  return apiFetch(`/api/metric-catalog${qs ? `?${qs}` : ""}`);
};
export const getMetricCatalogServices = () => apiFetch("/api/metric-catalog/services");
export const getDefaultTemplate       = () => apiFetch("/api/metric-catalog/default-template");
export const getAccountMetrics        = (accountId) => apiFetch(`/api/account-metrics/${accountId}`);
export const saveAccountMetrics       = (accountId, enabledIds) =>
  apiFetch(`/api/account-metrics/${accountId}`, { method: "PUT", body: JSON.stringify({ enabled_metric_ids: enabledIds }) });
export const applyDefaultTemplate     = (accountId) =>
  apiFetch(`/api/account-metrics/${accountId}/apply-default`, { method: "POST" });
export const discoverNamespaceMetrics = (accountId, namespace, region) =>
  apiFetch(`/api/account-metrics/${accountId}/discover?namespace=${encodeURIComponent(namespace)}${region ? `&region=${region}` : ""}`, { method: "POST" });
export const downloadYaceConfig = (accountId, tier) =>
  `/api/account-metrics/${accountId}/yace-config${tier ? `?tier=${tier}` : ""}`;
'''

FULL_REWRITES = [
    ("frontend/src/auth/AuthContext.jsx", AUTHCONTEXT_OLD_ANCHOR, AUTHCONTEXT_NEW),
    ("frontend/src/api/api.js", API_JS_OLD_ANCHOR, API_JS_NEW),
]

# ─────────────────────────────────────────────────────────────────────────
# Anchor-based partial patches
# ─────────────────────────────────────────────────────────────────────────
PATCHES = []

MAIN_JSX_OLD = '''import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'

createRoot(document.getElementById('root')).render('''

MAIN_JSX_NEW = '''import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './api/httpDefaults.js'
import App from './App.jsx'

createRoot(document.getElementById('root')).render('''

PATCHES.append(("frontend/src/main.jsx", [(MAIN_JSX_OLD, MAIN_JSX_NEW)]))

APP_JSX_OLD = '''function RequireAuth({ children }) {
  const { isLoggedIn } = useAuth();
  return isLoggedIn ? children : <Navigate to="/login" replace />;
}

function AppRoutes() {
  const { isLoggedIn } = useAuth();
  return (
    <Routes>
      <Route path="/login" element={isLoggedIn ? <Navigate to="/overview" replace /> : <Login />} />'''

APP_JSX_NEW = '''function SessionCheckingScreen() {
  // Shown only for the brief moment while AuthContext asks the backend
  // "am I logged in" on first load — avoids flashing the login page
  // (or, worse, a protected page) before that answer comes back.
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "center",
      height: "100vh", color: "#888", fontSize: "0.95rem",
    }}>
      Checking session\u2026
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
      <Route path="/login" element={isLoggedIn ? <Navigate to="/overview" replace /> : <Login />} />'''

PATCHES.append(("frontend/src/App.jsx", [(APP_JSX_OLD, APP_JSX_NEW)]))

SETTINGS_JSX_OLD = '''    try {
      const token = localStorage.getItem("token");
      const res = await fetch(downloadYaceConfig(accountId, tier), {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });'''

SETTINGS_JSX_NEW = '''    try {
      // No manual auth header needed — the global fetch patch
      // (src/api/httpDefaults.js) already attaches the session cookie.
      const res = await fetch(downloadYaceConfig(accountId, tier));'''

PATCHES.append(("frontend/src/pages/Settings.jsx", [(SETTINGS_JSX_OLD, SETTINGS_JSX_NEW)]))


def preflight():
    print("=== Pre-flight: verifying anchors match exactly ===")
    problems = []

    for rel_path, old_anchor, _new in FULL_REWRITES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        if old_anchor not in text:
            problems.append(f"{rel_path}: expected anchor '{old_anchor}' not found")
        else:
            print(f"  OK  {rel_path}: ready for full rewrite")

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        for old, _new in replacements:
            count = text.count(old)
            if count == 0:
                problems.append(f"{rel_path}: anchor not found (0 matches)")
            elif count > 1:
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1")
            else:
                print(f"  OK  {rel_path}: anchor matched exactly once")

    for rel_path, _content in NEW_FILES:
        full_path = REPO_ROOT / rel_path
        if full_path.exists():
            print(f"  (already exists, will skip creating) {rel_path}")

    if problems:
        print("\n".join(problems))

        def _already(rel, new_text):
            p = REPO_ROOT / rel
            return p.exists() and new_text in p.read_text(encoding="utf-8")

        already_applied = all(_already(rel, new) for rel, _anchor, new in FULL_REWRITES) and all(
            _already(rel, new) for rel, repls in PATCHES for _old, new in repls
        )
        if already_applied:
            print("\nAll target text already present — patch appears to be already applied. Nothing to do.")
            sys.exit(0)
        raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")
    print("Pre-flight OK.\n")


def apply_all(dry_run: bool):
    changed_files = []
    report = []

    for rel_path, content in NEW_FILES:
        full_path = REPO_ROOT / rel_path
        if full_path.exists():
            continue
        if dry_run:
            report.append(f"[DRY RUN] would create: {rel_path}")
        else:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            report.append(f"CREATED: {rel_path}")
            changed_files.append(full_path)

    for rel_path, _old_anchor, new_content in FULL_REWRITES:
        full_path = REPO_ROOT / rel_path
        if dry_run:
            report.append(f"[DRY RUN] would fully rewrite: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(new_content, encoding="utf-8")
            report.append(f"REWROTE: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        original_text = text
        for old, new in replacements:
            if old not in text:
                raise PatchError(f"{rel_path}: expected anchor vanished mid-patch — aborting")
            text = text.replace(old, new, 1)

        if text == original_text:
            continue

        if dry_run:
            report.append(f"[DRY RUN] would patch: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(text, encoding="utf-8")
            report.append(f"PATCHED: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)

    for line in report:
        print(line)

    return changed_files


def validate_with_node(dry_run: bool):
    """
    Best-effort JS/JSX syntax check via the frontend's own toolchain
    (node + esbuild, which Vite already depends on) — skipped quietly
    if node isn't on PATH rather than failing the whole patch over a
    missing dev tool.
    """
    if dry_run:
        return
    node = shutil.which("node")
    if not node:
        print("\n(node not found on PATH — skipping JS syntax validation; "
              "`npm run build` in the next step will catch any real problem)")
        return

    print("\n=== Validating JS/JSX syntax (esbuild, via node) ===")
    esbuild_bin = REPO_ROOT / "frontend" / "node_modules" / ".bin" / ("esbuild.cmd" if sys.platform == "win32" else "esbuild")
    if not esbuild_bin.exists():
        print("(esbuild not installed yet in frontend/node_modules — run npm install, "
              "then `npm run build` will validate everything)")
        return

    files = [
        "frontend/src/api/httpDefaults.js",
        "frontend/src/auth/AuthContext.jsx",
        "frontend/src/api/api.js",
        "frontend/src/main.jsx",
        "frontend/src/App.jsx",
        "frontend/src/pages/Settings.jsx",
    ]
    for rel in files:
        full = REPO_ROOT / rel
        result = subprocess.run(
            [str(esbuild_bin), str(full), "--loader=jsx" if full.suffix == ".jsx" else "--loader=js", "--bundle=false"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise PatchError(f"SYNTAX ERROR in {rel}:\n{result.stderr}")
        print(f"  OK  {rel}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        preflight()
        changed = apply_all(args.dry_run)
        if not args.dry_run:
            validate_with_node(args.dry_run)
            print(f"\n=== Done. {len(changed)} file(s) touched. ===")
            print("\nNext:")
            print("  cd frontend && npm run dev")
            print("  (or npm run build for a production bundle)")
            print("\nLog in through the UI as normal. You should see 'Checking session\u2026'")
            print("very briefly on load, then land on /login (first visit) or /overview")
            print("(if a session cookie from an earlier test is still valid).")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
