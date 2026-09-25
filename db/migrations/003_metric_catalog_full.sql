-- db/migrations/003_metric_catalog_full.sql
-- Adds full CloudWatch metric catalog support + per-account metric selection.
-- Safe to run multiple times.
--
-- Run: mysql -umonitor -p monitoring_hub < db/migrations/003_metric_catalog_full.sql
-- (audit d01: redacted a real-looking plaintext password that was
--  committed here -- pass it interactively / via your own .env instead.)
-- Then seed the catalog:  python scripts/seed_metric_catalog.py

-- ── 1. Extend metric_catalog ─────────────────────────────────────
-- category:
--   'core'      = already collected by this app (EC2/EBS/RDS/ALB/Lambda/S3/ECS)
--   'extended'  = curated, full metric list, not yet wired into collection
--   'directory' = namespace known (from AWS docs), individual metrics not
--                 hand-enumerated — populated on demand via CloudWatch
--                 ListMetrics ("Discover" button) per account/region.
ALTER TABLE metric_catalog
    ADD COLUMN IF NOT EXISTS namespace       VARCHAR(100) AFTER service,
    ADD COLUMN IF NOT EXISTS display_service VARCHAR(150) AFTER namespace,
    ADD COLUMN IF NOT EXISTS category        ENUM('core','extended','directory') NOT NULL DEFAULT 'extended' AFTER unit,
    ADD COLUMN IF NOT EXISTS description     VARCHAR(255) AFTER category,
    ADD COLUMN IF NOT EXISTS is_default      TINYINT(1) NOT NULL DEFAULT 0 AFTER description;

ALTER TABLE metric_catalog
    ADD UNIQUE KEY IF NOT EXISTS uniq_catalog_entry (namespace, metric_name);

ALTER TABLE metric_catalog
    ADD INDEX IF NOT EXISTS idx_catalog_service (service),
    ADD INDEX IF NOT EXISTS idx_catalog_category (category);

-- ── 2. Per-account metric selection ──────────────────────────────
-- Which catalog entries a given AWS account has opted in to monitor.
-- Rows are created from the default template at onboarding time and can
-- be freely added/removed afterwards from Settings.
CREATE TABLE IF NOT EXISTS account_metric_selections (
  id             BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id BIGINT NOT NULL,
  metric_id      BIGINT NOT NULL,
  enabled        TINYINT(1) NOT NULL DEFAULT 1,
  source         ENUM('template','manual','discovered') NOT NULL DEFAULT 'template',
  created_at     TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_account_metric (aws_account_id, metric_id),
  KEY idx_ams_account (aws_account_id),
  KEY idx_ams_metric (metric_id),
  CONSTRAINT fk_ams_account FOREIGN KEY (aws_account_id) REFERENCES aws_accounts(id) ON DELETE CASCADE,
  CONSTRAINT fk_ams_metric  FOREIGN KEY (metric_id)      REFERENCES metric_catalog(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
