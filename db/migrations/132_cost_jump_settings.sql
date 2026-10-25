-- db/migrations/132_cost_jump_settings.sql
--
-- Cost-jump alert settings, editable on the Cloud cost page by anyone holding usage.manage.
--
--   scope_key 0   the default for ALL accounts
--   scope_key N   an override for the account with aws_accounts.id = N
--
-- A NULL column means "not set here: inherit". Resolution for an account is: its own row, else the all-accounts row,
-- else the server environment (COST_JUMP_ALERT_ENABLED / COST_JUMP_THRESHOLD_PCT / COST_JUMP_MIN_INCREASE_USD),
-- else the built-in default (off, 30 %, 0.10 USD). Nothing changes until someone edits a setting or the env var is set.
--
-- No foreign key on purpose: the all-accounts row has no account, and rows for removed accounts are simply never read.
-- Idempotent: safe to run twice. MySQL 8 and MariaDB.

CREATE TABLE IF NOT EXISTS cost_jump_settings (
  scope_key         BIGINT        NOT NULL,
  enabled           TINYINT(1)    NULL,
  threshold_pct     DECIMAL(7,2)  NULL,
  min_increase_usd  DECIMAL(12,4) NULL,
  updated_by        VARCHAR(100)  NULL,
  updated_at        TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (scope_key)
);
