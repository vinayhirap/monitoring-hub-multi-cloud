-- 077_alert_breach_snapshot.sql
--
-- Audit A3 (alert numbers contradict severity/threshold).
--
-- alerts.current_value is overwritten on EVERY evaluation, including the
-- healthy cycles an open alert needs before it auto-resolves
-- (healthy_streak). The evidence timeline printed that live value beside
-- the threshold on the "triggered" row, so a CPU alert opened at 70%+
-- read "WARNING - CPU 11.68% vs threshold 70%" once the CPU had dropped.
--
-- breach_value / breach_threshold hold the last reading that actually
-- breached and the line it breached (static or dynamic band, as resolved
-- by the evaluator in that same cycle). They are written only when a
-- cycle is breaching, never on recovery, so value-vs-threshold shown for
-- a triggered alert always agrees with its severity.
--
-- NULL on rows created before this migration (the UI omits the pair for
-- those rather than guessing). Idempotent: guarded by information_schema.

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'alerts' AND column_name = 'breach_value');
SET @s := IF(@c = 0, 'ALTER TABLE alerts ADD COLUMN breach_value DOUBLE NULL AFTER threshold', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'alerts' AND column_name = 'breach_threshold');
SET @s := IF(@c = 0, 'ALTER TABLE alerts ADD COLUMN breach_threshold DOUBLE NULL AFTER breach_value', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
