-- 079_audit_logs_created_at_index.sql
--
-- Audit D3/E8. GET /api/audit-logs runs `ORDER BY created_at DESC LIMIT n` (optionally with
-- actor/action LIKE filters) and audit_logs is append-only and unbounded, with no index on
-- created_at -- every Compliance page load (and its 30 s auto-refresh) sorts the whole table.
-- Cheap today, linear in table size forever. Idempotent (information_schema guard).

SET @i := (SELECT COUNT(*) FROM information_schema.statistics
           WHERE table_schema = DATABASE() AND table_name = 'audit_logs' AND index_name = 'idx_audit_logs_created_at');
SET @s := IF(@i = 0, 'ALTER TABLE audit_logs ADD INDEX idx_audit_logs_created_at (created_at)', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
