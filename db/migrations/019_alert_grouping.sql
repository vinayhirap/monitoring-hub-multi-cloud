-- db/migrations/019_alert_grouping.sql
--
-- Alert grouping/dedup, phase 10 of the roadmap discussed 2026-09-13.
--
-- PROBLEM: a real outage that breaches the same metric on N resources in
-- the same account (e.g. "CPU high on 6 EC2 instances after an AZ event")
-- currently produces N separate rows in `alerts`, each acked/resolved/
-- muted independently. The Alerts page has no way to say "this is one
-- incident, 6 resources" -- it's N rows of noise, which is exactly the
-- kind of alert-storm fatigue that makes people stop looking at the
-- Alerts page during the outage that matters most.
--
-- DESIGN CHOICE: this does NOT merge alert rows together. Each
-- resource+metric breach keeps its own row, its own ack/resolve/mute
-- state, and its own audit trail -- merging rows would break that
-- semantics (which specific resource did someone ack?) for a UI
-- convenience that a read-time GROUP BY can deliver instead. `group_key`
-- is a denormalized column computed once at alert-creation time
-- (account + resource_type + metric_name) purely so the grouped-view
-- query (app/api/alerts.py's new /grouped endpoint) can GROUP BY it
-- directly instead of re-deriving it via a 3-way join on every request.
--
-- group_key format: "<aws_account_id>:<resource_type>:<metric_name>"
-- e.g. "7:ec2:CPUUtilization" -- deliberately NOT including region, so
-- "CPU high across the whole account" groups together even if the
-- breaching instances span regions (the common real-world incident
-- shape); this can be revisited if per-region grouping turns out to be
-- what's actually wanted once this is in use.

ALTER TABLE alerts
  ADD COLUMN group_key VARCHAR(150) NULL AFTER environment;

-- Composite index: the grouped-view query filters status='active' and
-- groups by group_key together, so this covers that query directly
-- instead of falling back to a status-only index + filesort.
CREATE INDEX idx_alerts_group_key ON alerts (status, group_key);

-- Backfill existing rows so alerts created before this migration also
-- show up correctly grouped (new rows get group_key set at INSERT time
-- by app/collector/alert_evaluator.py going forward -- this UPDATE only
-- covers history).
UPDATE alerts a
JOIN resources r      ON r.resource_id = a.resource_id
JOIN aws_accounts acc ON acc.id = r.aws_account_id
SET a.group_key = CONCAT(acc.id, ':', r.resource_type, ':', a.metric_name)
WHERE a.group_key IS NULL;
