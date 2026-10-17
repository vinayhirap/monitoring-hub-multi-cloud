-- db/migrations/098_resource_env_warning.sql
--
-- Workstream C (environment governance): mismatch warnings.
--
-- resources.env_warning  "<kind>: <short sentence>" when two pieces of evidence about a resource's environment
--   disagree (its tag vs its name, a manual choice vs its tag or name, a volume vs the instance it is attached
--   to). Written by app/collector/env_sync.py from app/environment_classifier.detect_warning().
--   NULL = nothing disagrees (or not evaluated yet).
--
-- A warning is information for a person. It never changes env_resolved, so it cannot change alerting.
-- NULLable, idempotent, safe to re-run. Apply BEFORE restarting the service (the standard deploy order); a
-- restart before this migration still works, env_sync just skips warnings until the column exists.

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'resources' AND column_name = 'env_warning');
SET @sql := IF(@c = 0,
  'ALTER TABLE resources ADD COLUMN env_warning VARCHAR(200) NULL',
  'SELECT "resources.env_warning already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
