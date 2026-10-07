-- 082_synthetic_https_tls.sql
--
-- First-class HTTPS + TLS monitoring for synthetic checks.
--
-- check_type gains 'https': the same HTTP probe as 'http', plus TLS inspection
-- of the connection that probe already makes (no second connection): TLS
-- version, cipher, handshake time, certificate expiry / subject / issuer and
-- whether the chain + hostname verified. See app/collector/synthetic.py
-- (_probe_https). Existing 'http' checks are untouched.
--
-- synthetic_check_results: one nullable column per TLS fact. They are filled
-- only by 'https' probes; http/tcp/dns rows keep NULLs and are still written
-- with the pre-082 INSERT.
--   cert_days_left  whole days until notAfter (negative = already expired)
--   cert_not_after  certificate notAfter, UTC
--   cert_valid      1 = chain and hostname verified, 0 = verification failed,
--                   NULL = no TLS session was reached (DNS/TCP failure, etc.)
--
-- synthetic_checks.expect_https_redirect: optional per-check flag; when 1 the
-- probe also requests the plain-HTTP URL once and requires a redirect to
-- https://. Default 0 (inert).
--
-- Idempotent: every change is guarded by information_schema, safe to re-run.
-- Apply BEFORE restarting the backend (the collector and the list API select
-- the new columns).

-- 1. check_type ENUM: add 'https' (the table is tiny, the rebuild is instant).
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'synthetic_checks'
             AND column_name = 'check_type' AND LOCATE('https', column_type) > 0);
SET @s := IF(@c = 0,
  "ALTER TABLE synthetic_checks MODIFY COLUMN check_type ENUM('http','https','tcp','dns') NOT NULL DEFAULT 'http'",
  'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 2. per-check redirect flag

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'synthetic_checks' AND column_name = 'expect_https_redirect');
SET @s := IF(@c = 0, 'ALTER TABLE synthetic_checks ADD COLUMN expect_https_redirect TINYINT(1) NOT NULL DEFAULT 0 AFTER expected_keyword', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 3. TLS facts on each result row

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'synthetic_check_results' AND column_name = 'cert_days_left');
SET @s := IF(@c = 0, 'ALTER TABLE synthetic_check_results ADD COLUMN cert_days_left INT NULL AFTER error_message', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'synthetic_check_results' AND column_name = 'cert_not_after');
SET @s := IF(@c = 0, 'ALTER TABLE synthetic_check_results ADD COLUMN cert_not_after DATETIME NULL AFTER cert_days_left', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'synthetic_check_results' AND column_name = 'cert_subject');
SET @s := IF(@c = 0, 'ALTER TABLE synthetic_check_results ADD COLUMN cert_subject VARCHAR(255) NULL AFTER cert_not_after', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'synthetic_check_results' AND column_name = 'cert_issuer');
SET @s := IF(@c = 0, 'ALTER TABLE synthetic_check_results ADD COLUMN cert_issuer VARCHAR(255) NULL AFTER cert_subject', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'synthetic_check_results' AND column_name = 'tls_version');
SET @s := IF(@c = 0, 'ALTER TABLE synthetic_check_results ADD COLUMN tls_version VARCHAR(16) NULL AFTER cert_issuer', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'synthetic_check_results' AND column_name = 'tls_cipher');
SET @s := IF(@c = 0, 'ALTER TABLE synthetic_check_results ADD COLUMN tls_cipher VARCHAR(64) NULL AFTER tls_version', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'synthetic_check_results' AND column_name = 'handshake_ms');
SET @s := IF(@c = 0, 'ALTER TABLE synthetic_check_results ADD COLUMN handshake_ms INT NULL AFTER tls_cipher', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'synthetic_check_results' AND column_name = 'cert_valid');
SET @s := IF(@c = 0, 'ALTER TABLE synthetic_check_results ADD COLUMN cert_valid TINYINT(1) NULL AFTER handshake_ms', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
