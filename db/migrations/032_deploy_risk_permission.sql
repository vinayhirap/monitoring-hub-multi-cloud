-- db/migrations/032_deploy_risk_permission.sql
--
-- Deploy-risk correlation (2026-09-14) -- read side. New
-- GET /api/deploy-risk (app/api/deploy_risk.py) lists recent
-- deployments (ingested via POST /api/webhooks/deploy, see
-- app/api/webhooks.py) alongside how many alerts followed each one,
-- so a team can see "which of our last N deploys were actually risky"
-- at a glance instead of piecing it together alert-by-alert.
--
-- Own permission, not piggy-backed on alerts.view, following this
-- app's existing convention (see 024_topology_manage_permission.sql /
-- 029_search_permission.sql) -- read-only, granted to every role by
-- default, same ladder position as alerts.view.
INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('deploy_risk.view', 'Alerts', 'View Deploy Risk', 'View recent deployments and the alerts that followed them', 0);

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'viewer', id FROM permissions WHERE code = 'deploy_risk.view';
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code = 'deploy_risk.view';
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code = 'deploy_risk.view';
