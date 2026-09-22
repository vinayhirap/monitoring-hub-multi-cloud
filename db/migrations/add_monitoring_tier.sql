-- db/migrations/add_monitoring_tier.sql
-- Phase 2: Add monitoring_tier to resources table
-- SECURITY (audit b05): a real-looking plaintext DB credential was
-- committed here in the original "Run:" example command and is
-- visible to anyone with repo/git-history access. Removed -- rotate
-- DB_PASSWORD immediately if that value was ever the actual password
-- (see this audit's DEPLOY COMMANDS), then apply via migrate.py using
-- the standard mysql one-liner (reads DB_USER/DB_PASSWORD from .env),
-- never a credential typed into a comment or shell history again.

ALTER TABLE resources
    ADD COLUMN monitoring_tier ENUM('critical', 'standard', 'low')
    NOT NULL DEFAULT 'standard'
    AFTER instance_state;

UPDATE resources SET monitoring_tier = 'critical' WHERE resource_type = 'rds';
UPDATE resources SET monitoring_tier = 'critical' WHERE resource_type = 'elb';
UPDATE resources SET monitoring_tier = 'low'      WHERE resource_type = 'ebs';
UPDATE resources SET monitoring_tier = 'low'      WHERE resource_type = 'lambda';
UPDATE resources SET monitoring_tier = 'low'
    WHERE resource_type = 'ec2' AND instance_state != 'running';

ALTER TABLE resources
    ADD INDEX idx_resources_tier (aws_account_id, monitoring_tier, instance_state);