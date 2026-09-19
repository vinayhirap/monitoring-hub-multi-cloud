-- db/migrations/049_close_require_role_bypass.sql
--
-- Phase 1 of the RBAC/onboarding audit plan: closes the 12 endpoints
-- that were gated by require_role("admin") / require_role("admin",
-- "editor") instead of a delegable permission code (audit Finding 1a).
-- 11 of the 12 already have a matching code from earlier migrations
-- (015, 041); only bulk alert-clear had none -- it was riding on the
-- bare role check with no code to distinguish it from a per-alert
-- alerts.suppress. This adds that one missing code.
--
-- alerts.clear is deliberately NOT granted to editor here (nor was it
-- reachable by anything other than require_role("admin") before this
-- migration) -- clearing alerts in bulk, across an account, is a
-- higher blast-radius action than resolving/suppressing one, and
-- follows the same "stays admin-only regardless of scope" line 041
-- already drew for accounts.delete, accounts.credentials.manage and
-- the rbac.* codes. admin gets it implicitly (app.auth.permissions
-- bypasses the table lookup for role == "admin"); the row below only
-- exists so the permission-matrix UI can render admin's column
-- correctly, matching every other admin-only code in this catalog.
--
-- Purely additive; safe to re-run (INSERT IGNORE below).

INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('alerts.clear', 'Monitoring', 'Bulk-Clear Alerts', 'Clear all alerts for an account in one action', 0)
;

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code = 'alerts.clear';

INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'admin' AND p.code = 'alerts.clear';

-- groups.view (defined in 015, never granted to editor there) --
-- app/api/admin/groups.py's own module comment says the intended
-- model is "read-only listing is admin/editor; only structure changes
-- [create/update/delete, already gated by groups.create/update/
-- delete, which editor was never granted] are admin-only". The three
-- read endpoints (list groups, a user's groups, group detail) were
-- just never given a code to express that -- they ran on the bare
-- require_role("admin","editor") check instead. Granting editor
-- groups.view here lets those three switch to require_permission
-- with NO change in who can reach them.
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code = 'groups.view';

INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'editor' AND p.code = 'groups.view';
