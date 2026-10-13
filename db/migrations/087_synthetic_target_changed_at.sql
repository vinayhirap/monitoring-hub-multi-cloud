-- 087_synthetic_target_changed_at.sql
--
-- synthetic_checks.target_changed_at: UTC time of the last edit that changed a check's target (NULL = never changed).
--
-- Why: the list/drawer took "latest TLS facts", "last error" and 24h uptime from the check's whole result history,
-- so after pointing a check at a different host the old host's certificate, TLS version and uptime kept showing
-- next to the new target. The API now ignores result rows older than target_changed_at for those figures and
-- for the detail drawer. Nothing is deleted: the old rows stay in synthetic_check_results.
--
-- Idempotent (guarded by information_schema), safe to re-run. Apply BEFORE restarting the backend: the list and
-- results queries reference the column.

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'synthetic_checks' AND column_name = 'target_changed_at');
SET @s := IF(@c = 0, 'ALTER TABLE synthetic_checks ADD COLUMN target_changed_at DATETIME NULL', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
