-- db/migrations/074_alert_cycle_clock.sql
--
-- Adds a dedicated "last counted cycle" clock to alerts and alert_pending.
--
-- WHY (found live 2026-09-30): the evaluator only advances an alert's healthy_streak
-- (and an alert_pending row's breach_cycles) when >= MIN_CYCLE_SECONDS (240 s) have
-- passed "since the row was last touched", so that extra evaluations on the 2-minute
-- critical tick are not counted as extra 5-minute cycles. But "last touched" was
-- last_seen_at, which EVERY evaluation overwrites -- including the ones that did not
-- advance the counter. For metrics evaluated every 2 minutes (RDS/ELB P1 metrics) the
-- gap between touches is never >= 240 s, so:
--   * healthy_streak never left 0  -> an open alert could never auto-resolve
--     (prod: ELB "Target 5xx Errors" 0 / 5, CRITICAL, ACTIVE for hours, which also kept
--      both accounts "Critical" and two resources under health score 70);
--   * breach_cycles never left 1   -> any P1 rule needing >= 2 cycles could never fire.
--
-- cycle_at is set only when a cycle is actually counted (and on a breach / on creation),
-- so the gate now measures time since the last COUNTED cycle. last_seen_at keeps its
-- meaning (last evaluation) for staleness and display.
--
-- NULL is fine: the code treats it as triggered_at (alerts) / first_breach_at (pending),
-- so existing stuck alerts resolve on their next healthy evaluation after deploy.
-- Idempotent: each ADD COLUMN is guarded by an information_schema check.

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'alerts' AND column_name = 'cycle_at');
SET @s := IF(@c = 0, 'ALTER TABLE alerts ADD COLUMN cycle_at DATETIME NULL AFTER healthy_streak', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'alert_pending' AND column_name = 'cycle_at');
SET @s := IF(@c = 0, 'ALTER TABLE alert_pending ADD COLUMN cycle_at DATETIME NULL AFTER breach_cycles', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
