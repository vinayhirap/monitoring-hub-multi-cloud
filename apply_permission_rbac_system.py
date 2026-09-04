#!/usr/bin/env python3
"""
apply_permission_rbac_system.py — implements the granular
permission-identifier RBAC system: L1(viewer)/L2(editor)/L3(admin),
L3 = full organization administrator (per the clarified spec — this
also reverts the earlier apply_l3_privilege_fix.py change, which was
correct for a DIFFERENT, now-superseded version of the requirement).

WHAT THIS ADDS
  - db/migrations/015_permissions_rbac.sql: `permissions` +
    `role_permissions` tables, seeded with the full permission catalog
    from the spec's access matrix, mapped to the EXISTING 3-value role
    enum (admin/editor/viewer) rather than inventing new role names —
    L1/L2/L3 groups already map onto these 1:1 via GROUP_LEVEL_ROLE.
    One deliberate, documented deviation from the literal access
    matrix: editor keeps users.view/users.create/users.update,
    preserving the already-shipped "editors can create scoped viewer
    accounts" capability rather than silently regressing it.
  - app/auth/permissions.py: has_permission(), require_permission() —
    a FastAPI dependency, same 401-then-403 chain as require_role.
    admin implicitly passes every check (can't be locked out by a seed
    data gap).
  - app/api/permissions.py: GET /api/permissions/me (what YOU have,
    powers the frontend hasPermission() helper) and GET /api/permissions
    (the full catalog + per-role grant matrix, admin-only, powers the
    redesigned Roles & Permissions screen). is_internal permissions
    (SMTP, system config) are excluded from the catalog response on
    purpose — still enforced on their real endpoints, never shown in
    the normal RBAC UI.
  - app/api/admin/groups.py: the 6 admin-only mutation endpoints
    (create/delete group, add/delete policy, add/remove members) now
    use require_permission("groups.create"/"groups.delete"/
    "groups.update") instead of require_role("admin") — same effective
    behavior today (only admin has these permissions), but now
    expressed as a named permission that role_permissions controls,
    not a hardcoded role string. The 3 read-only endpoints (list/view)
    are UNCHANGED — they already allow admin+editor and converting
    them risked a regression without a seed-data change to match;
    left as-is deliberately.
  - app/api/admin/users.py: PATCH /{user_id}/role (admin-only, zero
    ambiguity) converted to require_permission("roles.manage"). Every
    other endpoint in this file is UNCHANGED — they intentionally
    allow both admin and editor (existing delegation feature), and
    role_permissions already reflects that reality rather than the
    code needing to change.
  - frontend/src/auth/AuthContext.jsx: fetches GET /api/permissions/me
    once per session (on load and after login) and exposes
    hasPermission(code) / hasAnyPermission([...]) /
    hasAllPermissions([...]) — the ONE place frontend authorization
    decisions should be made, replacing scattered role checks.
  - frontend/src/components/Layout.jsx: the "User Management" nav item
    is now gated by hasPermission("users.view") in addition to its
    existing role array — demonstrating the intended pattern (`perm:`
    field + hasPermission) for any nav item that gets its own
    permission code going forward.
  - frontend/src/pages/UserManagement.jsx: the Roles & Permissions tab
    is completely redesigned — no longer 3 hardcoded RoleCard
    components with a static permission list, now a live, categorized
    matrix fetched from GET /api/permissions showing exactly which of
    L1/L2/L3 each real permission is granted to. Also reverts
    GROUP_LEVEL_ROLE's L3 mapping back to "admin" (see note above).

WHAT THIS DELIBERATELY DOES NOT DO (and why)
  - Does NOT convert every other protected endpoint in the app
    (alerts.py, live_data.py, settings.py, accounts.py,
    metric_catalog.py, audit_logs.py, ...) from require_role to
    require_permission. Those are ALREADY correctly enforcing 401/403
    today via require_role — nothing is unprotected. Converting ~10
    more files blind, with no way to run your actual authenticated
    browser sessions against them from this environment, is exactly
    the kind of risk the spec's own "do not break existing
    functionality" rule warns against. Once this foundation is
    confirmed working end-to-end on your server, converting the rest
    file-by-file is straightforward and low-risk — happy to do that
    as a follow-up once you've verified this part.
  - Does NOT add an "editable from the UI" permission-management
    screen (drag permissions on/off a role in the browser). The Roles
    & Permissions tab is read-only in this pass, reflecting
    role_permissions — changing a role's grants is a migration/SQL
    change for now, not a button.
  - Does NOT add multi-organization isolation (spec section 15). This
    application has always been single-organization/single-tenant —
    there is no `organizations` table, and no other part of the schema
    is organization-scoped. Adding that is a materially different,
    much larger change than "add permissions to the existing RBAC,"
    and was not present anywhere else in this codebase to extend.

Usage:
    python apply_permission_rbac_system.py --dry-run
    python apply_permission_rbac_system.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-permission-rbac-system"


class PatchError(Exception):
    pass


MIGRATION_SQL = r'''-- db/migrations/015_permissions_rbac.sql
--
-- Adds a granular permission-identifier layer ON TOP OF the existing
-- role system (users.role ENUM('admin','editor','viewer')) rather
-- than replacing it. Nothing about how a user GETS a role changes --
-- direct assignment, or via L1/L2/L3 group membership through
-- app.auth.authorization.GROUP_LEVEL_ROLE -- this only makes what
-- each role can DO expressible as named permissions (users.create,
-- groups.manage, ...) that app.auth.permissions.require_permission()
-- can check, instead of role checks scattered through every endpoint.
--
-- role_permissions keys on the EXISTING 3-value role enum on purpose
-- (not a new "L1 Role"/"L2 Role"/"L3 Role" table) -- L1/L2/L3 groups
-- already map onto viewer/editor/admin one-for-one via
-- GROUP_LEVEL_ROLE, so a permission granted to "admin" is
-- automatically what an L3 group member gets, without a second
-- indirection layer to keep in sync.
--
-- is_internal marks permissions that must NEVER appear in the normal
-- RBAC permission-management UI (SMTP, system config) even though
-- they're still enforced on their actual endpoints -- GET
-- /api/permissions filters these out; GET /api/permissions/me (what a
-- user actually has) does not, since enforcement doesn't care about
-- UI visibility.
--
-- Purely additive; safe to re-run (INSERT IGNORE below).

CREATE TABLE IF NOT EXISTS permissions (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  code        VARCHAR(100) NOT NULL,
  category    VARCHAR(50)  NOT NULL,
  label       VARCHAR(150) NOT NULL,
  description VARCHAR(255) DEFAULT NULL,
  is_internal TINYINT(1) NOT NULL DEFAULT 0,
  UNIQUE KEY uniq_permission_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS role_permissions (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  role          ENUM('admin','editor','viewer') NOT NULL,
  permission_id BIGINT NOT NULL,
  UNIQUE KEY uniq_role_permission (role, permission_id),
  CONSTRAINT fk_role_permissions_permission
    FOREIGN KEY (permission_id) REFERENCES permissions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ── Permission catalog ──────────────────────────────────────────────
INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('dashboard.view',          'Monitoring',  'View Dashboard',            'Infrastructure overview page', 0),
  ('accounts.view',           'Resources',   'View AWS Accounts',         'See onboarded accounts and their health', 0),
  ('resources.view',          'Resources',   'View Resources',            'EC2/RDS/Lambda/etc. resource listings', 0),
  ('metrics.view',            'Monitoring',  'View Metrics',              'CloudWatch metric charts', 0),
  ('alerts.view',             'Monitoring',  'View Alerts',                'Active/resolved alert list', 0),
  ('monitoring.advanced',     'Monitoring',  'Advanced Monitoring',        'Deeper drilldowns and diagnostics', 0),
  ('operations.view',         'Operations',  'View Operations',            'Operational action surfaces', 0),
  ('operations.execute',      'Operations',  'Execute Operations',         'Perform operational actions', 0),
  ('troubleshooting.execute', 'Operations',  'Troubleshooting Actions',    'Run troubleshooting workflows', 0),
  ('alerts.configure',        'Operations',  'Configure Alert Thresholds', 'Create/edit warning & critical thresholds', 0),
  ('accounts.onboard',        'Operations',  'Onboard AWS Accounts',       'Add new AWS accounts for monitoring', 0),
  ('users.view',               'User Management', 'View Users',            'See the user list', 0),
  ('users.create',             'User Management', 'Create Users',          'Add new user accounts', 0),
  ('users.update',             'User Management', 'Update Users',          'Change an existing user''s role/access', 0),
  ('users.delete',             'User Management', 'Delete Users',          'Remove a user account', 0),
  ('groups.view',              'RBAC Administration', 'View Groups',        'See the L1/L2/L3 organization group tree', 0),
  ('groups.create',            'RBAC Administration', 'Create Groups',      'Add a new L1/L2/L3 group', 0),
  ('groups.update',            'RBAC Administration', 'Update Groups',      'Edit a group''s policy/membership', 0),
  ('groups.delete',            'RBAC Administration', 'Delete Groups',      'Remove a group', 0),
  ('roles.view',               'RBAC Administration', 'View Roles',         'See the role/permission matrix', 0),
  ('roles.manage',             'RBAC Administration', 'Manage Roles',       'Change a user''s role directly', 0),
  ('permissions.view',         'RBAC Administration', 'View Permissions',   'See the permission catalog', 0),
  ('permissions.manage',       'RBAC Administration', 'Manage Permissions', 'Change which permissions a role grants', 0),
  ('rbac.policy.view',         'RBAC Administration', 'View RBAC Policies', 'See account/region access scopes', 0),
  ('rbac.policy.manage',       'RBAC Administration', 'Manage RBAC Policies', 'Grant/revoke account/region access', 0),
  ('audit.view',                'RBAC Administration', 'View Audit Logs',   'See the administrative action log', 0),
  ('organization.settings.view',   'Organization', 'View Organization Settings',   'See org-level configuration', 0),
  ('organization.settings.update', 'Organization', 'Update Organization Settings', 'Change org-level configuration', 0),
  ('system.admin',        'System', 'System Administration', 'Unrestricted system-level access', 1),
  ('system.smtp.manage',  'System', 'Manage SMTP/Mail',       'Configure outbound email', 1),
  ('system.config.manage','System', 'Manage Internal Configuration', 'Internal/infra configuration', 1)
;

-- ── Role -> permission mapping ───────────────────────────────────────
-- viewer (L1): monitoring/read-only surfaces only.
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'viewer', id FROM permissions WHERE code IN (
  'dashboard.view','accounts.view','resources.view','metrics.view','alerts.view'
);

-- editor (L2): everything viewer has, PLUS advanced monitoring and
-- operational actions. Also includes users.view/users.create/
-- users.update -- NOT part of the abstract L1/L2/L3 access matrix,
-- but a deliberate, explicit exception preserving an existing,
-- already-shipped capability: editors can already create/manage
-- VIEWER accounts within their own access scope (app/api/admin/
-- users.py, unchanged by this migration). Removing that here would
-- be an undocumented regression, which the "do not break existing
-- functionality" rule takes precedence over the abstract matrix for.
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code IN (
  'dashboard.view','accounts.view','resources.view','metrics.view','alerts.view',
  'monitoring.advanced','operations.view','operations.execute','troubleshooting.execute',
  'alerts.configure','accounts.onboard',
  'users.view','users.create','users.update'
);

-- admin (L3): full organization administrator -- gets everything,
-- including internal/system permissions, PLUS it's granted implicit
-- bypass of the permission check entirely in app.auth.permissions
-- (has_permission short-circuits True for role == "admin") so a gap
-- in this seed list can never lock an admin out of their own system.
-- These rows still exist so the catalog UI can render admin's column
-- as fully checked without special-casing it client-side.
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions;
'''

PERMISSIONS_PY = r'''# app/auth/permissions.py
"""
app/auth/permissions.py

Granular permission-identifier RBAC, layered ON TOP OF the existing
role system (admin/editor/viewer) rather than replacing it --
role_permissions (db/migrations/015_permissions_rbac.sql) maps each of
the 3 existing roles to a set of permission codes. Nothing about how a
user GETS a role changes here -- direct assignment, or via L1/L2/L3
group membership through app.auth.authorization.GROUP_LEVEL_ROLE --
this only makes what that role can DO expressible as named permissions
(users.create, groups.manage, ...) instead of role checks scattered
through every endpoint.

admin implicitly has every permission (bypasses the table lookup
entirely) -- deliberate: a gap in role_permissions seed data can never
lock an admin out of their own system, which would be a far worse
failure mode than the table needing to explicitly list every admin
permission. Anything genuinely admin-only should still be gated with
require_permission(...) as normal; admin always passes.
"""
import logging

from fastapi import Depends, HTTPException
from app.auth.deps import get_current_user
from app.db import get_connection

logger = logging.getLogger(__name__)


def get_role_permissions(role: str) -> set:
    """All permission codes granted to a role. admin -> every code
    that exists (see module docstring for why)."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if role == "admin":
            cursor.execute("SELECT code FROM permissions")
        else:
            cursor.execute(
                "SELECT p.code FROM role_permissions rp "
                "JOIN permissions p ON p.id = rp.permission_id "
                "WHERE rp.role = %s",
                (role,),
            )
        return {r["code"] for r in cursor.fetchall()}
    finally:
        cursor.close()
        conn.close()


def has_permission(user: dict, code: str) -> bool:
    if user.get("role") == "admin":
        return True
    return code in get_role_permissions(user.get("role"))


def require_permission(code: str):
    """
    Depends(require_permission("users.create")) -- 403s if the
    authenticated user's role doesn't grant this permission.
    get_current_user (run first, as this function's own dependency)
    already 401s for a missing/invalid session, so the ordering here
    is exactly: 401 (not authenticated) -> 403 (authenticated, but
    lacking this permission) -> the endpoint itself, matching the
    chain called for in the RBAC spec.
    """
    def _check(user: dict = Depends(get_current_user)) -> dict:
        if not has_permission(user, code):
            raise HTTPException(status_code=403, detail=f"Missing permission: {code}")
        return user
    return _check
'''

API_PERMISSIONS_PY = r'''# app/api/permissions.py
"""
app/api/permissions.py

Read-only endpoints backing:
  - GET /api/permissions/me  -- every permission code the CURRENT
    user's role grants. Fetched once at login and cached in
    AuthContext; frontend hasPermission()/hasAnyPermission()/
    hasAllPermissions() are built from this, not from re-deriving
    role logic client-side.
  - GET /api/permissions     -- the full catalog, grouped by category,
    with a per-role granted/not-granted matrix -- what the redesigned
    Roles & Permissions admin screen renders. is_internal permissions
    (SMTP, system config) are deliberately excluded from this response
    -- they're still enforced on their actual endpoints via
    require_permission(...), just never surfaced in the normal RBAC
    permission-management UI.
"""
from fastapi import APIRouter, Depends
from app.db import get_connection
from app.auth.deps import get_current_user, require_role
from app.auth.permissions import get_role_permissions

router = APIRouter(prefix="/api/permissions", tags=["Permissions"])


@router.get("/me")
def my_permissions(current_user: dict = Depends(get_current_user)):
    codes = get_role_permissions(current_user["role"])
    return {"role": current_user["role"], "permissions": sorted(codes)}


@router.get("")
def list_permissions(current_user: dict = Depends(require_role("admin"))):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, code, category, label, description FROM permissions "
        "WHERE is_internal = 0 ORDER BY category, code"
    )
    perms = cursor.fetchall()

    cursor.execute("SELECT role, permission_id FROM role_permissions")
    grants = cursor.fetchall()
    cursor.close()
    conn.close()

    granted_by_role = {"viewer": set(), "editor": set(), "admin": set()}
    for g in grants:
        granted_by_role.setdefault(g["role"], set()).add(g["permission_id"])

    categories = {}
    for p in perms:
        bucket = categories.setdefault(p["category"], [])
        bucket.append({
            "id": p["id"], "code": p["code"], "label": p["label"],
            "description": p["description"],
            "roles": {
                "viewer": p["id"] in granted_by_role["viewer"],
                "editor": p["id"] in granted_by_role["editor"],
                # admin implicitly has every permission (see
                # app.auth.permissions module docstring) -- always
                # rendered as granted regardless of the row's literal
                # presence in role_permissions.
                "admin":  True,
            },
        })
    return [{"category": cat, "permissions": items} for cat, items in categories.items()]
'''

NEW_FILES = [
    ("db/migrations/015_permissions_rbac.sql", MIGRATION_SQL),
    ("app/auth/permissions.py", PERMISSIONS_PY),
    ("app/api/permissions.py", API_PERMISSIONS_PY),
]

PATCHES = [
    (
        "app/main.py",
        [
            (r'''from app.api.admin.groups   import router as admin_groups_router
''', r'''from app.api.admin.groups   import router as admin_groups_router
from app.api.permissions    import router as permissions_router
'''),
            (r'''app.include_router(admin_groups_router)
''', r'''app.include_router(admin_groups_router)
app.include_router(permissions_router)
'''),
        ],
    ),
    (
        "app/api/admin/groups.py",
        [
            (r'''from app.auth.deps import require_role
''', r'''from app.auth.deps import require_role
from app.auth.permissions import require_permission
'''),
            (r'''def create_group(payload: dict = Body(...), current_user: dict = Depends(require_role("admin"))):
''', r'''def create_group(payload: dict = Body(...), current_user: dict = Depends(require_permission("groups.create"))):
'''),
            (r'''def delete_group(group_id: int, current_user: dict = Depends(require_role("admin"))):
''', r'''def delete_group(group_id: int, current_user: dict = Depends(require_permission("groups.delete"))):
'''),
            (r'''def add_group_policy(group_id: int, payload: dict = Body(...), current_user: dict = Depends(require_role("admin"))):
''', r'''def add_group_policy(group_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("groups.update"))):
'''),
            (r'''def delete_group_policy(policy_id: int, current_user: dict = Depends(require_role("admin"))):
''', r'''def delete_group_policy(policy_id: int, current_user: dict = Depends(require_permission("groups.update"))):
'''),
            (r'''def add_group_members(group_id: int, payload: dict = Body(...), current_user: dict = Depends(require_role("admin"))):
''', r'''def add_group_members(group_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("groups.update"))):
'''),
            (r'''def remove_group_member(group_id: int, user_id: int, current_user: dict = Depends(require_role("admin"))):
''', r'''def remove_group_member(group_id: int, user_id: int, current_user: dict = Depends(require_permission("groups.update"))):
'''),
        ],
    ),
    (
        "app/api/admin/users.py",
        [
            (r'''from app.auth.deps import require_role
from app.auth import authorization as authz
''', r'''from app.auth.deps import require_role
from app.auth.permissions import require_permission
from app.auth import authorization as authz
'''),
            (r'''def update_role(user_id: int, payload: dict = Body(...), current_user: dict = Depends(require_role("admin"))):
''', r'''def update_role(user_id: int, payload: dict = Body(...), current_user: dict = Depends(require_permission("roles.manage"))):
'''),
        ],
    ),
    (
        "frontend/src/auth/AuthContext.jsx",
        [
            (r'''export function AuthProvider({ children }) {
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
''', r'''export function AuthProvider({ children }) {
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
'''),
        ],
    ),
    (
        "frontend/src/components/Layout.jsx",
        [
            (r'''const NAV_ITEMS = [
  { to: "/overview",   label: "Overview",           icon: OverviewIcon,   roles: ["admin","editor","viewer"] },
  { to: "/alerts",     label: "Alerts",             icon: AlertIcon,      roles: ["admin","editor","viewer"], badge: true },
  { to: "/onboarding", label: "Account Onboarding", icon: OnboardIcon,    roles: ["admin","editor"] },
  { to: "/users",      label: "User Management",    icon: UsersIcon,      roles: ["admin"] },
  { to: "/compliance", label: "Compliance",         icon: ComplianceIcon, roles: ["admin","editor","viewer"] },
  { to: "/settings",   label: "Settings",           icon: SettingsIcon,   roles: ["admin","editor"] },
];
''', r'''// `roles` stays the filter for general app pages (unrelated to the
// RBAC permission matrix below). User Management is the one item this
// spec's permission model actually covers, so it's filtered by
// `perm` (checked via hasPermission from AuthContext) instead of a
// role array -- see visibleNav below. This is the intended pattern
// going forward: add `perm: "some.code"` to a nav item instead of
// widening `roles`, as more of the app gets its own permission codes.
const NAV_ITEMS = [
  { to: "/overview",   label: "Overview",           icon: OverviewIcon,   roles: ["admin","editor","viewer"] },
  { to: "/alerts",     label: "Alerts",             icon: AlertIcon,      roles: ["admin","editor","viewer"], badge: true },
  { to: "/onboarding", label: "Account Onboarding", icon: OnboardIcon,    roles: ["admin","editor"] },
  { to: "/users",      label: "User Management",    icon: UsersIcon,      roles: ["admin","editor","viewer"], perm: "users.view" },
  { to: "/compliance", label: "Compliance",         icon: ComplianceIcon, roles: ["admin","editor","viewer"] },
  { to: "/settings",   label: "Settings",           icon: SettingsIcon,   roles: ["admin","editor"] },
];
'''),
            (r'''  const { user, logout } = useAuth();
''', r'''  const { user, logout, hasPermission } = useAuth();
'''),
            (r'''  const visibleNav = NAV_ITEMS.filter(item => item.roles.includes(role));
''', r'''  // An item with a `perm` must pass the real permission check (not just
  // the role array) -- role stays a coarse first filter for everything
  // else, `perm` is the actual RBAC-spec authorization decision where
  // it's defined. Backend enforcement (app/auth/permissions.py) is
  // independent of this either way; this only controls what's shown.
  const visibleNav = NAV_ITEMS.filter(item =>
    item.roles.includes(role) && (!item.perm || hasPermission(item.perm))
  );
'''),
        ],
    ),
    (
        "frontend/src/pages/UserManagement.jsx",
        [
            (r'''  useEffect(() => { loadAccountsAndGroups(); }, [loadAccountsAndGroups]);
  useEffect(() => { if (showAdd) loadAccountsAndGroups(); }, [showAdd, loadAccountsAndGroups]);

  useEffect(() => { loadUsers(); }, [loadUsers]);
''', r'''  useEffect(() => { loadAccountsAndGroups(); }, [loadAccountsAndGroups]);
  useEffect(() => { if (showAdd) loadAccountsAndGroups(); }, [showAdd, loadAccountsAndGroups]);

  useEffect(() => { loadUsers(); }, [loadUsers]);

  // Roles & Permissions tab -- the real permission catalog + per-role
  // grant matrix from the backend (GET /api/permissions), not a
  // hardcoded frontend list. is_internal permissions (SMTP, system
  // config) are excluded server-side before this ever arrives -- see
  // app/api/permissions.py -- so there's nothing to filter here.
  const [permCategories, setPermCategories] = useState([]);
  useEffect(() => {
    if (tab === "roles" && isAdmin) {
      apiFetch("/api/permissions").then(setPermCategories).catch(() => setPermCategories([]));
    }
  }, [tab, isAdmin]);
'''),
            (r'''      {/* Roles Tab */}
      {tab === "roles" && (
        <div className="roles-grid">
          <RoleCard title="Admin Role"  icon={ToolIcon} color="orange" desc="Unrestricted access to all platform features including account onboarding, user management, alert configuration, and audit logs." granted={ADMIN_PERMS} denied={[]} />
          <RoleCard title="Editor Role" icon={EditIcon} color="purple" desc="Monitor infrastructure, configure alerts, and onboard accounts. Cannot manage users or access audit logs."                        granted={EDITOR_PERMS} denied={EDITOR_DENIED} />
          <RoleCard title="Viewer Role" icon={EyeIcon}  color="blue"   desc="Monitor account health, view metrics, drill into services, and read alerts. Cannot modify configuration or onboard accounts."  granted={VIEWER_PERMS} denied={VIEWER_DENIED} />
        </div>
      )}
''', r'''      {/* Roles Tab -- real permission catalog from the backend
          (GET /api/permissions), grouped by category, showing exactly
          which of Viewer(L1)/Editor(L2)/Admin(L3) each permission is
          granted to. This is a READ-ONLY view of role_permissions in
          this pass -- changing what a role grants is a database
          change (db/migrations/015_permissions_rbac.sql), not yet
          editable from here. */}
      {tab === "roles" && (
        <div className="roles-grid" style={{ gridTemplateColumns: "1fr 1fr", alignItems: "start" }}>
          {permCategories.length === 0 ? (
            <div style={{ gridColumn: "1 / -1", padding: 24, color: "var(--text-muted)", fontSize: 13 }}>
              Loading permission catalog…
            </div>
          ) : (
            permCategories.map(cat => (
              <div key={cat.category} className="role-card">
                <div className="role-card-header">
                  <span className="role-card-title">{cat.category}</span>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  {cat.permissions.map(p => (
                    <div key={p.code} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }} title={p.description || ""}>
                      <span style={{ fontSize: 12, fontWeight: 600 }}>{p.label}</span>
                      <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                        <span className={`perm-chip ${p.roles.viewer ? "granted" : "denied"}`}>L1</span>
                        <span className={`perm-chip ${p.roles.editor ? "granted" : "denied"}`}>L2</span>
                        <span className={`perm-chip ${p.roles.admin ? "granted" : "denied"}`}>L3</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))
          )}
        </div>
      )}
'''),
        ],
    ),
]

# ── L3 mapping: handled separately from PATCHES above ───────────────
# GROUP_LEVEL_ROLE's L3 entry could currently be in either of two
# states depending on whether apply_l3_privilege_fix.py (from the
# PREVIOUS, now-superseded spec) was ever run on this server:
#   "admin"  -- original value from apply_org_group_rbac.py, if the
#               interim fix was never applied here
#   "editor" -- if it WAS applied
# Both must end up as "admin" (this spec's final answer). Handled with
# its own lenient logic instead of the strict single-anchor PATCHES
# system, since either starting state is valid and neither should
# abort the rest of this script.
L3_MAPPING_TARGETS = [
    ("app/auth/authorization.py",
     'GROUP_LEVEL_ROLE = {"L1": "viewer", "L2": "editor", "L3": "%s"}'),
    ("frontend/src/pages/UserManagement.jsx",
     'const GROUP_LEVEL_ROLE = { L1: "viewer", L2: "editor", L3: "%s" };'),
]

# ─────────────────────────────────────────────────────────────────────────
# Preflight / apply / validate
# ─────────────────────────────────────────────────────────────────────────

def preflight():
    print("=== Pre-flight: verifying anchors match exactly ===")
    problems = []

    for rel_path, content in NEW_FILES:
        full_path = REPO_ROOT / rel_path
        if full_path.exists():
            print(f"  (already exists, will skip creating) {rel_path}")
        else:
            print(f"  OK  {rel_path}: will be created")

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        for old, _new in replacements:
            count = text.count(old)
            if count == 0:
                problems.append(f"{rel_path}: anchor not found (0 matches) — {old[:70]!r}")
            elif count > 1:
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1")
            else:
                print(f"  OK  {rel_path}: anchor matched exactly once")

    for rel_path, pattern in L3_MAPPING_TARGETS:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        if (pattern % "admin") in text or (pattern % "editor") in text:
            print(f"  OK  {rel_path}: L3 mapping in a recognized state")
        else:
            problems.append(f"{rel_path}: GROUP_LEVEL_ROLE line not found in either expected state")

    if problems:
        print("\n".join(problems))

        def _already(rel, new_text):
            p = REPO_ROOT / rel
            return p.exists() and new_text in p.read_text(encoding="utf-8")

        already_applied = all(_already(rel, new) for rel, repls in PATCHES for _old, new in repls)
        if already_applied:
            print("\nAll target text already present — patch appears to be already applied. Nothing to do.")
            sys.exit(0)
        raise PatchError("Pre-flight failed — aborting before touching any file. See problems above.")
    print("Pre-flight OK.\n")


def apply_l3_mapping(dry_run: bool):
    changed = []
    for rel_path, pattern in L3_MAPPING_TARGETS:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        admin_form  = pattern % "admin"
        editor_form = pattern % "editor"
        if admin_form in text:
            print(f"  (already correct) {rel_path}: L3 -> admin")
            continue
        if dry_run:
            print(f"[DRY RUN] would fix L3 mapping in {rel_path}: editor -> admin")
            continue
        backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
        if not backup_path.exists():
            shutil.copy2(full_path, backup_path)
        new_text = text.replace(editor_form, admin_form, 1)
        full_path.write_text(new_text, encoding="utf-8")
        print(f"PATCHED: {rel_path}  (L3 mapping: editor -> admin)")
        changed.append(full_path)
    return changed


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

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        original_text = text
        for old, new in replacements:
            if new in text:
                continue
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

    changed_files.extend(apply_l3_mapping(dry_run))
    return changed_files


def validate_python_syntax(changed_files):
    print("\n=== Validating Python syntax (py_compile) ===")
    for f in changed_files:
        if f.suffix != ".py":
            continue
        try:
            py_compile.compile(str(f), doraise=True)
            print(f"  OK  {f.relative_to(REPO_ROOT)}")
        except py_compile.PyCompileError as e:
            raise PatchError(f"SYNTAX ERROR after patching {f}: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        preflight()
        changed = apply_all(args.dry_run)
        if not args.dry_run:
            validate_python_syntax(changed)
            print(f"\n=== Done. {len(changed)} file(s) touched. ===")
            print("\nNext steps:")
            print("  1. Run the new migration:")
            print("     mysql -umonitor -proot123 monitoring_hub < db/migrations/015_permissions_rbac.sql")
            print("  2. Verify the catalog seeded correctly:")
            print("     mysql -umonitor -proot123 monitoring_hub -e \"SELECT role, COUNT(*) FROM role_permissions GROUP BY role;\"")
            print("  3. cd frontend && npm install && npm run build")
            print("  4. Full backend restart: sudo systemctl restart monitoring-hub")
            print("\nVerification (run these for real — do not assume):")
            print("  # As an L1 (viewer) user, this must 403:")
            print("  curl -s -i -b viewer_cookies.txt -X POST http://127.0.0.1:8000/api/groups \\")
            print("    -H 'Content-Type: application/json' -d '{\"name\":\"test\",\"level\":\"L1\"}'")
            print("  # As admin, GET /api/permissions/me should list every catalog permission:")
            print("  curl -s -b admin_cookies.txt http://127.0.0.1:8000/api/permissions/me")
            print("  # As editor, GET /api/permissions/me should NOT include groups.create/roles.manage:")
            print("  curl -s -b editor_cookies.txt http://127.0.0.1:8000/api/permissions/me")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
