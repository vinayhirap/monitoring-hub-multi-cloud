-- db/migrations/009_multi_cloud_provider_columns_rollback.sql
-- Reverses 009_multi_cloud_provider_columns.sql exactly. Safe to run even
-- if only some of the columns were added (each DROP is independent).

ALTER TABLE aws_accounts DROP COLUMN IF EXISTS credential_ref;
ALTER TABLE aws_accounts DROP COLUMN IF EXISTS service_account_email;
ALTER TABLE aws_accounts DROP COLUMN IF EXISTS project_id;
ALTER TABLE aws_accounts DROP COLUMN IF EXISTS client_id;
ALTER TABLE aws_accounts DROP COLUMN IF EXISTS subscription_id;
ALTER TABLE aws_accounts DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE aws_accounts DROP COLUMN IF EXISTS provider;

ALTER TABLE resources DROP COLUMN IF EXISTS normalized_resource_type;

ALTER TABLE metric_catalog DROP COLUMN IF EXISTS provider;
