-- db/migrations/101_alert_priority.sql
--
-- Phase 4b, patch 1: alerts.priority (1 = P1 ... 4 = P4), NULL until computed.
--
-- Nothing reads or writes this column unless PRIORITY_ENABLED is on (app/priority.py), so deploying the
-- migration alone changes no behaviour. The column is filled by the evaluator each cycle and by
-- scripts/backfill_alert_priority.py. It is stored, not generated, because it depends on the resource's
-- classification and tags, not on the alert row alone.
--
-- Idempotent: safe to run twice.

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'alerts' AND column_name = 'priority');
SET @sql := IF(@c = 0,
  'ALTER TABLE alerts ADD COLUMN priority TINYINT UNSIGNED NULL',
  'SELECT "alerts.priority already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.statistics
           WHERE table_schema = DATABASE() AND table_name = 'alerts' AND index_name = 'idx_alerts_priority_status');
SET @sql := IF(@c = 0,
  'CREATE INDEX idx_alerts_priority_status ON alerts (priority, status)',
  'SELECT "idx_alerts_priority_status already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
