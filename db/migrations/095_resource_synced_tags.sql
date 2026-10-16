-- db/migrations/095_resource_synced_tags.sql
--
-- Workstream B (tag sync): the AWS tags the Resource Groups Tagging API reports for a resource,
-- kept SEPARATE from resources.tags on purpose.
--
--   resources.synced_tags     JSON NULL    {"Environment": "prod", ...} as AWS reports them (aws:* tags dropped).
--                                          {} means "the sweep completed and the resource was not in it".
--   resources.tags_synced_at  DATETIME NULL  when tag_sync last confirmed that value.
--
-- Why not merge into resources.tags: discovery rewrites `tags` on every run, and the legacy alert
-- evaluator reads tags["Environment"] directly, so merging would both lose the data each cycle and
-- change alert severity with ENV_AWARE_CLASSIFICATION off.
--
-- Nothing reads these columns unless TAG_SYNC_ENABLED is on. NULLable, idempotent, safe to re-run.

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'resources' AND column_name = 'synced_tags');
SET @sql := IF(@c = 0,
  'ALTER TABLE resources ADD COLUMN synced_tags JSON NULL',
  'SELECT "resources.synced_tags already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'resources' AND column_name = 'tags_synced_at');
SET @sql := IF(@c = 0,
  'ALTER TABLE resources ADD COLUMN tags_synced_at DATETIME NULL',
  'SELECT "resources.tags_synced_at already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
