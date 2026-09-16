-- db/migrations/045_fix_resources_cross_account_unique_key.sql
--
-- resources' live unique key is `uniq_resource (resource_id, resource_type)`
-- -- it does NOT include aws_account_id. Migration 017 was recorded in
-- schema_migrations as APPLIED via the (baseline) path -- meaning it was
-- assumed already satisfied when migrate.py was first pointed at this
-- database, never actually verified or executed here. `SHOW INDEX FROM
-- resources` on prod confirms the correct composite key
-- (`uniq_resource_identity`, covering aws_account_id too) was never
-- actually created on this box; the old two-column key was still live
-- underneath the false "APPLIED" record.
--
-- Impact (confirmed live, 2026-09-16, AuroGov Mumbai / U4RAD):
-- app/collector/discovery/*.py's _upsert_resource() does
--   INSERT ... ON DUPLICATE KEY UPDATE
-- keyed only on (resource_id, resource_type). Whenever two different
-- monitored AWS accounts happen to have a resource with the identical
-- name -- routine for AWS-managed defaults like the "System" log group,
-- or "cid-DataExportCreator"/"cid-CID-Analytics-DataExports" (stock
-- names from AWS's own Cost Intelligence Dashboard QuickStart, deployed
-- identically in many accounts) -- whichever account's discovery cycle
-- writes that resource_id FIRST permanently owns that row. Every other
-- account sharing the same resource name only ever updates the first
-- account's row (name/tags/region/last_seen_at) and never gets a row of
-- its own, because the ON DUPLICATE KEY UPDATE clause does not (and must
-- not) touch aws_account_id. This silently under-counts every OTHER
-- account's resources for that resource_id/type, with no error raised
-- anywhere -- not specific to CloudWatch Logs, U4RAD, or this one
-- incident; any resource_type/resource_id pair shared by two accounts
-- is affected.
--
-- This migration:
--   1. Adds the correct composite key (aws_account_id, resource_type,
--      resource_id). No existing rows can violate it -- the old key
--      already guaranteed at most one row per (resource_id,
--      resource_type) across the whole table, and a 3-column key that
--      is a strict superset of a 2-column key can never reject a row
--      the narrower key already accepted.
--   2. Drops the old, incorrect 2-column key so it stops shadowing the
--      correct one going forward.
--
-- IMPORTANT -- this does NOT retroactively recover data: any resource
-- that was already merged into the wrong account's row keeps that
-- row's current aws_account_id until the next discovery cycle runs
-- with the fixed key in place and inserts each account's own row
-- fresh. Expect resource counts to jump upward across MANY accounts
-- (not just U4RAD) on the next discovery cycle after this migration
-- is applied -- that is the fix taking effect, not a new bug.

SET @correct_key_exists := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'resources'
    AND index_name = 'uniq_resource_identity'
);
SET @sql := IF(@correct_key_exists = 0,
  'ALTER TABLE resources ADD UNIQUE KEY uniq_resource_identity (aws_account_id, resource_type, resource_id)',
  'SELECT "resources.uniq_resource_identity already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @old_key_exists := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'resources'
    AND index_name = 'uniq_resource'
);
SET @sql := IF(@old_key_exists > 0,
  'ALTER TABLE resources DROP INDEX uniq_resource',
  'SELECT "resources.uniq_resource already absent, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
