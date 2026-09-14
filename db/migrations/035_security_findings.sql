-- db/migrations/035_security_findings.sql
--
-- Lite CSPM (Cloud Security Posture Management), 2026-09-14.
--
-- Small, high-signal checklist against each monitored AWS account --
-- NOT a full CSPM product (Wiz/Prisma Cloud check hundreds of rules
-- across every service and compliance framework). This checks the
-- handful of misconfigurations that cause the most real incidents:
-- a public S3 bucket, a security group wide open to the internet on a
-- sensitive port, an unencrypted EBS volume, an IAM console user with
-- no MFA, and a stale (90+ day) IAM access key. See
-- app/collector/cspm.py for the actual check implementations.
--
-- Reuses this app's own AssumeRole/static-key session resolution
-- (app/aws/sts.py's get_boto3_session()) -- no new credential storage,
-- same accounts already onboarded for metrics/topology.
CREATE TABLE IF NOT EXISTS security_findings (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id  BIGINT NOT NULL,
  check_id        VARCHAR(60)  NOT NULL,   -- e.g. 's3_bucket_public', 'sg_open_to_world'
  resource_id     VARCHAR(512) NOT NULL,   -- bucket name / security-group id / IAM username / volume id
  severity        ENUM('HIGH', 'MEDIUM', 'LOW') NOT NULL,
  title           VARCHAR(255) NOT NULL,
  description     TEXT NULL,
  status          ENUM('open', 'resolved') NOT NULL DEFAULT 'open',
  first_seen_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at     TIMESTAMP NULL,
  UNIQUE KEY uniq_finding (aws_account_id, check_id, resource_id),
  INDEX idx_findings_account_status (aws_account_id, status)
);

INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('security.view', 'Resources', 'View Security Findings', 'View cloud security posture findings (public buckets, open security groups, etc.)', 0);

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'viewer', id FROM permissions WHERE code = 'security.view';
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code = 'security.view';
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code = 'security.view';
