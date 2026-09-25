-- Migration: add owner_team and environment columns to aws_accounts
-- Run ONCE on server after deploying:
--   mysql -umonitor -p monitoring_hub < db/migrate_add_owner_team.sql
-- (audit d01: redacted a real-looking plaintext password that was
--  committed here -- pass it interactively / via your own .env instead
--  of typing it on the command line or into a comment.)

ALTER TABLE aws_accounts
  ADD COLUMN IF NOT EXISTS owner_team  VARCHAR(100) NOT NULL DEFAULT '' AFTER description,
  ADD COLUMN IF NOT EXISTS environment VARCHAR(50)  NOT NULL DEFAULT 'PROD' AFTER owner_team;
