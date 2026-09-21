-- db/migrations/051_alert_lifecycle_hardening.sql
--
-- 2026-09-20 alerts audit. Additive + data-repair only; safe to run twice.
-- APPLY THIS BEFORE RESTARTING THE SERVICE with the matching code (the new
-- code writes these columns).
--
--  1. Lifecycle audit columns. Until now an alert that left 'active' left no
--     trace of WHY or BY WHOM: ack/resolve wrote nothing, and the collector's
--     auto-resolves (account removed, orphan, stopped instance, ...) were
--     indistinguishable from a human clicking Resolve.
--        resolution_reason  'recovered' | 'manual' | 'threshold_disabled' |
--                           'resource_gone' | 'account_inactive' |
--                           'instance_stopped' | 'no_data_expired' |
--                           'duplicate' | 'anomaly_cleared' | ...
--        acked_by / acked_at / resolved_by
--  2. Lookup index for the canonical state queries (app/alert_rules.py).
--  3. Alerts inserted with aws_account_id = NULL by writers that predate
--     migration 048 (Settings "check thresholds", multivariate_anomaly,
--     synthetic uptime) are INVISIBLE to every account-scoped query. Where the
--     resource_id maps to exactly ONE account we attribute them; ambiguous
--     rows are left NULL (never guessed -- see 048's own caveat).
--  4. Duplicate open alerts for the same (account, resource, metric), created
--     by two writers that disagreed on what "already open" meant, are collapsed
--     to the newest row; the older ones are resolved with reason 'duplicate'.

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'alerts' AND column_name = 'resolution_reason');
SET @s := IF(@c = 0, 'ALTER TABLE alerts ADD COLUMN resolution_reason VARCHAR(60) NULL', 'SELECT "resolution_reason exists"');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'alerts' AND column_name = 'acked_by');
SET @s := IF(@c = 0, 'ALTER TABLE alerts ADD COLUMN acked_by VARCHAR(100) NULL', 'SELECT "acked_by exists"');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'alerts' AND column_name = 'acked_at');
SET @s := IF(@c = 0, 'ALTER TABLE alerts ADD COLUMN acked_at DATETIME NULL', 'SELECT "acked_at exists"');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'alerts' AND column_name = 'resolved_by');
SET @s := IF(@c = 0, 'ALTER TABLE alerts ADD COLUMN resolved_by VARCHAR(100) NULL', 'SELECT "resolved_by exists"');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

SET @i := (SELECT COUNT(*) FROM information_schema.statistics
           WHERE table_schema = DATABASE() AND table_name = 'alerts' AND index_name = 'idx_alerts_state_account');
SET @s := IF(@i = 0, 'ALTER TABLE alerts ADD INDEX idx_alerts_state_account (status, aws_account_id, severity)', 'SELECT "idx exists"');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;

-- 3. attribute NULL-account alerts only when unambiguous
UPDATE alerts a
JOIN (
    SELECT resource_id, MIN(aws_account_id) AS acct
    FROM resources
    GROUP BY resource_id
    HAVING COUNT(DISTINCT aws_account_id) = 1
) x ON x.resource_id = a.resource_id
SET a.aws_account_id = x.acct
WHERE a.aws_account_id IS NULL;

-- 4. collapse duplicate open alerts (keep newest)
UPDATE alerts a
JOIN (
    SELECT aws_account_id, resource_id, metric_name, MAX(id) AS keep_id
    FROM alerts
    WHERE status IN ('active', 'acknowledged') AND aws_account_id IS NOT NULL
    GROUP BY aws_account_id, resource_id, metric_name
    HAVING COUNT(*) > 1
) d ON d.aws_account_id = a.aws_account_id
   AND d.resource_id = a.resource_id
   AND d.metric_name = a.metric_name
SET a.status = 'resolved',
    a.resolved_at = UTC_TIMESTAMP(),
    a.last_seen_at = UTC_TIMESTAMP(),
    a.resolution_reason = 'duplicate'
WHERE a.status IN ('active', 'acknowledged') AND a.id <> d.keep_id;

-- 5. resource_health was keyed by resource_id ALONE. Two accounts sharing a
--    resource_id (e.g. the stock CloudWatch log group "System") overwrote each
--    other's score, and ON DUPLICATE KEY UPDATE even flipped aws_account_id,
--    moving the row to the other account. Key it per account like every other
--    table fixed in 045-048. Rows are derived data (recomputed every cycle),
--    so nothing is lost.
SET @pk := (SELECT COUNT(*) FROM information_schema.key_column_usage
            WHERE table_schema = DATABASE() AND table_name = 'resource_health'
              AND constraint_name = 'PRIMARY');
SET @s := IF(@pk = 1,
  'ALTER TABLE resource_health DROP PRIMARY KEY, ADD PRIMARY KEY (aws_account_id, resource_id)',
  'SELECT "resource_health PK already per-account"');
PREPARE st FROM @s; EXECUTE st; DEALLOCATE PREPARE st;
