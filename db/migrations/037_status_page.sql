-- db/migrations/037_status_page.sql
--
-- Public status page (2026-09-14) -- a simple public webpage/API
-- (status.yourcompany.com style) showing "All Systems Operational" /
-- "Degraded Performance" / "Outage" per customer-facing service, so
-- customers can self-check during an incident instead of emailing
-- support. Every serious SaaS has one (Statuspage.io, Instatus, or a
-- vendor's own build).
--
-- CRITICAL DESIGN CONSTRAINT: this is the one feature in the whole
-- app meant to be reachable with NO LOGIN (app/api/status_page.py's
-- public GET endpoint has no _auth_dep, same pattern as
-- app/api/sso.py/app/api/webhooks.py). That means it must NEVER leak
-- internal details -- no raw AWS resource IDs, no account names, no
-- alert/metric internals. status_page_components is the curation
-- layer that makes this safe: an admin picks a small, human-friendly
-- NAME ("API", "Database", "Web Dashboard") and maps it to one or more
-- real resource_ids -- only the friendly name and a computed
-- operational/degraded/outage status ever appear in the public
-- response. See app/api/status_page.py for where that sanitization is
-- enforced.
CREATE TABLE IF NOT EXISTS status_page_components (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id  BIGINT NOT NULL,
  name            VARCHAR(100) NOT NULL,   -- public-facing, e.g. "API", "Database"
  resource_ids    JSON NOT NULL,           -- array of resources.resource_id strings this maps to
  display_order   INT NOT NULL DEFAULT 0,
  enabled         TINYINT(1) NOT NULL DEFAULT 1,  -- disabled = hidden from the public page entirely
  created_by      VARCHAR(100) NULL,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_status_page_account (aws_account_id)
);

INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('status_page.manage', 'Alerts', 'Manage Status Page', 'Curate which services appear on the public status page', 0);

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code = 'status_page.manage';
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code = 'status_page.manage';
-- Deliberately NOT granted to viewer -- curating what customers see
-- publicly is an editorial action, matching this app's existing
-- convention that every write action has its own permission one rung
-- above plain viewing (see 024_topology_manage_permission.sql).
-- There is no status_page.view permission at all: reading the ADMIN
-- list of components (with real resource_ids visible) requires
-- status_page.manage; the PUBLIC status page needs no permission
-- because it needs no login, by design.
