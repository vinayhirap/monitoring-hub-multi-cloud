-- db/migrations/120_host_agent_server_foundation.sql
--
-- Host agent, phase B: the three tables behind the push-based host agent, plus its two permissions.
--
--   host_agent_enrollment_keys  one-time-ish keys an admin issues so an agent can enroll (only SHA-256 stored)
--   host_agents                 one row per (account, hostname); config_version / config_json drive agent config
--   host_agent_tokens           per-agent bearer tokens (only SHA-256 stored); revoked_at set on rotate / revoke
--
--   hostagents.view    viewer, editor, admin   GET /api/host-agents (scoped by the caller's accessible accounts)
--   hostagents.manage  admin only              enrollment keys, revoke host, PATCH config
--
-- Inert until HOST_AGENT_ENABLED=true: no existing code reads these tables. No FK to users or aws_accounts on
-- purpose (the audit trail must survive deletes; see app/auth/principals.py). All times are UTC.
--
-- Idempotent (CREATE TABLE IF NOT EXISTS, INSERT IGNORE against the unique keys on permissions.code,
-- role_permissions(role, permission_id) and the v2 equivalent). Nothing here is MySQL-8-only, so it runs on
-- MySQL 8 and MariaDB. Seeds BOTH role_permissions (route gate) and role_permissions_v2 (scoped bindings),
-- the same pattern as 110_help_and_usage_permissions.sql.

CREATE TABLE IF NOT EXISTS host_agent_enrollment_keys (
  id             BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id BIGINT NOT NULL,
  key_hash       CHAR(64) NOT NULL,
  label          VARCHAR(100) NULL,
  max_uses       INT NOT NULL DEFAULT 1,
  used_count     INT NOT NULL DEFAULT 0,
  expires_at     DATETIME NULL,
  revoked        TINYINT(1) NOT NULL DEFAULT 0,
  created_by     VARCHAR(100) NULL,
  created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_host_agent_enrollment_key_hash (key_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS host_agents (
  id             BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id BIGINT NOT NULL,
  hostname       VARCHAR(255) NOT NULL,
  instance_id    VARCHAR(64) NULL,
  resource_id    VARCHAR(512) NULL,
  os             VARCHAR(40) NULL,
  arch           VARCHAR(20) NULL,
  agent_version  VARCHAR(40) NULL,
  status         ENUM('active','revoked') NOT NULL DEFAULT 'active',
  last_seen_at   DATETIME NULL,
  last_seq       BIGINT NOT NULL DEFAULT 0,
  config_version INT NOT NULL DEFAULT 1,
  config_json    JSON NULL,
  enrolled_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_host_agent (aws_account_id, hostname)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS host_agent_tokens (
  id             BIGINT AUTO_INCREMENT PRIMARY KEY,
  host_agent_id  BIGINT NOT NULL,
  token_hash     CHAR(64) NOT NULL,
  created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  revoked_at     DATETIME NULL,
  UNIQUE KEY uq_host_agent_token_hash (token_hash),
  CONSTRAINT fk_host_agent_tokens_agent FOREIGN KEY (host_agent_id) REFERENCES host_agents (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('hostagents.view',   'Operations', 'View Host Agents',
   'See the servers that run the CloudOps host agent, their status and last check-in, for the accounts the user can access', 0),
  ('hostagents.manage', 'Operations', 'Manage Host Agents',
   'Create and revoke host agent enrollment keys, revoke enrolled hosts and change their agent configuration', 0);

-- v2 resolver
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'viewer' AND p.code IN ('hostagents.view');

INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'editor' AND p.code IN ('hostagents.view');

INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'admin' AND p.code IN ('hostagents.view', 'hostagents.manage');

-- legacy role_permissions
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'viewer', id FROM permissions WHERE code IN ('hostagents.view');
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code IN ('hostagents.view');
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code IN ('hostagents.view', 'hostagents.manage');
