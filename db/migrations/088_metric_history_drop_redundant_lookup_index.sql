-- 088_metric_history_drop_redundant_lookup_index.sql
--
-- metric_history carries two indexes on exactly (resource_id, metric_name, metric_timestamp):
--   idx_metric_history_lookup               (002c, a plain KEY)
--   uniq_metric_history_resource_metric_ts  (043, UNIQUE)
-- The UNIQUE key serves every query the plain one was made for, so the plain one only costs an extra index
-- write (and extra index-record locks) on every INSERT into this hot table. 043 never dropped it.
--
-- Idempotent and guarded: drops idx_metric_history_lookup only if it exists AND the unique key that replaces it
-- exists, so it can never leave the table without the lookup index. Nothing else changes; no data is touched.
-- The DROP is an online operation in InnoDB (no table copy). Safe to re-run.

SET @has_old := (SELECT COUNT(*) FROM information_schema.statistics
                 WHERE table_schema = DATABASE() AND table_name = 'metric_history'
                   AND index_name = 'idx_metric_history_lookup');
SET @has_uniq := (SELECT COUNT(*) FROM information_schema.statistics
                  WHERE table_schema = DATABASE() AND table_name = 'metric_history'
                    AND index_name = 'uniq_metric_history_resource_metric_ts');
SET @s := IF(@has_old > 0 AND @has_uniq > 0,
             'ALTER TABLE metric_history DROP INDEX idx_metric_history_lookup', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
