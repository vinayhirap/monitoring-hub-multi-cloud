-- db/migrations/002_resources_region_instance_state.sql
--
-- Root cause of "Unknown column 'r.region' in 'field list'":
-- db/schema.sql's CREATE TABLE resources never included `region` or
-- `instance_state`, but every write path (app/collector/discovery/runner.py
-- _upsert_resource, the EC2 instance-state UPDATE) and every read path
-- (app/collector/alert_evaluator.py's SELECT r.region) has always assumed
-- both columns exist. They DO exist on environments where someone ran an
-- ad-hoc `ALTER TABLE resources ADD COLUMN region ...` by hand at some
-- point and it never got turned into a committed migration — any
-- environment bootstrapped strictly from schema.sql + the committed
-- migrations (e.g. a fresh EC2 setup) hits this error on the very first
-- discovery run.
--
-- db/migrations/add_monitoring_tier.sql already depends on
-- `instance_state` existing (`ADD COLUMN monitoring_tier ... AFTER
-- instance_state`), so this migration must run before that one.
--
-- Purely additive, MySQL 8 IF NOT EXISTS guards — safe to re-run.

ALTER TABLE resources
    ADD COLUMN IF NOT EXISTS region VARCHAR(50) DEFAULT NULL AFTER tags,
    ADD COLUMN IF NOT EXISTS instance_state VARCHAR(30) DEFAULT NULL AFTER region;

ALTER TABLE resources
    ADD INDEX IF NOT EXISTS idx_resources_region (aws_account_id, region);
