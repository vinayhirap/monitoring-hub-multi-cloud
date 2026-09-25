-- db/migrations/019b_audit_log_ip_address.sql
-- (renumbered from 019 -- 019 was already taken by 019_alert_grouping.sql;
--  the header text below was never updated to match)
--
-- audit_logs previously had no way to record WHERE an action came from
-- -- no source IP, nothing. For a compliance/audit trail that's a real
-- gap: an incident review can't tell whether a sensitive action (login,
-- user creation, role change, AWS console open) came from an expected
-- location/session or somewhere unexpected. app/audit.py's new shared
-- write_audit() helper populates this from the request's X-Forwarded-For
-- header (falling back to the direct client address) whenever a FastAPI
-- Request is available to the caller; nullable so existing rows and any
-- caller that doesn't have a Request in scope are unaffected.
--
-- Uses the same portable information_schema-check + dynamic-SQL pattern
-- as 004_metrics_last_value_only.sql / 012_alert_evaluation_hardening.sql
-- / 018_aws_static_key_auth.sql, since a plain
-- `ADD COLUMN IF NOT EXISTS` has previously failed live on prod despite
-- being valid MySQL 8.0.29+ syntax in isolation (see 018's comment).

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'audit_logs' AND column_name = 'ip_address'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE audit_logs ADD COLUMN ip_address VARCHAR(45) NULL AFTER payload',
  'SELECT "audit_logs.ip_address already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
