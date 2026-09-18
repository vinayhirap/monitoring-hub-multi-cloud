-- 047_reports_engine.sql
-- CloudOps client/stakeholder report engine: S3-backed generated
-- reports with a DB metadata index (so "list/search past reports"
-- never has to LIST the S3 bucket), plus an async job table so
-- generation runs in the background without blocking the request
-- that triggered it. See app/reports/ for the code this backs.

CREATE TABLE IF NOT EXISTS report_jobs (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  report_type     ENUM('WEEKLY','MONTHLY','QUARTERLY','CUSTOM') NOT NULL,
  scope_type      ENUM('ACCOUNT','RESOURCE','INCIDENT','CLIENT') NOT NULL,
  scope_id        VARCHAR(191) NOT NULL,          -- aws_accounts.id / resource_id / incident id / client name
  account_id      BIGINT NULL,                    -- denormalized for RBAC scope filtering on list endpoints
  period_start    DATETIME NOT NULL,
  period_end      DATETIME NOT NULL,
  status          ENUM('QUEUED','PROCESSING','COMPLETE','FAILED') NOT NULL DEFAULT 'QUEUED',
  attempts        INT NOT NULL DEFAULT 0,
  max_attempts    INT NOT NULL DEFAULT 3,
  error_message   TEXT NULL,
  requested_by    VARCHAR(191) NOT NULL,          -- username, from write_audit's own convention
  requested_by_role VARCHAR(32) NULL,
  claimed_by      VARCHAR(64) NULL,               -- hostname:pid of the worker that picked this up
  claimed_at      DATETIME NULL,
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_report_jobs_status (status, created_at),
  INDEX idx_report_jobs_account (account_id),
  INDEX idx_report_jobs_scope (scope_type, scope_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS reports (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  job_id          BIGINT NOT NULL,
  report_type     ENUM('WEEKLY','MONTHLY','QUARTERLY','CUSTOM') NOT NULL,
  scope_type      ENUM('ACCOUNT','RESOURCE','INCIDENT','CLIENT') NOT NULL,
  scope_id        VARCHAR(191) NOT NULL,
  scope_label     VARCHAR(255) NULL,              -- human-readable, e.g. account name / resource name
  account_id      BIGINT NULL,
  period_start    DATETIME NOT NULL,
  period_end      DATETIME NOT NULL,
  s3_bucket       VARCHAR(255) NOT NULL,
  s3_key          VARCHAR(1024) NOT NULL,         -- full object key, see app/reports/s3_client.py for layout
  s3_version_id   VARCHAR(255) NULL,              -- captured if bucket versioning is on
  sha256          CHAR(64) NOT NULL,               -- integrity check, verified again before every download
  size_bytes      BIGINT NOT NULL,
  content_type    VARCHAR(100) NOT NULL DEFAULT 'application/pdf',
  generated_by    VARCHAR(191) NOT NULL,
  generated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at      DATETIME NOT NULL,               -- generated_at + RETENTION_DAYS, drives the S3 lifecycle rule
  emailed_at      DATETIME NULL,
  emailed_to      VARCHAR(255) NULL,
  UNIQUE KEY uniq_reports_job (job_id),
  INDEX idx_reports_scope (scope_type, scope_id),
  INDEX idx_reports_account (account_id),
  INDEX idx_reports_period (period_start, period_end),
  INDEX idx_reports_expires (expires_at),
  CONSTRAINT fk_reports_job FOREIGN KEY (job_id) REFERENCES report_jobs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ── Permission catalog additions ─────────────────────────────────────
INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('reports.view',     'Reports', 'View Reports',      'See generated report history', 0),
  ('reports.generate', 'Reports', 'Generate Reports',  'Trigger a new client/stakeholder report', 0),
  ('reports.download', 'Reports', 'Download Reports',  'Download a generated report file', 0),
  ('reports.email',    'Reports', 'Email Reports',     'Send a generated report to a stakeholder by email', 0)
;

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'viewer', id FROM permissions WHERE code IN ('reports.view');

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code IN
  ('reports.view','reports.generate','reports.download','reports.email');

-- admin gets everything implicitly (see app/auth/permissions.py docstring).
