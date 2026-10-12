-- db/migrations/083_environment_classification.sql
--
-- Phase 4a: per-resource environment classification.
--
-- Adds, all NULLable and all idempotent (safe to re-run):
--   resources.env_override / env_override_by / env_override_at
--       a user's manual classification (prod | uat | dev | ignore). Lives on
--       the resources row, not in a side table: the discovery upserts only
--       ever UPDATE name/tags/region/last_seen_at, so it is never clobbered,
--       and it needs no string join (migration 038: new tables default to a
--       different collation than the old ones, and a resource_id join across
--       the two raises error 1267).
--   resources.env_resolved / env_source / env_reason
--       the RESULT of app/environment_classifier.resolve_environment():
--       prod | uat | dev | unknown | ignore, how it was decided
--       (manual | tag | name | account | unknown) and a short human reason.
--       Written by app/collector/env_sync.py and by the classification API,
--       read by the Needs-classification queue and (flag-gated) the alert
--       evaluator. NULL = not classified yet.
--   aws_accounts.default_env
--       the account-level default (prod | uat | dev), NULL = none set.
--       Separate from the existing aws_accounts.environment on purpose: that
--       column defaults to 'PROD' for every account, so "an admin chose PROD"
--       cannot be told apart from "nobody chose".
--
-- Nothing here changes alerting on its own: the columns are inert until
-- ENV_AWARE_CLASSIFICATION=true (see app/environment_classifier.py).
--
-- Also seeds environments.view / environments.manage.

-- -- resources.env_override ---------------------------------------------------
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'resources' AND column_name = 'env_override');
SET @sql := IF(@c = 0,
  'ALTER TABLE resources ADD COLUMN env_override VARCHAR(10) NULL',
  'SELECT "resources.env_override already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'resources' AND column_name = 'env_override_by');
SET @sql := IF(@c = 0,
  'ALTER TABLE resources ADD COLUMN env_override_by VARCHAR(100) NULL',
  'SELECT "resources.env_override_by already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'resources' AND column_name = 'env_override_at');
SET @sql := IF(@c = 0,
  'ALTER TABLE resources ADD COLUMN env_override_at DATETIME NULL',
  'SELECT "resources.env_override_at already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- -- resources.env_resolved / env_source / env_reason --------------------------
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'resources' AND column_name = 'env_resolved');
SET @sql := IF(@c = 0,
  'ALTER TABLE resources ADD COLUMN env_resolved VARCHAR(10) NULL',
  'SELECT "resources.env_resolved already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'resources' AND column_name = 'env_source');
SET @sql := IF(@c = 0,
  'ALTER TABLE resources ADD COLUMN env_source VARCHAR(10) NULL',
  'SELECT "resources.env_source already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'resources' AND column_name = 'env_reason');
SET @sql := IF(@c = 0,
  'ALTER TABLE resources ADD COLUMN env_reason VARCHAR(200) NULL',
  'SELECT "resources.env_reason already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- queue / summary lookups: "unknown resources in account X"
SET @c := (SELECT COUNT(*) FROM information_schema.statistics
           WHERE table_schema = DATABASE() AND table_name = 'resources' AND index_name = 'idx_resources_env_source');
SET @sql := IF(@c = 0,
  'ALTER TABLE resources ADD INDEX idx_resources_env_source (aws_account_id, env_source)',
  'SELECT "idx_resources_env_source already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- -- aws_accounts.default_env ------------------------------------------------------
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'aws_accounts' AND column_name = 'default_env');
SET @sql := IF(@c = 0,
  'ALTER TABLE aws_accounts ADD COLUMN default_env VARCHAR(10) NULL',
  'SELECT "aws_accounts.default_env already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- -- permissions ---------------------------------------------------------------------
INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('environments.view',   'Resources', 'View Environment Classification',
   'See each resource''s environment, how it was determined, and the Needs-classification queue', 0),
  ('environments.manage', 'Resources', 'Manage Environment Classification',
   'Mark resources prod / uat / dev / ignore and set an account''s default environment', 0);

-- viewer: read only
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'viewer' AND p.code IN ('environments.view');

-- editor: view + manage (still limited to the accounts in the editor's scope)
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'editor' AND p.code IN ('environments.view', 'environments.manage');

-- admin: everything
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'admin' AND p.code IN ('environments.view', 'environments.manage');

-- legacy role_permissions (still seeded by recent migrations, e.g. 080)
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code IN ('environments.view', 'environments.manage');
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code IN ('environments.view', 'environments.manage');
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'viewer', id FROM permissions WHERE code IN ('environments.view');
