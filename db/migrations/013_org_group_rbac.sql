-- db/migrations/013_org_group_rbac.sql
--
-- Phase 2 of the RBAC project: hierarchical ORGANIZATION GROUPS
-- (L1 / L2 / L3), modeled after AWS Organizations OUs / IAM Identity
-- Center permission sets.
--
-- WHY THIS EXISTS
-- Phase 1 (access_scopes, migration 011) gave every user their own
-- flat list of scope grants. That works, but at real org scale
-- ("give everyone on the India NOC team read access to ap-south-1,
-- give the platform team owners access to everything") it means
-- re-granting the same account/region combination to every single
-- user individually, and redoing it again whenever someone new joins.
--
-- This migration adds a GROUP layer that sits ABOVE users:
--   org_groups              -- the L1/L2/L3 hierarchy itself
--   group_policies          -- account/region-specific policy attached
--                               to a group (same shape as access_scopes)
--   user_group_memberships  -- which users belong to which group(s)
--
-- INHERITANCE MODEL (see app/auth/authorization.py for the resolver)
-- L1 is a top-level org unit (e.g. "APAC", "Platform-Eng"). L2 is a
-- child of exactly one L1 (e.g. "APAC > India-NOC"). L3 is a child of
-- exactly one L2 (e.g. "APAC > India-NOC > L3-OnCall"). A user placed
-- in an L3 group inherits that L3 group's OWN policy PLUS its L2
-- parent's policy PLUS its L1 grandparent's policy -- ADDITIVE
-- (union), the same way multiple IAM Identity Center permission sets
-- attached at different OU levels all apply to a principal beneath
-- them. This is a permission-INHERITANCE hierarchy, not an SCP-style
-- restriction hierarchy: a child group only ever has AT LEAST as much
-- access as its parent, never less, by construction.
--
-- Account/region SPECIFICITY is enforced exactly the way Phase 1 does
-- it for users: account_ref_id NULL = every account under `cloud`,
-- regions NULL/[] = every region under that account. group_policies
-- is intentionally schema-identical to access_scopes so the same
-- validation / containment logic (authz.validate_scope_shape) covers
-- both without duplicating it.
--
-- Purely additive: three new tables, zero ALTERs on existing tables.

CREATE TABLE IF NOT EXISTS org_groups (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    name             VARCHAR(150) NOT NULL,
    level            ENUM('L1','L2','L3') NOT NULL,
    parent_group_id  BIGINT NULL,
    description      VARCHAR(500) NULL,
    created_by       BIGINT NOT NULL,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_org_groups_parent
        FOREIGN KEY (parent_group_id) REFERENCES org_groups(id) ON DELETE RESTRICT,
    CONSTRAINT fk_org_groups_created_by
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT,

    UNIQUE KEY uq_org_groups_name (name),
    INDEX idx_org_groups_parent (parent_group_id),
    INDEX idx_org_groups_level (level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ON DELETE RESTRICT on parent_group_id: you cannot drop an L1/L2
-- group while children still point at it. app/api/admin/groups.py
-- also checks this up front for a clean 409 instead of a raw FK
-- error, but this constraint is the real backstop.

CREATE TABLE IF NOT EXISTS group_policies (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    group_id        BIGINT NOT NULL,
    cloud           ENUM('aws','azure','gcp') NOT NULL,
    account_ref_id  BIGINT NULL,
    regions         JSON NULL,
    resource_groups JSON NULL,
    resource_types  JSON NULL,
    resource_ids    JSON NULL,
    granted_by      BIGINT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_group_policies_group
        FOREIGN KEY (group_id) REFERENCES org_groups(id) ON DELETE CASCADE,
    CONSTRAINT fk_group_policies_account
        FOREIGN KEY (account_ref_id) REFERENCES aws_accounts(id) ON DELETE CASCADE,
    CONSTRAINT fk_group_policies_granted_by
        FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE RESTRICT,

    INDEX idx_group_policies_group (group_id),
    INDEX idx_group_policies_account (account_ref_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_group_memberships (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    group_id     BIGINT NOT NULL,
    assigned_by  BIGINT NOT NULL,
    assigned_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_ugm_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_ugm_group
        FOREIGN KEY (group_id) REFERENCES org_groups(id) ON DELETE CASCADE,
    CONSTRAINT fk_ugm_assigned_by
        FOREIGN KEY (assigned_by) REFERENCES users(id) ON DELETE RESTRICT,

    UNIQUE KEY uq_ugm_user_group (user_id, group_id),
    INDEX idx_ugm_user (user_id),
    INDEX idx_ugm_group (group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
