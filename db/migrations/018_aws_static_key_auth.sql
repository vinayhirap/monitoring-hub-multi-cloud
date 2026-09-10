-- db/migrations/018_aws_static_key_auth.sql
--
-- Onboarding is adding a second AWS auth path: a per-account IAM user's
-- long-lived access key + secret key, as an alternative to a cross-account
-- AssumeRole trust relationship. frontend/src/pages/AccountOnboarding.jsx
-- already has this UI built (auth_method="access_keys", access_key/
-- secret_key form fields) but the backend has never read those fields --
-- onboarding silently ignored them and fell back to same-account/ambient
-- credentials regardless of what was submitted.
--
-- Two schema changes needed:
--
-- 1. aws_accounts.auth_mode -- lets the collector (app/aws/sts.py's new
--    get_boto3_session()) know which credential path to use for this
--    account without re-deriving it from other columns. Defaults to
--    'assume_role' so every existing row keeps its current behavior
--    unchanged.
--
-- 2. provider_credentials.provider -- this table already stores Azure
--    client secrets and GCP service-account keys as Fernet-encrypted
--    blobs (see app/credentials.py's save_credential/load_credential,
--    added in migration 010). Its ENUM explicitly excluded 'aws' on the
--    assumption AWS never needed secret storage here -- true until now.
--    Widening it lets AWS static keys reuse that exact same encrypted
--    storage path with zero changes to app/credentials.py itself: the
--    access key ID + secret access key pair is JSON-encoded into one
--    string and stored as the "raw" secret, same as GCP's full JSON key.
--
-- Fix 2026-09-10: originally wrote #1 as a single declarative
-- `ADD COLUMN IF NOT EXISTS ... AFTER role_arn`, which failed live on
-- prod (MySQL 8.4.11) with a syntax error right at "IF NOT EXISTS" --
-- despite that clause being valid MySQL 8.0.29+ syntax in isolation, so
-- the exact cause wasn't pinned down before rewriting. Rather than trust
-- that specific clause combination against a live prod DB a second time,
-- this uses the same portable information_schema-check + dynamic-SQL
-- pattern this repo's own 004_metrics_last_value_only.sql and
-- 012_alert_evaluation_hardening.sql already rely on successfully --
-- proven to actually run here, not just valid in principle.

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'aws_accounts' AND column_name = 'auth_mode'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE aws_accounts ADD COLUMN auth_mode ENUM(''assume_role'',''static_keys'') NOT NULL DEFAULT ''assume_role'' AFTER role_arn',
  'SELECT "aws_accounts.auth_mode already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

ALTER TABLE provider_credentials
    MODIFY COLUMN provider ENUM('aws','azure','gcp') NOT NULL;
