-- 068_security_findings_region.sql (audit b20 follow-up, F17)
-- CSPM now scans security groups / EBS volumes in every enabled AWS
-- region, not just default_region. Each regional finding records its
-- region so (a) the console deep link opens in the right region and
-- (b) a check that fails in ONE region only protects that region's
-- findings from auto-resolve. NULL = global/unknown (S3, IAM, Azure,
-- GCP, and rows written before this migration).
-- Idempotent.
SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'security_findings' AND column_name = 'region'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE security_findings ADD COLUMN region VARCHAR(32) NULL AFTER resource_id',
  'SELECT "security_findings.region already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
