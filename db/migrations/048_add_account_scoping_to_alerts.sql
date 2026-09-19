-- db/migrations/047_add_account_scoping_to_alerts.sql
--
-- Second follow-up to 045/046. audit_cross_account_keys.py's scan of
-- unique keys in the live schema missed this one because `alerts`
-- doesn't enforce "one active alert per resource+metric" with a DB
-- unique key at all -- it's enforced purely in application code
-- (app/collector/alert_evaluator.py's `SELECT ... WHERE resource_id = %s
-- AND metric_name = %s AND status = 'active' LIMIT 1`, with no
-- aws_account_id in that WHERE clause). A schema-only scan can't see
-- an invariant that only exists in a SELECT statement, so this table
-- was never flagged even though it has the exact same bug shape as
-- alert_pending/metric_baseline -- and per the comment already sitting
-- in _dynamic_bounds() ("2026-09-16 AuroGov Mumbai/U4RAD cross-account
-- resource_id collision"), two real customer accounts have already hit
-- a resource_id collision in production.
--
-- Concretely, without this fix, two accounts sharing a resource_id
-- (any AWS resource ID is only unique WITHIN one account, not
-- globally) could:
--   - have one account's breach silently update/reopen the OTHER
--     account's existing alert row instead of creating its own, or
--   - have one account's recovery resolve the OTHER account's still-
--     breaching alert, since the "existing open alert" lookup can't
--     tell them apart.
-- Downstream, app/collector/health_score.py and
-- app/collector/correlate.py's unscoped `JOIN alerts a ON
-- a.resource_id = r.resource_id` (no account match) could double- or
-- cross-count alerts across accounts sharing a resource_id, and
-- app/api/alerts.py's _get_alert_account_id() -- the authorization
-- check gating ack/resolve/mute/console-url -- could resolve to the
-- WRONG account's id via fetchone() picking an arbitrary match,
-- mis-scoping a permission check rather than just a display bug.
--
-- Unlike alert_pending/metric_baseline, `alerts` deliberately keeps
-- MANY historical rows per (resource_id, metric_name) over time (a new
-- row per breach-then-resolve cycle), so a unique key isn't the right
-- tool here -- the invariant is "at most one row with status='active'"
-- at a time, which is an application-level rule, not a schema-level
-- one. This migration therefore only adds the column + a lookup index,
-- and does not touch existing uniqueness. Existing rows that can't be
-- attributed to exactly one account (resource no longer exists, or
-- already-merged collision data) are left as NULL rather than deleted
-- -- alerts is audit/history data, unlike the safely-recomputed
-- baseline/pending tables 046 handled -- so nothing is destroyed;
-- those rows simply won't benefit from the new scoping until they age
-- out naturally.
--
-- Companion code fix (same commit): alert_evaluator.py's existing-alert
-- lookup and INSERT now read/write aws_account_id explicitly;
-- health_score.py and correlate.py's alerts joins now match on it;
-- alerts.py's _get_alert_account_id() reads it directly instead of
-- re-deriving account via a resources join.

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'alerts' AND column_name = 'aws_account_id'
);
SET @sql := IF(@col_exists = 0,
  'ALTER TABLE alerts ADD COLUMN aws_account_id BIGINT NULL AFTER id',
  'SELECT "alerts.aws_account_id already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Best-effort backfill, same caveat as 045/046: if a resource_id is
-- currently shared by >1 account, MySQL's UPDATE...JOIN picks one
-- arbitrarily for this historical row -- unavoidable, since the data
-- was already ambiguous before this migration. Going forward, every
-- new alert gets the correct account_id at insert time from the
-- evaluator, which already knows it unambiguously.
UPDATE alerts a
JOIN resources r ON r.resource_id = a.resource_id
SET a.aws_account_id = r.aws_account_id
WHERE a.aws_account_id IS NULL;

SET @idx_exists := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'alerts'
    AND index_name = 'idx_alerts_account_resource_metric_status'
);
SET @sql := IF(@idx_exists = 0,
  'ALTER TABLE alerts ADD INDEX idx_alerts_account_resource_metric_status (aws_account_id, resource_id, metric_name, status)',
  'SELECT "alerts.idx_alerts_account_resource_metric_status already exists, skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
