-- db/migrations/135_email_center.sql
--
-- Email & Reports centre, foundation (patch 0001). Inert until EMAIL_CENTER_ENABLED=true.
--
--   email_delivery_log        one row per email handed to SMTP (sent / failed / skipped), from every feature
--   email_recipient_lists     named recipient lists: CloudOps users, org groups and typed addresses
--   report_schedules          which report is emailed to which list, how often (the scheduler arrives in patch 0002)
--   report_jobs / reports     report_type gains YEARLY (appended at the end: no table rewrite, existing rows untouched)
--   emailcenter.view          editor, admin   status, schedules
--   emailcenter.manage        admin only      lists, schedules, delivery log, test email
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, MODIFY COLUMN sets the same list, INSERT IGNORE for the permissions.
-- Uses nothing MySQL-8-only, so it runs on MySQL 8 and MariaDB. No string joins between these tables and others.

CREATE TABLE IF NOT EXISTS email_delivery_log (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  category    VARCHAR(30) NOT NULL DEFAULT 'system',      -- alert | report | escalation | test | system ...
  to_addr     VARCHAR(254) NOT NULL,
  subject     VARCHAR(255) NULL,
  status      ENUM('sent','failed','skipped') NOT NULL,
  error       VARCHAR(300) NULL,                          -- short, secret-free reason (mailer._short_err)
  report_id   BIGINT NULL,
  schedule_id BIGINT NULL,
  INDEX idx_email_log_created (created_at),
  INDEX idx_email_log_status (status, created_at),
  INDEX idx_email_log_category (category, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS email_recipient_lists (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  name         VARCHAR(100) NOT NULL,
  description  VARCHAR(255) NULL,
  user_ids     TEXT NULL,                                  -- JSON list of users.id (current email is read when sending)
  group_ids    TEXT NULL,                                  -- JSON list of org_groups.id (active members)
  emails       TEXT NULL,                                  -- comma-separated typed addresses
  created_by   VARCHAR(100) NULL,
  created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_email_recipient_list_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS report_schedules (
  id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
  name               VARCHAR(120) NOT NULL,
  enabled            TINYINT(1) NOT NULL DEFAULT 1,
  report_type        ENUM('WEEKLY','MONTHLY','QUARTERLY','YEARLY') NOT NULL,
  scope_type         VARCHAR(20) NOT NULL,
  scope_id           VARCHAR(191) NOT NULL,
  account_id         BIGINT NULL,
  run_weekday        TINYINT NULL,                         -- 0 = Monday .. 6 = Sunday (WEEKLY)
  run_month          TINYINT NULL,                         -- 1..12 (YEARLY)
  run_day            TINYINT NULL,                         -- 1..28 (MONTHLY, QUARTERLY, YEARLY)
  run_hour           TINYINT NOT NULL DEFAULT 8,
  run_minute         TINYINT NOT NULL DEFAULT 0,
  timezone           VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
  recipient_list_id  BIGINT NULL,
  extra_emails       TEXT NULL,
  created_by         VARCHAR(100) NULL,
  created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  last_run_at        DATETIME NULL,
  last_period_end    DATETIME NULL,
  last_status        VARCHAR(20) NULL,
  last_error         VARCHAR(300) NULL,
  UNIQUE KEY uq_report_schedule_name (name),
  INDEX idx_report_schedules_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE report_jobs
  MODIFY COLUMN report_type ENUM('WEEKLY','MONTHLY','QUARTERLY','CUSTOM','YEARLY') NOT NULL;
ALTER TABLE reports
  MODIFY COLUMN report_type ENUM('WEEKLY','MONTHLY','QUARTERLY','CUSTOM','YEARLY') NOT NULL;

INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('emailcenter.view',   'Operations', 'View Email & Reports',
   'See the email delivery status and the report schedules', 0),
  ('emailcenter.manage', 'Operations', 'Manage Email & Reports',
   'Send test emails, manage recipient lists and report schedules, read the email delivery log', 0);

-- v2 resolver
INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'editor' AND p.code IN ('emailcenter.view');

INSERT IGNORE INTO role_permissions_v2 (role_id, permission_id)
SELECT r.id, p.id FROM roles r CROSS JOIN permissions p
WHERE r.role_key = 'admin' AND p.code IN ('emailcenter.view', 'emailcenter.manage');

-- legacy role_permissions
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code IN ('emailcenter.view');
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code IN ('emailcenter.view', 'emailcenter.manage');
