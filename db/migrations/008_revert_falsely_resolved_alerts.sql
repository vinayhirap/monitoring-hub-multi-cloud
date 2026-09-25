-- db/migrations/008_revert_falsely_resolved_alerts.sql
-- Reverts alerts that were incorrectly auto-resolved by a buggy staleness
-- check (resolved purely because no metric arrived in 30 min — NOT because
-- their account was actually removed). Only touches alerts belonging to
-- still-active accounts, resolved very recently, so it won't touch alerts
-- you legitimately resolved yourself in the past.
--
-- Run: mysql --host=127.0.0.1 --port=3307 --user=root --password monitoring_hub < db/migrations/008_revert_falsely_resolved_alerts.sql
-- (audit d01: redacted a real-looking plaintext password that was
--  committed here -- pass it interactively / via your own .env instead.)

UPDATE alerts a
JOIN resources r      ON r.resource_id = a.resource_id
JOIN aws_accounts acc ON acc.id = r.aws_account_id AND acc.status = 'active'
SET a.status = 'active',
    a.resolved_at = NULL
WHERE a.status = 'resolved'
  AND a.resolved_at >= NOW() - INTERVAL 1 HOUR;

-- Check result:
-- SELECT status, COUNT(*) FROM alerts GROUP BY status;
