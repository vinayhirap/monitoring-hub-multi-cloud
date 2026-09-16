-- db/migrations/046_add_account_scoping_to_baseline_and_pending.sql
--
-- Follow-up to 045_fix_resources_cross_account_unique_key.sql. That
-- migration fixed `resources` itself; this audits and fixes the same
-- bug shape (a unique key on a raw AWS resource_id string with NO
-- account-scoping column) in every OTHER table found to have it.
--
-- audit_cross_account_keys.py scanned every unique key in the live
-- schema for "identifier column present, no account-scoping column in
-- the same key" and found 4 hits. 2 were false positives (metrics,
-- metric_history -- their `resource_id` column is a BIGINT foreign key
-- to resources.id, already correctly account-scoped through that FK,
-- not the raw string). The other 2 are real and unfixed:
--
--   alert_pending.uq_pending_resource_metric (resource_id, metric_name)
--   metric_baseline.uniq_baseline_bucket (resource_id, metric_name, hour_of_day, day_of_week)
--
-- Both tables' resource_id is VARCHAR (the raw AWS resource name
-- string, matching resources.resource_id/alerts.resource_id -- see
-- migration 020's own comment on this), and NEITHER table has an
-- aws_account_id column at all. Two accounts sharing a resource name
-- (the exact "System"/"cid-DataExportCreator" case that surfaced the
-- original resources bug) would silently share:
--   - metric_baseline: the SAME computed mean/stddev anomaly-detection
--     bucket, corrupting dynamic alert thresholds for one or both
--     accounts with the wrong resource's historical behavior.
--   - alert_pending: the SAME breach-candidate row, so one account's
--     sustained breach could silently reset/merge with another
--     account's breach_cycles count, delaying or corrupting alert
--     promotion for whichever account didn't win the last upsert.
--
-- This migration adds aws_account_id to both tables, backfills it via
-- best-effort join to `resources` (ambiguous rows -- i.e. a resource_id
-- already shared by >1 account at backfill time -- can only be assigned
-- to ONE account; this does not retroactively separate data that was
-- already merged, same caveat as 045), then widens both unique keys to
-- include it, matching the same before/after shape 045 used for
-- `resources`.
--
-- Companion code fix (same commit): app/collector/alert_evaluator.py's
-- _touch_pending/_clear_pending/_dynamic_bounds and
-- app/collector/baseline.py's recompute_baselines() now read/write
-- aws_account_id explicitly instead of relying on resource_id alone --
-- baseline.py's aggregation query also had a related bug: it grouped by
-- the raw resource_id STRING instead of resources.id, so two accounts'
-- distinct resources sharing a name would have had their metric_history
-- averaged into one bucket even before reaching this table's unique key.

-- ── alert_pending ────────────────────────────────────────────────
SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'alert_pending' AND column_name = 'aws_account_id'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE alert_pending ADD COLUMN aws_account_id BIGINT NULL AFTER id',
  'SELECT "alert_pending.aws_account_id already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Best-effort backfill: match each pending row's resource_id string to
-- a resources row. If more than one account currently shares that
-- resource_id (the collision this migration is closing), MySQL's
-- UPDATE...JOIN picks one arbitrarily -- unavoidable given the row
-- was already merged; going forward, each account gets its own row.
UPDATE alert_pending p
JOIN resources r ON r.resource_id = p.resource_id
SET p.aws_account_id = r.aws_account_id
WHERE p.aws_account_id IS NULL;

-- Any row that still has no match (resource no longer exists in
-- `resources` at all) can't be safely attributed -- drop it rather than
-- leave a NULL in what is about to become a NOT NULL key column. A
-- lost pending-breach candidate just means the next real breach starts
-- a fresh candidate; nothing user-visible was ever built on top of it
-- (alert_pending rows are never shown to users, per migration 012).
DELETE FROM alert_pending WHERE aws_account_id IS NULL;

ALTER TABLE alert_pending
  MODIFY COLUMN aws_account_id BIGINT NOT NULL;

SET @old_key_exists := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'alert_pending'
    AND index_name = 'uq_pending_resource_metric'
);
SET @sql := IF(@old_key_exists > 0,
  'ALTER TABLE alert_pending DROP INDEX uq_pending_resource_metric',
  'SELECT "alert_pending.uq_pending_resource_metric already absent, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @new_key_exists := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'alert_pending'
    AND index_name = 'uq_pending_account_resource_metric'
);
SET @sql := IF(@new_key_exists = 0,
  'ALTER TABLE alert_pending ADD UNIQUE KEY uq_pending_account_resource_metric (aws_account_id, resource_id, metric_name)',
  'SELECT "alert_pending.uq_pending_account_resource_metric already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ── metric_baseline ──────────────────────────────────────────────
SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'metric_baseline' AND column_name = 'aws_account_id'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE metric_baseline ADD COLUMN aws_account_id BIGINT NULL AFTER id',
  'SELECT "metric_baseline.aws_account_id already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

UPDATE metric_baseline b
JOIN resources r ON r.resource_id = b.resource_id
SET b.aws_account_id = r.aws_account_id
WHERE b.aws_account_id IS NULL;

-- A stale/orphaned bucket with no matching resource left is not safe
-- to attribute either -- baseline.py fully recomputes this table from
-- scratch every run anyway (see its own docstring: "no incremental/
-- streaming state to get out of sync"), so dropping it just means one
-- cold-start bucket next run, not a permanent loss.
DELETE FROM metric_baseline WHERE aws_account_id IS NULL;

ALTER TABLE metric_baseline
  MODIFY COLUMN aws_account_id BIGINT NOT NULL;

SET @old_key_exists := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'metric_baseline'
    AND index_name = 'uniq_baseline_bucket'
);
SET @sql := IF(@old_key_exists > 0,
  'ALTER TABLE metric_baseline DROP INDEX uniq_baseline_bucket',
  'SELECT "metric_baseline.uniq_baseline_bucket already absent, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @new_key_exists := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'metric_baseline'
    AND index_name = 'uniq_account_baseline_bucket'
);
SET @sql := IF(@new_key_exists = 0,
  'ALTER TABLE metric_baseline ADD UNIQUE KEY uniq_account_baseline_bucket (aws_account_id, resource_id, metric_name, hour_of_day, day_of_week)',
  'SELECT "metric_baseline.uniq_account_baseline_bucket already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
