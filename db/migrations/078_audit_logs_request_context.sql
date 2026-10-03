-- 078_audit_logs_request_context.sql
--
-- Audit E8 / C8: audit_logs recorded the source IP (019b) but nothing that ties a row to the
-- HTTP request that produced it. Adds:
--   user_agent  VARCHAR(255)  the caller's User-Agent, truncated
--   request_id  VARCHAR(64)   the X-Request-ID of the request (also in the response header,
--                             error bodies and log lines), so one id finds the log line,
--                             the audit row and the screenshot of an error
-- Both nullable: existing rows, collector-written rows and callers with no Request in scope
-- are unaffected. Idempotent (information_schema guards, same pattern as 019b).

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'audit_logs' AND column_name = 'user_agent');
SET @s := IF(@c = 0, 'ALTER TABLE audit_logs ADD COLUMN user_agent VARCHAR(255) NULL AFTER ip_address', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'audit_logs' AND column_name = 'request_id');
SET @s := IF(@c = 0, 'ALTER TABLE audit_logs ADD COLUMN request_id VARCHAR(64) NULL AFTER user_agent', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
