-- db/migrations/130_two_environment_model_rollback.sql
-- Restores the values saved by 130_two_environment_model.sql. Run it only together with the previous code
-- release (git revert of the two-environment commit). Rows written after the migration keep their new value.
-- The file name ends in _rollback.sql, so `migrate.py apply --all-pending` never runs it.

UPDATE resources r JOIN env_migration_130_backup b
    ON b.tbl='resources' AND b.col='env_override' AND b.row_id = r.id
   SET r.env_override = b.old_value, r.env_source = COALESCE(b.extra, r.env_source);
UPDATE resources r JOIN env_migration_130_backup b
    ON b.tbl='resources' AND b.col='env_resolved' AND b.row_id = r.id
   SET r.env_resolved = b.old_value, r.env_source = COALESCE(b.extra, r.env_source);
UPDATE aws_accounts t JOIN env_migration_130_backup b
    ON b.tbl='aws_accounts' AND b.col='default_env' AND b.row_id = t.id SET t.default_env = b.old_value;
UPDATE aws_accounts t JOIN env_migration_130_backup b
    ON b.tbl='aws_accounts' AND b.col='environment' AND b.row_id = t.id SET t.environment = b.old_value;
UPDATE alerts t JOIN env_migration_130_backup b
    ON b.tbl='alerts' AND b.col='environment' AND b.row_id = t.id SET t.environment = b.old_value;
UPDATE alert_pending t JOIN env_migration_130_backup b
    ON b.tbl='alert_pending' AND b.col='environment' AND b.row_id = t.id SET t.environment = b.old_value;
UPDATE synthetic_checks t JOIN env_migration_130_backup b
    ON b.tbl='synthetic_checks' AND b.col='environment' AND b.row_id = t.id SET t.environment = b.old_value;

ALTER TABLE alerts MODIFY COLUMN environment VARCHAR(50) NULL DEFAULT 'uat';
-- env_migration_130_backup is kept; drop it by hand once you are sure:  DROP TABLE env_migration_130_backup;
