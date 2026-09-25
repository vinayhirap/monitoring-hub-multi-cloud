-- db/migrations/016_widen_resource_id_rollback.sql (renumbered from 011 -- see 016_widen_resource_id.sql's header)
-- Only safe to run if no resource_id values currently exceed 100 chars.
ALTER TABLE resources
  MODIFY COLUMN resource_id VARCHAR(100) NOT NULL;
