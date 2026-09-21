-- db/migrations/050_auth_session_hardening.sql
--
-- Audit B01 (auth / session / SSO). Idempotent -- safe to re-run.
--
--  1. users.token_version: bumped on password change/reset; JWTs carry the
--     value they were issued with (claim "tv") and app/auth/deps.py rejects
--     a token whose "tv" no longer matches, so every older session dies.
--  2. revoked_sessions: server-side logout. POST /api/auth/logout inserts
--     the token's jti; deps.get_current_user() rejects revoked jtis. Rows
--     are pruned on each logout once their token would have expired anyway.
--  3. Schema-drift reconcile for users: the live table has `password` and
--     `active` (see db/backups/*.sql) but db/schema.sql + earlier migrations
--     never create them (schema.sql has `password_hash`, no `active`). On a
--     DB that already has them (dev/prod) these are no-ops.

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'token_version'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE users ADD COLUMN token_version INT UNSIGNED NOT NULL DEFAULT 0',
  'SELECT "users.token_version already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS revoked_sessions (
  jti        VARCHAR(64) NOT NULL PRIMARY KEY,
  user_id    BIGINT NOT NULL,
  expires_at DATETIME NOT NULL,
  revoked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_revoked_sessions_expires (expires_at),
  KEY idx_revoked_sessions_user (user_id)
);

SET @has_password := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'password'
);
SET @has_password_hash := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'password_hash'
);
SET @sql := IF(@has_password = 0 AND @has_password_hash = 1,
  'ALTER TABLE users CHANGE COLUMN password_hash password VARCHAR(255) DEFAULT NULL',
  'SELECT "users.password already present (or nothing to rename), skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'active'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE users ADD COLUMN active TINYINT(1) DEFAULT 1',
  'SELECT "users.active already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
