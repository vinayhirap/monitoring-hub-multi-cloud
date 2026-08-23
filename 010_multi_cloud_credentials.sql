-- db/migrations/010_multi_cloud_credentials.sql
--
-- Follow-up to 009_multi_cloud_provider_columns.sql. That migration added
-- a `credential_ref` column as a placeholder pointer to wherever a
-- secret lives, without deciding where that actually is. This migration
-- makes the decision explicit: store the secret directly on the row,
-- matching the existing pattern already used for AWS (role_arn is
-- stored in plaintext on this same table today — no vault, no KMS
-- envelope encryption exists in this codebase yet). Same trust model,
-- extended to two more providers, not a new one invented for this patch.
--
-- Purely additive. No renames, no drops.
--
-- Do not run this file directly — use apply_multi_cloud_credentials.py.

ALTER TABLE aws_accounts
  ADD COLUMN client_secret VARCHAR(500) DEFAULT NULL AFTER client_id;

ALTER TABLE aws_accounts
  ADD COLUMN gcp_service_account_key TEXT DEFAULT NULL AFTER service_account_email;
