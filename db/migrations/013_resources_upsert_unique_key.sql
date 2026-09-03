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
-- Safe to run repeatedly (IF NOT EXISTS guard). If duplicate rows already
-- exist from past discovery cycles, run the dedup step below FIRST or the
-- ADD UNIQUE KEY statement will fail with "Duplicate entry".
--
-- Run: mysql -uroot -proot123 monitoring_hub < db/migrations/013_resources_upsert_unique_key.sql

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
ALTER TABLE resources
    ADD UNIQUE KEY IF NOT EXISTS uniq_resource_identity
        (aws_account_id, resource_type, resource_id);
