-- db/migrations/123_two_environment_model.sql
--
-- Two environments only: PROD and NON-PROD.
--
-- Stored values   resources.env_override / env_resolved, aws_accounts.default_env,
--                 alerts.environment, alert_pending.environment, synthetic_checks.environment : 'prod' | 'nonprod'
-- Account label   aws_accounts.environment : 'PROD' | 'NON-PROD'  (display label; separate from default_env on purpose)
--
-- Mapping         prod / production / prd                                   -> prod
--                 dev, test, sit, uat, qa, staging, stage, stg, preprod,
--                 sandbox, nonprod and their long forms                     -> nonprod
--                 unknown, ignore, NULL-ish, free text nobody recognises    -> prod   (fail-safe: never weaker than prod)
-- Resources that were 'unknown' or 'ignore' keep env_source = 'unknown' so they stay in the Needs-classification queue.
--
-- Reversible: the first run copies every row it changes into env_migration_123_backup (never overwritten on a
-- re-run); db/migrations/123_two_environment_model_rollback.sql puts the old values back.
-- Idempotent: safe to run twice (a second run changes nothing).

CREATE TABLE IF NOT EXISTS env_migration_123_backup (
  tbl        VARCHAR(40)  NOT NULL,
  row_id     VARCHAR(64)  NOT NULL,
  col        VARCHAR(40)  NOT NULL,
  old_value  VARCHAR(255) NULL,
  extra      VARCHAR(255) NULL,
  saved_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (tbl, row_id, col)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Everything below uses the same two patterns; a value that matches neither is treated as PROD.
SET @NP := '^(non[ _-]?prod(uction)?|nonprd|dev(elopment)?|test(ing)?|sit|uat|qas?|stag(e|ing)|stg|pre[ _-]?prod(uction)?|sandbox)[ _-]*[0-9]*$';

-- ---- backups (INSERT IGNORE: first run wins) ------------------------------------------------------------------
INSERT IGNORE INTO env_migration_123_backup (tbl, row_id, col, old_value, extra)
  SELECT 'resources', id, 'env_override', env_override, env_source FROM resources
   WHERE env_override IS NOT NULL AND env_override NOT IN ('prod','nonprod');
INSERT IGNORE INTO env_migration_123_backup (tbl, row_id, col, old_value, extra)
  SELECT 'resources', id, 'env_resolved', env_resolved, env_source FROM resources
   WHERE env_resolved IS NOT NULL AND env_resolved NOT IN ('prod','nonprod');
INSERT IGNORE INTO env_migration_123_backup (tbl, row_id, col, old_value)
  SELECT 'aws_accounts', id, 'default_env', default_env FROM aws_accounts
   WHERE default_env IS NOT NULL AND default_env NOT IN ('prod','nonprod');
INSERT IGNORE INTO env_migration_123_backup (tbl, row_id, col, old_value)
  SELECT 'aws_accounts', id, 'environment', environment FROM aws_accounts
   WHERE environment IS NULL OR environment NOT IN ('PROD','NON-PROD');
INSERT IGNORE INTO env_migration_123_backup (tbl, row_id, col, old_value)
  SELECT 'alerts', id, 'environment', environment FROM alerts
   WHERE environment IS NULL OR environment NOT IN ('prod','nonprod');
INSERT IGNORE INTO env_migration_123_backup (tbl, row_id, col, old_value)
  SELECT 'alert_pending', id, 'environment', environment FROM alert_pending
   WHERE environment IS NULL OR environment NOT IN ('prod','nonprod');
INSERT IGNORE INTO env_migration_123_backup (tbl, row_id, col, old_value)
  SELECT 'synthetic_checks', id, 'environment', environment FROM synthetic_checks
   WHERE environment IS NULL OR environment NOT IN ('prod','nonprod');

-- ---- resources -------------------------------------------------------------------------------------------------
-- 'unknown' / 'ignore' overrides carried no usable decision: clear them and send the resource back to the queue.
UPDATE resources
   SET env_override = NULL, env_override_by = NULL, env_override_at = NULL,
       env_resolved = 'prod', env_source = 'unknown',
       env_reason = 'Migrated to the two-environment model: placed as PROD until confirmed'
 WHERE LOWER(TRIM(env_override)) IN ('unknown','ignore');

UPDATE resources
   SET env_override = IF(LOWER(TRIM(env_override)) REGEXP @NP, 'nonprod', 'prod')
 WHERE env_override IS NOT NULL AND env_override NOT IN ('prod','nonprod');

UPDATE resources
   SET env_resolved = IF(LOWER(TRIM(env_resolved)) REGEXP @NP, 'nonprod', 'prod')
 WHERE env_resolved IS NOT NULL AND env_resolved NOT IN ('prod','nonprod');

-- ---- aws_accounts ----------------------------------------------------------------------------------------------
UPDATE aws_accounts
   SET default_env = IF(LOWER(TRIM(default_env)) REGEXP @NP, 'nonprod', 'prod')
 WHERE default_env IS NOT NULL AND default_env NOT IN ('prod','nonprod');

UPDATE aws_accounts
   SET environment = IF(LOWER(TRIM(environment)) REGEXP @NP, 'NON-PROD', 'PROD')
 WHERE environment IS NULL OR environment NOT IN ('PROD','NON-PROD');

-- ---- alerts / pending / synthetic checks -----------------------------------------------------------------------
UPDATE alerts
   SET environment = IF(LOWER(TRIM(environment)) REGEXP @NP, 'nonprod', 'prod')
 WHERE environment IS NULL OR environment NOT IN ('prod','nonprod');

UPDATE alert_pending
   SET environment = IF(LOWER(TRIM(environment)) REGEXP @NP, 'nonprod', 'prod')
 WHERE environment IS NULL OR environment NOT IN ('prod','nonprod');

UPDATE synthetic_checks
   SET environment = IF(LOWER(TRIM(environment)) REGEXP @NP, 'nonprod', 'prod')
 WHERE environment IS NULL OR environment NOT IN ('prod','nonprod');

-- ---- column defaults: a row written without an environment is PROD ----------------------------------------------
ALTER TABLE alerts        MODIFY COLUMN environment VARCHAR(50) NULL DEFAULT 'prod';
ALTER TABLE alert_pending MODIFY COLUMN environment VARCHAR(50) NULL DEFAULT 'prod';
