-- db/migrations/007_purge_stale_account_data.sql
-- One-time cleanup: removes alerts, metrics, and resources left behind by
-- AWS accounts that were removed/deactivated (status != 'active') or that
-- no longer exist. This is what was causing stale alerts (e.g. "AuroGov Hyd")
-- to keep showing up on the Alerts page even though only Mumbai is onboarded.
--
-- Safe to run multiple times.
-- Run: mysql -umonitor -p monitoring_hub < db/migrations/007_purge_stale_account_data.sql
-- (audit d01: redacted a real-looking plaintext password that was
--  committed here -- pass it interactively / via your own .env instead.)

-- 1. Delete alerts tied to resources whose account is missing/inactive
DELETE a FROM alerts a
LEFT JOIN resources   r   ON r.resource_id = a.resource_id
LEFT JOIN aws_accounts acc ON acc.id = r.aws_account_id
WHERE acc.id IS NULL OR acc.status != 'active';

-- 2. Delete metric history tied to those same resources
DELETE m FROM metrics m
JOIN resources r ON r.id = m.resource_id
LEFT JOIN aws_accounts acc ON acc.id = r.aws_account_id
WHERE acc.id IS NULL OR acc.status != 'active';

-- 3. Delete the orphaned/inactive resources themselves
DELETE r FROM resources r
LEFT JOIN aws_accounts acc ON acc.id = r.aws_account_id
WHERE acc.id IS NULL OR acc.status != 'active';

-- Sanity check afterwards — should show 0 rows:
-- SELECT COUNT(*) FROM resources r
-- LEFT JOIN aws_accounts acc ON acc.id = r.aws_account_id
-- WHERE acc.id IS NULL OR acc.status != 'active';
