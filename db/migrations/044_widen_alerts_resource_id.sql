-- db/migrations/044_widen_alerts_resource_id.sql
--
-- alerts.resource_id was VARCHAR(50) -- a leftover from before this
-- table stored real AWS resource identifiers (ARNs, CloudWatch Logs
-- group names) instead of a short internal id. Every later migration
-- that also stores a resource_id-shaped value (op_events, alert_pending,
-- resource_relationships, slo_definitions, maintenance_windows) already
-- uses VARCHAR(512) specifically to match resources.resource_id's own
-- width (widened in migration 016) -- alerts itself was simply missed
-- when that widening happened, since 016 only touched the `resources`
-- table.
--
-- Confirmed live (2026-09-16, U4RAD onboarding): alert_evaluator.py's
-- INSERT INTO alerts (resource_id, ...) was failing with
-- "1406 (22001): Data too long for column 'resource_id' at row 1" on
-- promotion of any pending alert whose resource_id exceeds 50 chars --
-- e.g. a CloudWatch Logs group name (119 chars) or an ELB/ACM ARN
-- (84-104 chars). This silently blocked the ENTIRE evaluate_alerts()
-- cycle (not just the one long resource_id) every run it was hit,
-- since the failing INSERT aborts the whole function -- see
-- app.collector.scheduler's "Standard tier error" log line.
--
-- This is NOT specific to U4RAD or long-ARN resource types -- ANY
-- account, including AuroGov Mumbai, could have silently hit this the
-- first time any of its own resources with a resource_id over 50 chars
-- needed to raise a new alert.

ALTER TABLE alerts
  MODIFY COLUMN resource_id VARCHAR(512) NOT NULL;
