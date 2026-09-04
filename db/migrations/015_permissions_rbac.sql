-- db/migrations/015_permissions_rbac.sql
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
