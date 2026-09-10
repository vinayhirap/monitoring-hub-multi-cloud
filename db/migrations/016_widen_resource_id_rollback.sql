-- db/migrations/011_widen_resource_id_rollback.sql
-- Only safe to run if no resource_id values currently exceed 100 chars.
ALTER TABLE resources
  MODIFY COLUMN resource_id VARCHAR(100) NOT NULL;
