-- db/migrations/040_rbac_v2_bindings.sql
--
-- Phase 4 of the RBAC project: SCOPED ROLE BINDINGS + a real service
-- dimension.
--
-- WHY THIS EXISTS
-- Phases 1-3 built three good pieces that do not currently compose:
--
--   011 access_scopes    -- WHERE a user may act (cloud/account/region/
--                           resource_type/resource_id), per user.
--   013 org_group_rbac   -- L1/L2/L3 groups as reusable bundles of WHERE.
--   015 permissions_rbac -- WHAT a role may do, as named permission codes.
--
-- The gap: role (WHAT) is a single global column on users, while scope
-- (WHERE) is a separate list. The two are never multiplied together.
-- That makes the single most common real-world ask inexpressible:
--
--     "Priya is an Editor on the prod AWS account in ap-south-1 for
--      EC2 and RDS only, and a Viewer everywhere else."
--
-- Today that is impossible: Priya is globally editor or globally
-- viewer, and her scope list applies equally to whichever one she is.
--
-- This migration adds the join: a ROLE BINDING is the triple
-- (principal, role, scope) -- the same primitive Azure RBAC role
-- assignments and GCP IAM bindings are built on. A principal is a user
-- OR a group, so group membership now confers ROLE-at-SCOPE rather
-- than bare scope, which is what makes the L1/L2/L3 tree actually
-- useful for delegation instead of only for account visibility.
--
-- Two further things this fixes, both audited as live gaps:
--
--   1. SERVICE is promoted to a first-class scope dimension
--      (scope_services), keyed on the same service keys the metric
--      catalog already uses ('ec2','rds','alb','lambda',...) and
--      resolvable against resources.resource_type. access_scopes
--      already had a resource_types column, but NO code path ever
--      filtered data by it -- it was consulted only by
--      authorization.scope_within (the delegation gate), never by a
--      single data-serving endpoint.
--
--   2. REGION becomes enforceable. authorization.
--      get_accessible_regions_for_account has existed since phase 1
--      with ZERO callers -- region scope was stored, validated and
--      ignored. app/auth/rbac.py's accessible_filter() returns region
--      and service alongside account so the filter can no longer be
--      silently skipped by a caller who only remembers accounts.
--
-- BACKWARD COMPATIBILITY (deliberate, not incidental)
-- Purely additive. users.role, access_scopes, group_policies,
-- role_permissions and every existing require_permission() call keep
-- working unchanged. The v2 resolver treats a user's legacy
-- users.role + access_scopes rows as an implicit global binding, so a
-- system that runs this migration and deploys no other change behaves
-- exactly as it does today. Bindings are opt-in per user; the legacy
-- path is only retired once a user has at least one explicit binding.
-- Same staging discipline migrations 011/013/015 used.

-- ─────────────────────────────────────────────────────────────────────
-- 1. ROLES become data, not an ENUM
-- ─────────────────────────────────────────────────────────────────────
-- users.role and role_permissions.role are ENUM('admin','editor',
-- 'viewer'), so a customer cannot define "NOC-L1-ReadOnly" or
-- "DBA-RDS-Operator" without an ALTER TABLE on a hot table. A roles
-- table removes that ceiling. The three builtin rows keep the exact
-- same keys as the ENUM values so legacy code comparing
-- role == 'admin' continues to match.

CREATE TABLE IF NOT EXISTS roles (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    role_key     VARCHAR(64)  NOT NULL,
    name         VARCHAR(150) NOT NULL,
    description  VARCHAR(500) NULL,
    -- is_builtin rows cannot be deleted or have their key changed;
    -- their permission set CAN still be edited, same as today.
    is_builtin   TINYINT(1) NOT NULL DEFAULT 0,
    -- rank orders roles for the delegation check ("you may not grant a
    -- role stronger than your own"). Higher = stronger. Custom roles
    -- default to 0 so they can never out-rank a builtin by accident.
    rank         INT NOT NULL DEFAULT 0,
    created_by   BIGINT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_roles_key (role_key),
    CONSTRAINT fk_roles_created_by
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO roles (role_key, name, description, is_builtin, rank) VALUES
  ('viewer', 'Viewer', 'Read-only access to monitoring surfaces within scope', 1, 10),
  ('editor', 'Editor', 'Operational actions and configuration within scope',  1, 20),
  ('admin',  'Administrator', 'Full administrative access within scope',      1, 30);

-- role_permissions_v2 keys on roles.id instead of the ENUM. The
-- original role_permissions table is left in place and still read by
-- app.auth.permissions for legacy global checks; a trigger is NOT used
-- to keep them in sync (triggers here would be invisible magic) --
-- app/api/admin/roles.py writes both while the legacy path is alive.
CREATE TABLE IF NOT EXISTS role_permissions_v2 (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    role_id       BIGINT NOT NULL,
    permission_id BIGINT NOT NULL,

    UNIQUE KEY uq_rpv2 (role_id, permission_id),
    CONSTRAINT fk_rpv2_role
        FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE,
    CONSTRAINT fk_rpv2_permission
        FOREIGN KEY (permission_id) REFERENCES permissions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Seed v2 from the existing v1 mapping so the two are identical on day
-- one. admin additionally gets every permission row explicitly (v1
-- relied on a hardcoded short-circuit in app.auth.permissions; that
-- short-circuit stays, but a scoped admin binding needs real rows to
-- intersect against).
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, rp.permission_id
FROM role_permissions rp JOIN roles r ON r.role_key = rp.role;

INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p WHERE r.role_key = 'admin';


-- ─────────────────────────────────────────────────────────────────────
-- 2. SCOPES become a reusable, addressable row
-- ─────────────────────────────────────────────────────────────────────
-- access_scopes and group_policies are schema-identical by design, but
-- each is welded to its owner (a user / a group). A binding needs to
-- point at a scope independently of who holds it, so the same scope
-- ("prod-aws / ap-south-1 / ec2+rds") can back a binding for a user, a
-- group, and a future service account without being retyped.
--
-- NULL on any dimension means UNRESTRICTED at that dimension, exactly
-- as in 011. The dimensions nest: cloud > account > region > service >
-- resource. A NULL at a level implies every value at that level AND
-- every level below it that is also NULL.

CREATE TABLE IF NOT EXISTS rbac_scopes (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    -- Human label shown in the admin UI ("Prod AWS - Mumbai - Core
    -- compute"). Not unique: two teams may legitimately name a scope
    -- the same thing.
    label           VARCHAR(200) NULL,
    -- NULL cloud = every cloud. This is the one dimension 011 could
    -- not express (its `cloud` was NOT NULL), which is why a true
    -- org-wide grant previously had to be three separate rows.
    cloud           ENUM('aws','azure','gcp') NULL,
    account_ref_id  BIGINT NULL,
    -- JSON arrays; NULL or [] = unrestricted on that dimension.
    regions         JSON NULL,
    -- NEW: service keys ('ec2','rds','alb',...) matching
    -- metric_catalog.service_key and resources.resource_type.
    services        JSON NULL,
    resource_groups JSON NULL,   -- Azure resource groups
    resource_ids    JSON NULL,
    -- NEW: tag-based selection, e.g. {"Environment":["prod"],"Team":["payments"]}.
    -- ANDed across keys, ORed within a key -- the same semantics as an
    -- AWS IAM aws:ResourceTag condition block. Evaluated against
    -- resources.tags. NULL = no tag constraint.
    tag_selector    JSON NULL,
    created_by      BIGINT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_rbac_scopes_account
        FOREIGN KEY (account_ref_id) REFERENCES aws_accounts(id) ON DELETE CASCADE,
    CONSTRAINT fk_rbac_scopes_created_by
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT,

    INDEX idx_rbac_scopes_account (account_ref_id),
    INDEX idx_rbac_scopes_cloud (cloud)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- The global scope: every cloud, every account, every region, every
-- service. id is not hardcoded anywhere -- app/auth/rbac.py looks it
-- up by "all dimensions NULL". Created by user id 1 (the bootstrap
-- admin) if one exists; the WHERE EXISTS guard keeps this migration
-- safe to run against a database with no users yet.
INSERT INTO rbac_scopes (label, cloud, account_ref_id, regions, services, created_by)
SELECT 'Organization (all clouds)', NULL, NULL, NULL, NULL, MIN(id) FROM users
WHERE EXISTS (SELECT 1 FROM users)
  AND NOT EXISTS (
    SELECT 1 FROM rbac_scopes
    WHERE cloud IS NULL AND account_ref_id IS NULL
      AND regions IS NULL AND services IS NULL
      AND resource_groups IS NULL AND resource_ids IS NULL
      AND tag_selector IS NULL
  );


-- ─────────────────────────────────────────────────────────────────────
-- 3. ROLE BINDINGS -- the actual join
-- ─────────────────────────────────────────────────────────────────────
-- (principal, role, scope). principal_type 'group' means every member
-- of that group AND every member of its descendant groups receives
-- this role at this scope, matching the additive L1->L2->L3
-- inheritance already documented in app/auth/authorization.py.
--
-- expires_at supports time-bound elevation (break-glass / on-call
-- rotation) without a separate table: a binding past its expiry is
-- simply not returned by the resolver. NULL = permanent.

CREATE TABLE IF NOT EXISTS role_bindings (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    principal_type  ENUM('user','group') NOT NULL,
    principal_id    BIGINT NOT NULL,
    role_id         BIGINT NOT NULL,
    scope_id        BIGINT NOT NULL,
    granted_by      BIGINT NOT NULL,
    -- Free-text justification, surfaced in the audit log and the
    -- access-review export. Required by app/api/admin/bindings.py for
    -- any binding carrying an admin-rank role.
    reason          VARCHAR(500) NULL,
    expires_at      TIMESTAMP NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- Same principal + same role + same scope twice is always a
    -- mistake, never a meaningful "double grant".
    UNIQUE KEY uq_role_bindings (principal_type, principal_id, role_id, scope_id),

    CONSTRAINT fk_role_bindings_role
        FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE RESTRICT,
    CONSTRAINT fk_role_bindings_scope
        FOREIGN KEY (scope_id) REFERENCES rbac_scopes(id) ON DELETE RESTRICT,
    CONSTRAINT fk_role_bindings_granted_by
        FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE RESTRICT,

    INDEX idx_role_bindings_principal (principal_type, principal_id),
    INDEX idx_role_bindings_role (role_id),
    INDEX idx_role_bindings_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- NOTE on the deliberate absence of an FK on principal_id: it points
-- at users.id when principal_type='user' and org_groups.id when
-- ='group'. MySQL cannot express a conditional FK, so referential
-- integrity for this column is enforced in app/api/admin/bindings.py
-- (existence-checked before insert) and swept by the orphan check in
-- scripts/rbac_consistency_check.py. This is the same tradeoff 011
-- documented for "account_ref_id's provider must equal cloud".


-- ─────────────────────────────────────────────────────────────────────
-- 4. DENY OVERRIDES
-- ─────────────────────────────────────────────────────────────────────
-- Pure-additive RBAC has no way to say "Ravi is an Editor on prod, but
-- must never run operations.execute there". Without this the only
-- answer is to invent a bespoke near-duplicate role per exception,
-- which is how role catalogs rot.
--
-- DENY ALWAYS WINS over any allow, at any scope, from any binding --
-- the same precedence AWS IAM explicit-deny and Azure RBAC deny
-- assignments use. Kept deliberately narrow: a deny names one
-- permission code at one scope, and only admins may write one.

CREATE TABLE IF NOT EXISTS permission_overrides (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    principal_type  ENUM('user','group') NOT NULL,
    principal_id    BIGINT NOT NULL,
    permission_id   BIGINT NOT NULL,
    -- scope_id NULL = this override applies everywhere.
    scope_id        BIGINT NULL,
    effect          ENUM('allow','deny') NOT NULL DEFAULT 'deny',
    reason          VARCHAR(500) NULL,
    granted_by      BIGINT NOT NULL,
    expires_at      TIMESTAMP NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uq_perm_override (principal_type, principal_id, permission_id, scope_id, effect),
    CONSTRAINT fk_po_permission
        FOREIGN KEY (permission_id) REFERENCES permissions(id) ON DELETE CASCADE,
    CONSTRAINT fk_po_scope
        FOREIGN KEY (scope_id) REFERENCES rbac_scopes(id) ON DELETE CASCADE,
    CONSTRAINT fk_po_granted_by
        FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE RESTRICT,

    INDEX idx_po_principal (principal_type, principal_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ─────────────────────────────────────────────────────────────────────
-- 5. SERVICE CATALOG for the scope picker
-- ─────────────────────────────────────────────────────────────────────
-- The admin UI needs a list of selectable services per cloud. Deriving
-- it from DISTINCT resources.resource_type would only ever show
-- services already discovered in an onboarded account -- you could not
-- pre-grant "RDS in prod" before the first RDS instance is discovered,
-- which is exactly when you want to. This table is the authoritative
-- pick-list; scripts/seed_metric_catalog.py keeps it aligned with
-- app/*/metric_catalog_data.py.

CREATE TABLE IF NOT EXISTS rbac_service_catalog (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    cloud        ENUM('aws','azure','gcp') NOT NULL,
    service_key  VARCHAR(64)  NOT NULL,
    display_name VARCHAR(150) NOT NULL,
    category     VARCHAR(50)  NOT NULL DEFAULT 'extended',

    UNIQUE KEY uq_rbac_service (cloud, service_key),
    INDEX idx_rbac_service_cloud (cloud)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO rbac_service_catalog (cloud, service_key, display_name, category) VALUES
  ('aws','ec2','Amazon EC2','core'),
  ('aws','ebs','Amazon EBS','core'),
  ('aws','rds','Amazon RDS','core'),
  ('aws','alb','Application Load Balancer','core'),
  ('aws','nlb','Network Load Balancer','core'),
  ('aws','lambda','AWS Lambda','core'),
  ('aws','s3','Amazon S3','core'),
  ('aws','ecs','Amazon ECS','core'),
  ('aws','eks','Amazon EKS','extended'),
  ('aws','elasticache','Amazon ElastiCache','extended'),
  ('aws','dynamodb','Amazon DynamoDB','extended'),
  ('aws','cloudfront','Amazon CloudFront','extended'),
  ('aws','apigateway','Amazon API Gateway','extended'),
  ('aws','sqs','Amazon SQS','extended'),
  ('aws','sns','Amazon SNS','extended'),
  ('aws','efs','Amazon EFS','extended'),
  ('aws','opensearch','Amazon OpenSearch','extended'),
  ('aws','redshift','Amazon Redshift','extended'),
  ('aws','documentdb','Amazon DocumentDB','extended'),
  ('aws','neptune','Amazon Neptune','extended'),
  ('aws','msk','Amazon MSK','extended'),
  ('aws','kinesis','Amazon Kinesis','extended'),
  ('aws','firehose','Kinesis Data Firehose','extended'),
  ('aws','natgateway','NAT Gateway','extended'),
  ('aws','transitgateway','Transit Gateway','extended'),
  ('aws','directconnect','AWS Direct Connect','extended'),
  ('aws','vpn','AWS Site-to-Site VPN','extended'),
  ('aws','route53','Amazon Route 53','extended'),
  ('aws','autoscaling','EC2 Auto Scaling','extended'),
  ('aws','backup','AWS Backup','extended'),
  ('aws','kms','AWS KMS','extended'),
  ('aws','memorydb','Amazon MemoryDB','extended'),
  ('aws','dax','Amazon DAX','extended'),
  ('aws','dms','AWS DMS','extended'),
  ('aws','cognito','Amazon Cognito','extended'),
  ('aws','certificatemanager','AWS Certificate Manager','extended'),
  ('aws','globalaccelerator','AWS Global Accelerator','extended'),
  ('aws','states','AWS Step Functions','extended'),
  ('aws','events','Amazon EventBridge','extended'),
  ('aws','logs','Amazon CloudWatch Logs','extended'),
  ('azure','vm','Azure Virtual Machines','core'),
  ('azure','disk','Azure Managed Disks','core'),
  ('azure','sql','Azure SQL Database','core'),
  ('azure','appservice','Azure App Service','core'),
  ('azure','loadbalancer','Azure Load Balancer','core'),
  ('azure','appgateway','Azure Application Gateway','extended'),
  ('azure','storage','Azure Storage','core'),
  ('azure','aks','Azure Kubernetes Service','extended'),
  ('azure','cosmosdb','Azure Cosmos DB','extended'),
  ('azure','redis','Azure Cache for Redis','extended'),
  ('azure','servicebus','Azure Service Bus','extended'),
  ('azure','functions','Azure Functions','extended'),
  ('azure','postgresql','Azure Database for PostgreSQL','extended'),
  ('azure','mysql','Azure Database for MySQL','extended'),
  ('gcp','gce','Compute Engine','core'),
  ('gcp','disk','Persistent Disk','core'),
  ('gcp','cloudsql','Cloud SQL','core'),
  ('gcp','gcs','Cloud Storage','core'),
  ('gcp','lb','Cloud Load Balancing','core'),
  ('gcp','gke','Google Kubernetes Engine','extended'),
  ('gcp','functions','Cloud Functions','extended'),
  ('gcp','run','Cloud Run','extended'),
  ('gcp','pubsub','Pub/Sub','extended'),
  ('gcp','bigquery','BigQuery','extended'),
  ('gcp','spanner','Cloud Spanner','extended'),
  ('gcp','memorystore','Memorystore','extended')
;


-- ─────────────────────────────────────────────────────────────────────
-- 6. ACCESS REVIEW SUPPORT
-- ─────────────────────────────────────────────────────────────────────
-- Any RBAC system that cannot answer "who had access to prod on
-- 12 August, and who approved it" is an audit finding waiting to
-- happen. audit_logs already records the mutation; this records the
-- periodic attestation on top of it.

CREATE TABLE IF NOT EXISTS access_reviews (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    binding_id     BIGINT NULL,
    principal_type ENUM('user','group') NOT NULL,
    principal_id   BIGINT NOT NULL,
    reviewed_by    BIGINT NOT NULL,
    decision       ENUM('retain','revoke','modify') NOT NULL,
    notes          VARCHAR(500) NULL,
    reviewed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_access_reviews_reviewer
        FOREIGN KEY (reviewed_by) REFERENCES users(id) ON DELETE RESTRICT,

    INDEX idx_access_reviews_principal (principal_type, principal_id),
    INDEX idx_access_reviews_date (reviewed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
