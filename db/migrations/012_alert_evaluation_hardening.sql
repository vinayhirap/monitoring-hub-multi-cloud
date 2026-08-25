-- db/migrations/012_alert_evaluation_hardening.sql
--
-- Purpose: stop alerts firing on a single noisy reading, stop them looking
-- "stale" while still genuinely active, and stop them displaying frozen
-- current_value/triggered_at for months.
--
-- 1) alerts.last_seen_at   -- touched every evaluation cycle the condition
--                             is re-confirmed (breach OR healthy-but-not-
--                             yet-resolved). Lets the API compute a real
--                             "no data for Xh" staleness flag instead of
--                             alerts just looking frozen forever.
-- 2) alerts.healthy_streak -- consecutive healthy readings since the alert
--                             went active. Requires N consecutive good
--                             readings (matching thresholds.evaluation_period,
--                             which existed already but was never read by
--                             the evaluator) before auto-resolving, so a
--                             single good blip doesn't flap the alert closed
--                             and reopen it a minute later.
-- 3) alert_pending          -- holding area for a breach that hasn't been
--                             confirmed for the required evaluation_period
--                             yet. Nothing here is shown to users or counted
--                             as an alert; a row is only promoted into
--                             `alerts` (and only then does it become visible
--                             / paged / beeped) once it has sustained for
--                             long enough. This is what makes threshold
--                             breaches respect thresholds.evaluation_period
--                             instead of firing off one 5-minute sample.
--
-- Deliberately NOT touching the resolve semantics for "no data" -- see
-- 008_revert_falsely_resolved_alerts.sql. This migration adds visibility
-- (last_seen_at, staleness) without silently resolving anything on missing
-- data. Safe / idempotent -- checks before altering.

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'alerts' AND column_name = 'last_seen_at'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE alerts ADD COLUMN last_seen_at DATETIME NULL AFTER resolved_at',
  'SELECT "alerts.last_seen_at already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'alerts' AND column_name = 'healthy_streak'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE alerts ADD COLUMN healthy_streak INT NOT NULL DEFAULT 0 AFTER last_seen_at',
  'SELECT "alerts.healthy_streak already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Backfill last_seen_at for existing rows so staleness math doesn't treat
-- every pre-migration alert as instantly stale.
UPDATE alerts SET last_seen_at = COALESCE(resolved_at, triggered_at) WHERE last_seen_at IS NULL;

CREATE TABLE IF NOT EXISTS alert_pending (
  id               BIGINT NOT NULL AUTO_INCREMENT,
  resource_id      VARCHAR(500) NOT NULL,
  metric_name      VARCHAR(100) NOT NULL,
  severity         VARCHAR(20)  NOT NULL,
  environment      VARCHAR(10)  DEFAULT 'prod',
  first_breach_at  DATETIME NOT NULL,
  last_seen_at     DATETIME NOT NULL,
  breach_cycles    INT NOT NULL DEFAULT 1,
  current_value    DOUBLE,
  threshold_value  DOUBLE,
  PRIMARY KEY (id),
  UNIQUE KEY uq_pending_resource_metric (resource_id, metric_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Check result:
-- SHOW COLUMNS FROM alerts LIKE 'last_seen_at';
-- SHOW COLUMNS FROM alerts LIKE 'healthy_streak';
-- SHOW TABLES LIKE 'alert_pending';
