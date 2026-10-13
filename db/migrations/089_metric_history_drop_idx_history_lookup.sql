-- 089_metric_history_drop_idx_history_lookup.sql
--
-- Corrects 088. 088 looked for idx_metric_history_lookup (the name 002c uses), but on the running databases the
-- duplicate is named idx_history_lookup (created by apply_direct_gmd_metrics_revival.py), so 088 found nothing
-- and changed nothing.
--
-- idx_history_lookup is a plain index on exactly (resource_id, metric_name, metric_timestamp), the same columns in
-- the same order as the UNIQUE key uniq_metric_history_resource_metric_ts (043). The unique key serves every query
-- it was made for, so it only costs an extra index write and extra index-record locks on each INSERT into this
-- hot table.
--
-- Idempotent and guarded: drops it only if it exists AND the unique key exists. ALGORITHM=INPLACE, LOCK=NONE makes
-- MySQL refuse (rather than block writers) if it could not drop the index online. No data is touched.
-- Safe to re-run; a no-op on databases that never had the index.

SET @has_old := (SELECT COUNT(*) FROM information_schema.statistics
                 WHERE table_schema = DATABASE() AND table_name = 'metric_history'
                   AND index_name = 'idx_history_lookup');
SET @has_uniq := (SELECT COUNT(*) FROM information_schema.statistics
                  WHERE table_schema = DATABASE() AND table_name = 'metric_history'
                    AND index_name = 'uniq_metric_history_resource_metric_ts');
SET @s := IF(@has_old > 0 AND @has_uniq > 0,
             'ALTER TABLE metric_history DROP INDEX idx_history_lookup, ALGORITHM=INPLACE, LOCK=NONE', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
