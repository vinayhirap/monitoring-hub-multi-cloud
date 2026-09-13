-- db/migrations/002b_metric_catalog_base_table.sql
--
-- Fills a real gap found 2026-09-13 while replaying this repo's migration
-- history from scratch on a brand-new database (db/schema.sql + every
-- db/migrations/*.sql file in order): 003_metric_catalog_full.sql begins
-- with `ALTER TABLE metric_catalog ADD COLUMN ...`, assuming the table
-- already exists -- but its base CREATE TABLE is not present anywhere in
-- db/schema.sql or any earlier migration. On every real, already-running
-- deployment this is harmless (the table obviously already exists,
-- created at some point outside the tracked migration history -- exactly
-- the class of drift migrate.py's own docstring says it exists to catch,
-- see its "014_user_email_column.sql shipped but was never applied to
-- production" example), but it means a genuinely fresh install following
-- this repo's own documented migration path would fail immediately on
-- 003. This migration is a no-op (CREATE TABLE IF NOT EXISTS) on any
-- database where the table already exists, so it's safe to apply
-- unconditionally on already-running dev/prod databases too.
--
-- Column list and types reconstructed from how the codebase actually
-- reads/writes this table (confirmed against every `mc.<column>` read in
-- app/, and the authoritative INSERT statements in
-- scripts/seed_metric_catalog.py and scripts/seed_multicloud_metric_catalog.py
-- -- not guessed): service, namespace, display_service, metric_name,
-- statistic, unit, default_interval, category, description, is_default,
-- enabled, provider. 003_metric_catalog_full.sql's own ADD COLUMN IF NOT
-- EXISTS statements for namespace/display_service/category/description/
-- is_default are also included directly here (rather than left for 003 to
-- add moments later) purely so this file is a complete, correct base
-- table on its own -- 003 still runs immediately afterward unmodified
-- and its ADD COLUMN IF NOT EXISTS calls simply no-op against the columns
-- this file already created.

CREATE TABLE IF NOT EXISTS metric_catalog (
  id               BIGINT AUTO_INCREMENT PRIMARY KEY,
  service          VARCHAR(100)  NOT NULL,
  namespace        VARCHAR(100)  NULL,
  display_service  VARCHAR(150)  NULL,
  metric_name      VARCHAR(150)  NOT NULL,
  statistic        VARCHAR(50)   NULL,
  unit             VARCHAR(50)   NULL,
  default_interval INT           NOT NULL DEFAULT 300,
  category         ENUM('core','extended','directory') NOT NULL DEFAULT 'extended',
  description      VARCHAR(255)  NULL,
  is_default       TINYINT(1)    NOT NULL DEFAULT 0,
  enabled          TINYINT(1)    NOT NULL DEFAULT 1,
  provider         VARCHAR(20)   NOT NULL DEFAULT 'aws',
  created_at       TIMESTAMP     NULL DEFAULT CURRENT_TIMESTAMP,

  KEY idx_metric_catalog_service (service),
  KEY idx_metric_catalog_category (category),
  KEY idx_metric_catalog_provider (provider)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Deliberately NOT adding the (namespace, metric_name) unique key here --
-- 003_metric_catalog_full.sql already does that itself
-- (`ADD UNIQUE KEY IF NOT EXISTS uniq_catalog_entry`), immediately after
-- this file runs. Adding it twice would just be redundant, not harmful,
-- but keeping each migration's own responsibility contained (this file:
-- base table shape; 003: catalog-specific indexing/columns it was
-- already written to own) is clearer than duplicating that line here.
