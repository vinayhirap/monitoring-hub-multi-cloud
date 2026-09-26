-- db/migrations/071_aws_accounts_credential_columns.sql
--
-- Audit d01 finding: aws_accounts.client_secret / gcp_service_account_key
-- are actively used throughout app/api/cspm.py, app/azure/discovery.py,
-- app/azure/provider.py, app/azure/metrics_collector.py, and
-- app/admin/accounts.py -- but the ONLY place that creates them today is
-- the repo-root 010_multi_cloud_credentials.sql, which is explicitly NOT
-- a tracked migration ("Do not run this file directly -- use
-- apply_multi_cloud_credentials.py") and lives outside db/migrations/,
-- so migrate.py's `*.sql` glob never sees it.
--
-- (aws_accounts.status was also checked against this same concern and is
-- fine: it's already present in db_schema_only.sql -- the real bootstrap
-- source setup.sh/deploy.sh actually load, per their own comments -- so
-- it doesn't need a migration here. db/schema.sql, the OTHER schema file
-- in this repo, is stale and does not reflect this; do not use it as a
-- reference for what a fresh install actually gets.)
--
-- A fresh install or disaster-recovery restore that only runs the
-- documented standard deploy path (`migrate.py apply --all-pending`)
-- would silently end up without Azure/GCP credential storage at all,
-- with every Azure/GCP account failing at the first action that needs a
-- secret. This migration promotes that same, unchanged ALTER into the
-- tracked path so it actually runs everywhere. AFTER client_id / AFTER
-- service_account_email match 009_multi_cloud_provider_columns.sql's
-- column order exactly.
--
-- Fix (audit d01 follow-up): the first version of this migration used
-- `ADD COLUMN IF NOT EXISTS`, matching the style shown in this
-- directory's own 002/003 comments -- but those files were only ever
-- applied as `baseline` (marked satisfied, never actually executed by
-- migrate.py), so that syntax was never really proven against this
-- server. It needs MySQL 8.0.29+, and dev's actual server rejected it
-- outright with a syntax error on first real use. Rewritten to use the
-- same information_schema-guarded PREPARE/EXECUTE pattern already
-- proven working here (012_alert_evaluation_hardening.sql,
-- 065_metric_catalog_unique_key_provider.sql). Same end state as
-- before, just reached in a way this server actually supports.
SET @has_client_secret := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'aws_accounts' AND column_name = 'client_secret'
);
SET @sql := IF(@has_client_secret = 0,
  'ALTER TABLE aws_accounts ADD COLUMN client_secret VARCHAR(500) DEFAULT NULL AFTER client_id',
  'SELECT "aws_accounts.client_secret already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_gcp_key := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'aws_accounts' AND column_name = 'gcp_service_account_key'
);
SET @sql := IF(@has_gcp_key = 0,
  'ALTER TABLE aws_accounts ADD COLUMN gcp_service_account_key TEXT DEFAULT NULL AFTER service_account_email',
  'SELECT "aws_accounts.gcp_service_account_key already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
