-- db/migrations/036_maintenance_windows.sql
--
-- Maintenance windows with topology-aware silencing (2026-09-14).
--
-- PROBLEM: planned maintenance on one resource (e.g. taking a database
-- down for a patch) is expected to make everything downstream of it
-- alert too (app servers losing their DB connection, an ALB's targets
-- failing health checks) -- without this feature, an operator has to
-- either manually mute every downstream resource one at a time, or
-- just eat the 2am page for something they scheduled themselves.
--
-- DESIGN: a maintenance window names ONE resource + a time range.
-- app/collector/maintenance.py's background job (see its own
-- docstring) walks resource_relationships OUTWARD from that resource
-- (recursively, via a WITH RECURSIVE CTE -- real multi-hop cascading,
-- not just direct dependents) to find every resource that depends on
-- it, and marks active alerts on the whole affected set as `silenced`.
--
-- SILENCING DOES NOT HIDE OR DELETE DATA -- alerts are still created,
-- still visible in the Alerts API/UI (with a silenced flag/reason so
-- the UI can badge them), still feed correlate.py/health_score.py/
-- rca.py exactly as before. The ONLY thing silencing changes is
-- app/collector/escalation.py skips sending a page/email for a
-- silenced alert -- see that file's own updated query. This preserves
-- the historical record (you can still see what alerted during a
-- maintenance window afterward) while stopping the noise that
-- actually wakes someone up.
CREATE TABLE IF NOT EXISTS maintenance_windows (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id      BIGINT NOT NULL,
  resource_id         VARCHAR(512) NOT NULL,  -- resources.resource_id -- the resource under maintenance
  reason              VARCHAR(500) NOT NULL,
  starts_at           DATETIME NOT NULL,
  ends_at             DATETIME NOT NULL,
  silence_downstream  TINYINT(1) NOT NULL DEFAULT 1,  -- also silence everything that depends on resource_id
  created_by          VARCHAR(100) NULL,
  created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_maintenance_window (aws_account_id, starts_at, ends_at)
);

-- Additive columns on the EXISTING alerts table -- nothing else that
-- reads `alerts` needs to change; a row with silenced=0 (the default,
-- backfilled for every existing alert) behaves exactly as it always
-- has. Only app/collector/escalation.py's query and
-- app/collector/maintenance.py's writer touch this column.
ALTER TABLE alerts
  ADD COLUMN silenced        TINYINT(1)   NOT NULL DEFAULT 0 AFTER status,
  ADD COLUMN silenced_reason VARCHAR(500) NULL AFTER silenced;

INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('maintenance.view',   'Alerts', 'View Maintenance Windows',   'View scheduled maintenance windows', 0),
  ('maintenance.manage', 'Alerts', 'Manage Maintenance Windows', 'Create, edit, or cancel maintenance windows', 0);

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'viewer', id FROM permissions WHERE code = 'maintenance.view';
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code IN ('maintenance.view', 'maintenance.manage');
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code IN ('maintenance.view', 'maintenance.manage');
