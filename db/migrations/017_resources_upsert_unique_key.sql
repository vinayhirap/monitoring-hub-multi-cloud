-- db/migrations/013_resources_upsert_unique_key.sql
--
-- app/providers/azure/discovery.py and app/providers/gcp/discovery.py's
-- _upsert_resource() has always written via
--   INSERT INTO resources (...) VALUES (...)
--   ON DUPLICATE KEY UPDATE ...
-- which only actually upserts if a UNIQUE key covers the columns that
-- identify "the same resource" -- (aws_account_id, resource_type,
-- resource_id). No committed migration ever added one. Without it, MySQL
-- silently falls back to a plain INSERT on every call, so every 15-minute
-- discovery cycle has been appending a fresh duplicate row per resource
-- instead of updating the existing one (same class of drift flagged in
-- 002_resources_region_instance_state.sql's docstring -- a column/key an
-- environment may have picked up ad-hoc, never captured as a migration).
--
-- This was containable while Azure/GCP discovery only covered 4 resource
-- types each; extending both to their full curated service list (19
-- Azure, 16 GCP) running every 15 minutes makes unbounded duplicate
-- growth in `resources` a real problem, so this migration is a
-- prerequisite for that expansion rather than an optional cleanup.
--
-- Safe to run repeatedly (information_schema existence check, see fix
-- note below). If duplicate rows already exist from past discovery
-- cycles, the dedup step below runs first, or the ADD UNIQUE KEY
-- statement would fail with "Duplicate entry".
--
-- Run: mysql -uroot -proot123 monitoring_hub < db/migrations/013_resources_upsert_unique_key.sql
--
-- Fix 2026-09-10: originally wrote step 2 as a single declarative
-- `ADD UNIQUE KEY IF NOT EXISTS uniq_resource_identity (...)`, which
-- failed live on DEV with "You have an error in your SQL syntax ...
-- near 'IF NOT EXISTS'" -- ADD KEY/ADD UNIQUE KEY does not support an
-- IF NOT EXISTS clause at all (unlike ADD COLUMN, which supports it on
-- some MySQL versions but not others -- see 018_aws_static_key_auth.sql's
-- own fix note for that exact confusion, hit live on prod for the same
-- reason). Replaced with the same portable information_schema-check +
-- dynamic-SQL pattern 018, 004, and 012 already rely on -- confirmed
-- compatible with migrate.py's own statement splitter (see
-- _split_sql_statements()'s docstring there). The dedup DELETE below is
-- unchanged and already ran as a no-op on DEV (0 duplicate rows found)
-- before the ALTER TABLE syntax error aborted the previous attempt;
-- migrate.py's cmd_apply() rolls back on any statement failure within a
-- file, so DEV's `resources` table was left untouched by that attempt.

-- ── 1. Dedup existing rows first (keep the newest row per identity) ────
-- Safe no-op if no duplicates exist yet.
DELETE r1 FROM resources r1
INNER JOIN resources r2
  ON  r1.aws_account_id = r2.aws_account_id
  AND r1.resource_type   = r2.resource_type
  AND r1.resource_id      = r2.resource_id
  AND r1.id < r2.id;

-- ── 2. Add the unique key the upsert logic has always assumed exists ──
-- resource_id is VARCHAR(512) (widened by migration 011 for Azure ARM
-- IDs) -- a full-column unique key on utf8mb4 would need up to 2048
-- bytes for that column alone, safely under InnoDB's 3072-byte index
-- limit even combined with the other two key columns, so no prefix
-- index is needed here.
SET @key_exists := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'resources'
    AND index_name = 'uniq_resource_identity'
);
SET @sql := IF(@key_exists = 0,
  'ALTER TABLE resources ADD UNIQUE KEY uniq_resource_identity (aws_account_id, resource_type, resource_id)',
  'SELECT "resources.uniq_resource_identity already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
