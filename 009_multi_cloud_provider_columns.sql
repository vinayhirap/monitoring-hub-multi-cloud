-- db/migrations/009_multi_cloud_provider_columns.sql
--
-- Step 1 of the multi-cloud refactor plan (see multi-cloud-architecture-
-- assessment.md, section 6, item 1).
--
-- Purely additive: new nullable/defaulted columns only. No renames, no
-- drops, no FK changes, no data loss. Every existing row backfills as
-- provider='aws' so current AWS behavior is unaffected. `aws_accounts`
-- keeps its name for now — renaming it to `cloud_accounts` is a separate,
-- higher-risk step deferred until the provider abstraction layer (item 2
-- in the plan) exists and every call site has been inventoried.
--
-- Do not run this file directly — use apply_multi_cloud_migration.py,
-- which checks column existence first (safe to re-run) and takes a
-- mysqldump backup of the affected tables before altering anything.
--
-- Verified 2026-08-22 against the real local DB schema (not the GitHub
-- clone, which turned out to diverge — see architecture assessment doc).
-- Local has no `enabled_metrics` table; per-account metric selection
-- lives in `account_metric_selections` instead and is untouched by this
-- migration. `resources` and `metric_catalog` locally carry extra
-- columns (monitoring_tier, region, display_service, category) beyond
-- what the clone had — none of that conflicts with what follows.

-- ── aws_accounts: provider + Azure/GCP identity fields ─────────────────
ALTER TABLE aws_accounts
  ADD COLUMN provider ENUM('aws','azure','gcp') NOT NULL DEFAULT 'aws' AFTER id;

-- Azure Service Principal identity (no secrets stored here — see credential_ref)
ALTER TABLE aws_accounts
  ADD COLUMN tenant_id VARCHAR(100) DEFAULT NULL AFTER external_id;
ALTER TABLE aws_accounts
  ADD COLUMN subscription_id VARCHAR(100) DEFAULT NULL AFTER tenant_id;
ALTER TABLE aws_accounts
  ADD COLUMN client_id VARCHAR(100) DEFAULT NULL AFTER subscription_id;

-- GCP Service Account identity
ALTER TABLE aws_accounts
  ADD COLUMN project_id VARCHAR(100) DEFAULT NULL AFTER client_id;
ALTER TABLE aws_accounts
  ADD COLUMN service_account_email VARCHAR(255) DEFAULT NULL AFTER project_id;

-- Pointer to wherever the actual secret (Azure client secret / GCP SA key
-- JSON) is stored. NOT the secret itself — this column never holds a raw
-- credential, matching the same principle already applied to AWS
-- (role_arn is an identity, not a credential). Where that secret actually
-- lives (encrypted column, env-backed vault, AWS Secrets Manager, etc.)
-- is an open decision — flagging rather than guessing.
ALTER TABLE aws_accounts
  ADD COLUMN credential_ref VARCHAR(255) DEFAULT NULL AFTER service_account_email;

-- ── resources: cross-cloud filtering support ────────────────────────────
ALTER TABLE resources
  ADD COLUMN normalized_resource_type VARCHAR(50) DEFAULT NULL AFTER resource_type;

-- ── metric_catalog: which provider a catalog row belongs to ────────────
ALTER TABLE metric_catalog
  ADD COLUMN provider ENUM('aws','azure','gcp') NOT NULL DEFAULT 'aws' AFTER service;
