-- 069_resource_health_account_pk.sql (audit b08)
-- resource_health's PRIMARY KEY was resource_id alone, but resource ids
-- are not unique across accounts (GCP instance names, RDS identifiers,
-- Lambda/IAM names). Two accounts with the same resource_id overwrote
-- each other's row every cycle (health_score.py upserts ON DUPLICATE
-- KEY UPDATE aws_account_id = ...), so a score flip-flopped between
-- accounts and one account could see a score computed from another's
-- alerts. Key it by (aws_account_id, resource_id) like resources' own
-- unique key (migration 045). Existing rows are already unique on
-- resource_id, so they stay unique on the pair. Idempotent.
SET @pk_cols := (
  SELECT COUNT(*) FROM information_schema.key_column_usage
  WHERE table_schema = DATABASE() AND table_name = 'resource_health' AND constraint_name = 'PRIMARY'
);
SET @sql := IF(@pk_cols = 1,
  'ALTER TABLE resource_health DROP PRIMARY KEY, ADD PRIMARY KEY (aws_account_id, resource_id)',
  'SELECT "resource_health primary key already (aws_account_id, resource_id), skipping"'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
