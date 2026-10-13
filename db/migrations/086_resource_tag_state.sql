-- db/migrations/084_resource_tag_state.sql
--
-- Phase 4a follow-up: tag compliance per resource (docs/TAGGING_STANDARD.md).
--
-- resources.tag_state  ok | nonstandard | invalid | missing | not_checked
--   Written by app/collector/env_sync.py from the stored tags. "not_checked" is deliberate: for
--   most resource types discovery does not store the resource's own AWS tags, so "untagged"
--   cannot be claimed. See app/environment_classifier.tags_known().
--
-- NULLable, idempotent, inert for alerting. Safe to re-run.

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'resources' AND column_name = 'tag_state');
SET @sql := IF(@c = 0,
  'ALTER TABLE resources ADD COLUMN tag_state VARCHAR(12) NULL',
  'SELECT "resources.tag_state already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
