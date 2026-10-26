-- db/migrations/133_cost_jump_recipients.sql
--
-- Cost-jump alert: who is emailed when the alert opens, in addition to the notification channels already configured.
--
--   notify_user_ids   JSON list of users.id, e.g. [3,5]. The user's CURRENT email is read when the alert opens, so a changed
--                     address follows them; inactive users and users with no email are skipped.
--   notify_emails     comma-separated addresses typed in directly (people with no CloudOps user, or a shared mailbox).
--
-- Set per scope exactly like the other settings (cost_jump_settings.scope_key 0 = all accounts, N = account N) but ADDITIVE:
-- an account is notified at its own recipients plus the all-accounts recipients. NULL = none at that scope.
--
-- Idempotent: safe to run twice. MySQL 8 and MariaDB.

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'cost_jump_settings' AND column_name = 'notify_user_ids');
SET @sql := IF(@c = 0,
  'ALTER TABLE cost_jump_settings ADD COLUMN notify_user_ids TEXT NULL',
  'SELECT "cost_jump_settings.notify_user_ids already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'cost_jump_settings' AND column_name = 'notify_emails');
SET @sql := IF(@c = 0,
  'ALTER TABLE cost_jump_settings ADD COLUMN notify_emails TEXT NULL',
  'SELECT "cost_jump_settings.notify_emails already exists, skipping"');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
