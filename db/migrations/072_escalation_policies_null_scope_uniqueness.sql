-- db/migrations/072_escalation_policies_null_scope_uniqueness.sql
--
-- D02 audit (migrations 020-039 slice): 023_escalation_policies.sql's
-- `UNIQUE KEY uniq_policy_scope (aws_account_id, severity)` does not
-- actually enforce uniqueness for the org-wide fallback policy (the
-- row with aws_account_id IS NULL). MySQL/InnoDB unique indexes treat
-- every NULL as distinct from every other NULL, so two admins (or one
-- admin twice) creating a global CRITICAL policy both succeed -- the
-- app/api/escalation.py create endpoint has no additional guard of its
-- own; it relies entirely on the DB raising a duplicate-key error
-- (caught via `if "uniq_policy_scope" in str(e)` -> HTTP 409), which
-- never fires for this case because the DB doesn't see a collision.
--
-- Impact: app/collector/escalation.py's alert-time lookup
-- (`ORDER BY a.id, (ep.aws_account_id IS NULL) ASC`, keep first row)
-- has no tiebreaker between two global policies of the same severity
-- -- which one an alert gets escalated under becomes dependent on
-- MySQL's row-return order for the tie, i.e. effectively
-- non-deterministic and liable to change after unrelated table
-- activity (an ANALYZE TABLE, an index rebuild, etc.).
--
-- Fix: MySQL has no direct way to make a unique index treat NULL as a
-- normal value, so this adds a generated column that substitutes a
-- sentinel (0) for NULL and keys on THAT instead. 0 is a safe sentinel
-- -- aws_accounts.id is BIGINT AUTO_INCREMENT starting at 1, so no
-- real account will ever collide with it.
--
-- Before adding the new key, dedupes any global policies that already
-- collided: for each severity with more than one aws_account_id IS
-- NULL row, keeps the lowest id (oldest, most likely to already be
-- referenced/relied upon) and deletes the rest. Account-specific rows
-- were never affected (the old key already enforced uniqueness for
-- any non-NULL aws_account_id correctly) and are left untouched.

-- ── Dedupe any pre-existing duplicate global policies ──────────────
DELETE ep1 FROM escalation_policies ep1
JOIN escalation_policies ep2
  ON ep1.severity = ep2.severity
  AND ep1.aws_account_id IS NULL
  AND ep2.aws_account_id IS NULL
  AND ep1.id > ep2.id;

-- ── Generated sentinel column + new unique key ─────────────────────
SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'escalation_policies' AND column_name = 'account_scope_key'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE escalation_policies ADD COLUMN account_scope_key BIGINT GENERATED ALWAYS AS (COALESCE(aws_account_id, 0)) STORED AFTER aws_account_id',
  'SELECT "escalation_policies.account_scope_key already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @old_key_exists := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'escalation_policies'
    AND index_name = 'uniq_policy_scope'
);

-- InnoDB won't let uniq_policy_scope (aws_account_id, severity) be
-- dropped while fk_esc_account's FOREIGN KEY (aws_account_id) still
-- needs SOME index starting with that column -- discovered by actually
-- running this migration end-to-end, not assumed (ERROR 1553 "needed
-- in a foreign key constraint"). Add a plain, non-unique index on
-- aws_account_id alone first so the FK always has one to fall back on.
SET @fk_idx_exists := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'escalation_policies'
    AND index_name = 'idx_esc_account_fk'
);
SET @sql := IF(@fk_idx_exists = 0 AND @old_key_exists > 0,
  'ALTER TABLE escalation_policies ADD INDEX idx_esc_account_fk (aws_account_id)',
  'SELECT "escalation_policies.idx_esc_account_fk already exists or old key already gone, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF(@old_key_exists > 0,
  'ALTER TABLE escalation_policies DROP INDEX uniq_policy_scope',
  'SELECT "escalation_policies.uniq_policy_scope already absent, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @new_key_exists := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'escalation_policies'
    AND index_name = 'uniq_policy_scope_v2'
);
SET @sql := IF(@new_key_exists = 0,
  'ALTER TABLE escalation_policies ADD UNIQUE KEY uniq_policy_scope_v2 (account_scope_key, severity)',
  'SELECT "escalation_policies.uniq_policy_scope_v2 already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- No app-code change needed: app/api/escalation.py's existing
-- `if "uniq_policy_scope" in str(e)` duplicate-key check still matches
-- -- "uniq_policy_scope_v2" contains "uniq_policy_scope" as a
-- substring -- so the 409 response path is unchanged.
