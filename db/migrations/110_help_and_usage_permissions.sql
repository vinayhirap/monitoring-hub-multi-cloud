-- db/migrations/110_help_and_usage_permissions.sql
--
-- Phase E: permissions for the in-app Help Center and the Cloud API cost panel.
--
--   help.view     viewer, editor, admin   GET /api/help/sections (the server filters each section again by the
--                                         permission that section itself requires)
--   usage.view    editor, admin           GET /api/usage/cloud-cost (read-only cost summary)
--   usage.manage  admin only              reserved for future cost settings; NOT enforced by any endpoint yet
--
-- Inert until an endpoint references a code, so this cannot change behaviour on its own. Idempotent (INSERT IGNORE
-- against the unique keys on permissions.code, role_permissions(role, permission_id) and the v2 equivalent), and
-- uses nothing MySQL-8-only, so it runs on MySQL 8 and MariaDB. Seeds BOTH the legacy role_permissions table
-- (what app/auth/permissions.py reads for the route gate) and role_permissions_v2 (what app/auth/rbac.py reads for
-- scoped bindings), the same pattern as 083_environment_classification.sql.
--
-- Admin passes every permission check implicitly (app/auth/permissions.py); the admin rows exist so the permission
-- matrix screen and the v2 resolver agree with that.

INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('help.view',    'System',     'View Help Center',
   'Open the in-app Help Center. Each section is still limited to what the user''s own permissions allow', 0),
  ('usage.view',   'Operations', 'View Cloud API Cost',
   'See the read-only summary of paid and free cloud API calls made by the collector', 0),
  ('usage.manage', 'Operations', 'Manage Cloud API Cost Settings',
   'Reserved for future cost settings. No endpoint enforces it yet', 0);

-- v2 resolver
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'viewer' AND p.code IN ('help.view');

INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'editor' AND p.code IN ('help.view', 'usage.view');

INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'admin' AND p.code IN ('help.view', 'usage.view', 'usage.manage');

-- legacy role_permissions
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'viewer', id FROM permissions WHERE code IN ('help.view');
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code IN ('help.view', 'usage.view');
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code IN ('help.view', 'usage.view', 'usage.manage');
