-- db/migrations/011_access_scopes.sql
--
-- Phase 1 of the RBAC project: the actual scope model.
--
-- users.role (admin/editor/viewer) already exists and is unchanged —
-- role answers "what can this user DO". This table answers "WHERE can
-- they do it" — which cloud, which account/subscription/project,
-- which regions, optionally which resource group / resource type /
-- specific resource.
--
-- A user can have MULTIPLE rows here. Effective scope = the union of
-- all their rows. ADMIN users need zero rows — admin implicitly has
-- full access everywhere; this table is only consulted for
-- editor/viewer. No rows for a non-admin user = no access (deny by
-- default), matching the spec's requirement.
--
-- account_ref_id points at the existing unified `aws_accounts` table,
-- which (per the multi-cloud build) already holds AWS, Azure, AND GCP
-- accounts in one place, distinguished by its `provider` column — so
-- one FK covers "account" for AWS, "subscription" for Azure, and
-- "project" for GCP, rather than three separate nullable columns.
-- account_ref_id NULL means "every account under this cloud" (the
-- cloud-wide case the spec asked for).
--
-- `cloud` is stored redundantly alongside account_ref_id (rather than
-- only joining to aws_accounts.provider) specifically so a cloud-wide
-- grant (account_ref_id IS NULL) still has something to filter on.
-- The application layer (not a DB constraint — MySQL can't express
-- "this FK's provider column must equal this other column" cleanly)
-- must enforce that when account_ref_id IS NOT NULL, that account's
-- provider matches this row's `cloud`.
--
-- regions / resource_groups / resource_types / resource_ids are JSON
-- arrays. NULL or an empty array means "not restricted on this
-- dimension" (e.g. regions NULL = all regions within the account).
-- resource_groups is Azure-specific in practice but stored generically
-- so the column set doesn't have to change per provider.
--
-- Verify against your REAL local DB before running (see the
-- .env.production.example note from migration 009 — the checked-in
-- db_schema_only.sql has drifted from the live schema before).
-- This migration is purely additive (one new table, no ALTERs on
-- existing tables), so it does not touch aws_accounts or users.

CREATE TABLE IF NOT EXISTS access_scopes (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    cloud           ENUM('aws','azure','gcp') NOT NULL,
    account_ref_id  BIGINT NULL,
    regions         JSON NULL,
    resource_groups JSON NULL,
    resource_types  JSON NULL,
    resource_ids    JSON NULL,
    granted_by      BIGINT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_access_scopes_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_access_scopes_account
        FOREIGN KEY (account_ref_id) REFERENCES aws_accounts(id) ON DELETE CASCADE,
    CONSTRAINT fk_access_scopes_granted_by
        FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE RESTRICT,

    INDEX idx_access_scopes_user (user_id),
    INDEX idx_access_scopes_account (account_ref_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
