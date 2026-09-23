-- 059_widen_alert_environment_and_pending_resource_id.sql
-- Audit B07 (alert evaluation engine).
--
-- alerts.environment and alert_pending.environment were VARCHAR(10). The
-- evaluator copies the resource's "environment"/"Environment" tag into
-- both; a tag such as "development" (11 chars) raised "Data too long"
-- under MySQL 8 strict mode and aborted the whole evaluate_alerts() cycle.
-- Widened to VARCHAR(50) (code truncates to 50 as well).
--
-- alert_pending.resource_id was VARCHAR(500) while resources.resource_id
-- and alerts.resource_id are VARCHAR(512) (migrations 016/044) -- long
-- Azure/GCP ids between 501 and 512 chars failed the same way.
--
-- Idempotent: each ALTER only runs if the column is still narrower.

SET @len := (
  SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'alerts' AND column_name = 'environment'
);
SET @sql := IF(@len IS NOT NULL AND @len < 50,
  'ALTER TABLE alerts MODIFY COLUMN environment VARCHAR(50) NULL DEFAULT ''uat''',
  'SELECT "alerts.environment already >= 50 (or missing), skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @len := (
  SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'alert_pending' AND column_name = 'environment'
);
SET @sql := IF(@len IS NOT NULL AND @len < 50,
  'ALTER TABLE alert_pending MODIFY COLUMN environment VARCHAR(50) NULL DEFAULT ''prod''',
  'SELECT "alert_pending.environment already >= 50 (or missing), skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @len := (
  SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'alert_pending' AND column_name = 'resource_id'
);
SET @sql := IF(@len IS NOT NULL AND @len < 512,
  'ALTER TABLE alert_pending MODIFY COLUMN resource_id VARCHAR(512) NOT NULL',
  'SELECT "alert_pending.resource_id already >= 512 (or missing), skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
