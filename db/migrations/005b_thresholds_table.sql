-- db/migrations/005b_thresholds_table.sql
--
-- Reconciliation migration (audit b12 handoff, finding F4): the
-- `thresholds` table's current live structure -- aws_account_id,
-- resource_type, metric_id, warning_value, critical_value, comparison,
-- evaluation_period, enabled, created_at, plus its two unique keys --
-- is used throughout app/api/settings.py's /thresholds routes and
-- app/aws/collector_direct.py's check_and_write_alerts(), but no
-- tracked migration ever created it. db/schema.sql's own
-- `CREATE TABLE thresholds` is a stale early-prototype shape (bare
-- `warning`/`critical` columns, no account scoping) that was
-- superseded out-of-band at some point and never updated to match.
--
-- Column list below is reconstructed from
-- db/backups/pre_alert_hardening_20260825_104729.sql (a since-untracked
-- DB dump -- see commit 2af2ae6 -- recovered here via
-- `git show 2af2ae6^:db/backups/...`), NOT from a live `SHOW CREATE
-- TABLE` on dev/prod. IF NOT EXISTS makes this a genuine no-op wherever
-- the table already exists out-of-band (dev/prod today), so it cannot
-- change anything there even if this reconstruction is slightly off or
-- the live table has since gained further out-of-band columns -- please
-- diff this against `SHOW CREATE TABLE thresholds` on dev at your
-- convenience and file a follow-up ALTER if anything's missing. This
-- migration only matters for a genuinely fresh install / disaster
-- recovery, where the table wouldn't exist at all otherwise.
--
-- Numbered 005b (between 005_password_reset_tokens.sql and
-- 006_drop_dead_tables.sql -- whose own comment already assumes
-- thresholds exists: "alert_rules -> superseded by thresholds") so it
-- runs before 020_metric_baseline_dynamic_thresholds.sql's
-- `ALTER TABLE thresholds ADD COLUMN use_dynamic, dynamic_k`, which
-- already assumes this table exists and would fail outright on a fresh
-- install otherwise. Same lettered-suffix convention already
-- established by 002b/002c to retrofit a migration between two
-- existing numbers -- migrate.py sorts by filename, and
-- "005b_..." < "006_..." under plain lexicographic sort.
CREATE TABLE IF NOT EXISTS thresholds (
  id                 BIGINT NOT NULL AUTO_INCREMENT,
  aws_account_id     BIGINT NOT NULL,
  resource_type      VARCHAR(50) NOT NULL,
  metric_id          BIGINT NOT NULL,
  warning_value      DOUBLE NOT NULL,
  critical_value     DOUBLE NOT NULL,
  comparison         ENUM('>','<','>=','<=') NOT NULL,
  evaluation_period  INT NOT NULL DEFAULT 5,
  enabled            TINYINT(1) DEFAULT 1,
  created_at         TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_threshold (aws_account_id, resource_type, metric_id),
  UNIQUE KEY uniq_acc_metric (aws_account_id, metric_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
