-- db/migrations/065_metric_catalog_unique_key_provider.sql
--
-- Audit b17 finding: metric_catalog's only unique key is
-- uniq_catalog_entry (namespace, metric_name) -- added by
-- 003_metric_catalog_full.sql, before `provider` existed on this table
-- at all (provider was added later, by 009_multi_cloud_provider_
-- columns.sql). It was never widened afterwards.
--
-- Every INSERT ... ON DUPLICATE KEY UPDATE against this table
-- (scripts/seed_metric_catalog.py, scripts/seed_multicloud_metric_
-- catalog.py, and app/api/metric_catalog.py's discover_namespace_
-- metrics()) relies on that key to decide "is this the same catalog
-- entry, or a new one?" -- but since it doesn't include `provider`,
-- the database physically cannot store two separate rows for the same
-- (namespace, metric_name) even across two DIFFERENT providers. A
-- curated AWS namespace string is very unlikely to collide with an
-- Azure/GCP one (different naming vocabularies), but discover_
-- namespace_metrics() lets a user type an arbitrary "directory"
-- namespace by hand -- e.g. two different accounts on two different
-- providers both typing a generic custom namespace like "MyApp" --
-- which would silently merge into ONE shared row, with whichever
-- provider's write ran most recently winning that row's `provider`
-- column and hiding the metric from the other provider's accounts
-- entirely (get_account_metrics/generate_yace_config both filter
-- `WHERE mc.provider = %s`). seed_multicloud_metric_catalog.py's own
-- docstring already claims tagging every row with `provider` "so it
-- never collides with or overwrites AWS's rows" -- that was only true
-- for the ON DUPLICATE UPDATE's column list, not for whether a
-- collision could happen in the first place; this migration makes it
-- actually true.
--
-- No existing-data cleanup needed: the CURRENT (namespace, metric_name)
-- key already made it impossible for a same-namespace+metric_name
-- collision to exist as two separate rows today, across any provider --
-- so there is nothing to deduplicate before widening it.
--
-- Idempotent: only touches the index if it doesn't already include
-- `provider`, using the same information_schema-guarded PREPARE/EXECUTE
-- pattern as 012_alert_evaluation_hardening.sql.
SET @needs_widening := (
  SELECT COUNT(*) = 0 FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'metric_catalog'
    AND index_name = 'uniq_catalog_entry' AND column_name = 'provider'
);

SET @sql := IF(@needs_widening,
  'ALTER TABLE metric_catalog DROP INDEX uniq_catalog_entry, ADD UNIQUE KEY uniq_catalog_entry (provider, namespace, metric_name)',
  'SELECT "metric_catalog.uniq_catalog_entry already includes provider, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
