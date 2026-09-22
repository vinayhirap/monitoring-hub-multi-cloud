-- db/migrations/055_aws_accounts_live_data_columns.sql
--
-- SCHEMA DRIFT (audit b16): app/api/live_data.py::_get_db_accounts() --
-- the query GET /api/live/accounts (the Overview dashboard, this app's
-- primary page) runs on every cache miss -- selects an explicit column
-- list: id, account_name, account_id, default_region, status, role_arn,
-- auth_mode, external_id, created_at, last_synced_at. Of those,
-- account_name, default_region, status, and last_synced_at are NOT
-- created by db/schema.sql or by any other committed file in
-- db/migrations/ (grepped the full tree to confirm -- not assumed).
--
-- These are nonetheless real, load-bearing columns the rest of the app
-- already depends on pervasively -- e.g. app/collector/discovery/
-- runner.py's own account query selects account_name directly, and
-- migration 008 (2026, long before this one) already filters
-- `acc.status = 'active'`, meaning `status` necessarily already existed
-- on whatever database that migration was written against. This is the
-- same class of gap db/migrations/002_resources_region_instance_state.sql
-- already fixed once for `resources.region`/`instance_state`: a column
-- added by hand against a live database at some point, never captured
-- in a committed migration. Any environment bootstrapped strictly from
-- schema.sql + the committed migrations (a fresh EC2 setup, exactly as
-- migrate.py's own docstring describes) hits "Unknown column" on the
-- very first dashboard load.
--
-- Purely additive; nothing here should ever fire against dev/prod,
-- where these columns already exist -- confirmed harmless there by the
-- information_schema check below. Uses the information_schema-check +
-- dynamic-SQL pattern from db/migrations/018_aws_static_key_auth.sql
-- rather than `ADD COLUMN IF NOT EXISTS` directly -- 018's own comment
-- documents that exact clause failing live on prod (MySQL 8.4.11) with
-- a syntax error for a reason never fully pinned down, so this follows
-- the pattern already proven to actually run there instead of risking
-- a repeat.

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'aws_accounts' AND column_name = 'account_name'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE aws_accounts ADD COLUMN account_name VARCHAR(100) NULL AFTER account_id',
  'SELECT "aws_accounts.account_name already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'aws_accounts' AND column_name = 'default_region'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE aws_accounts ADD COLUMN default_region VARCHAR(50) NULL AFTER account_name',
  'SELECT "aws_accounts.default_region already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'aws_accounts' AND column_name = 'status'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE aws_accounts ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT ''active''',
  'SELECT "aws_accounts.status already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'aws_accounts' AND column_name = 'last_synced_at'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE aws_accounts ADD COLUMN last_synced_at TIMESTAMP NULL DEFAULT NULL',
  'SELECT "aws_accounts.last_synced_at already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Backfill from the base schema's original columns, ONLY where those
-- source columns actually exist. HOTFIX (2026-09-22, caught live on
-- dev): the unconditional version of this backfill assumed name/
-- region_default -- db/schema.sql's original column names -- were
-- still present to copy FROM. They are not: dev's real aws_accounts
-- has neither column at all (confirmed live: "Unknown column 'name'
-- in 'field list'"), meaning account_name/default_region have been
-- the only names in use there for a long time, not a schema.sql-style
-- rename-in-place. schema.sql itself is evidently stale here in yet
-- another way beyond the 4 columns this migration already adds --
-- consistent with this repo's established pattern (see this
-- migration's own header comment on migration 002's precedent).
-- Guarding each UPDATE the same way the ADD COLUMN blocks above are
-- guarded makes this safe regardless of which of the two shapes an
-- environment happens to be in, without having to assume either one.
SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'aws_accounts' AND column_name = 'name'
);
SET @sql := IF(@col_exists > 0,
  'UPDATE aws_accounts SET account_name = name WHERE account_name IS NULL',
  'SELECT "aws_accounts.name column does not exist here, nothing to backfill account_name from"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'aws_accounts' AND column_name = 'region_default'
);
SET @sql := IF(@col_exists > 0,
  'UPDATE aws_accounts SET default_region = region_default WHERE default_region IS NULL AND region_default IS NOT NULL',
  'SELECT "aws_accounts.region_default column does not exist here, nothing to backfill default_region from"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
