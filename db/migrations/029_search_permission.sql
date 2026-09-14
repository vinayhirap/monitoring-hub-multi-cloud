-- db/migrations/029_search_permission.sql
--
-- AIOps roadmap #11 (natural-language dashboard search, 2026-09-14).
-- New GET /api/search endpoint (app/api/nlquery.py) is read-only over
-- the exact same `alerts` data GET /api/alerts already exposes, scoped
-- through the same get_accessible_account_ids() check -- but per this
-- app's own convention (every feature gets its own permission code,
-- see db/migrations/024_topology_manage_permission.sql's reasoning),
-- it gets its own code rather than silently piggy-backing on
-- alerts.view, so it shows up distinctly in the permissions-catalog UI
-- and can be revoked independently later if ever needed (e.g. without
-- taking away plain alert viewing).
--
-- Read-only, no side effects -- granted to viewer by default, same
-- ladder position as alerts.view.
INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('search.query', 'Alerts', 'Natural-Language Search', 'Search alerts/resources using plain-English queries', 0);

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'viewer', id FROM permissions WHERE code = 'search.query';

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code = 'search.query';

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code = 'search.query';
