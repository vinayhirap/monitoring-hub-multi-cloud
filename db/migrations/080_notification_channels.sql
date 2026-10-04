-- 080_notification_channels.sql
--
-- Audit C9. Alerts reached nobody outside the browser: the escalation engine reassigned an alert to
-- a group and recorded it, but only emailed that group (and only if SMTP was configured). There was
-- no way to send an alert to Slack, Teams, a generic webhook or a team mailbox, and Settings had a
-- "Webhook URL" box wired to nothing.
--
-- notification_channels: one row per destination.
--   type       email | slack | teams | webhook
--   target     email: comma-separated addresses; slack/teams/webhook: the https URL.
--              The URL IS the secret for Slack/Teams incoming webhooks, so the API never returns it
--              whole (it returns the host only) and the audit log never records it.
--   min_severity  WARNING sends warnings and criticals; CRITICAL sends criticals only.
--   events     which lifecycle events are delivered (opened | escalated), comma-separated.
--   aws_account_id  NULL = every account; otherwise only that account's alerts.
-- notification_log: one row per delivery attempt, so "did anyone get told?" is answerable.
-- Also seeds notifications.manage (admin only by default) so the feature has its own permission.

CREATE TABLE IF NOT EXISTS notification_channels (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  name            VARCHAR(100) NOT NULL,
  type            ENUM('email','slack','teams','webhook') NOT NULL,
  target          VARCHAR(1000) NOT NULL,
  min_severity    ENUM('WARNING','CRITICAL') NOT NULL DEFAULT 'CRITICAL',
  events          VARCHAR(50) NOT NULL DEFAULT 'opened,escalated',
  aws_account_id  BIGINT NULL,
  enabled         TINYINT(1) NOT NULL DEFAULT 1,
  created_by      VARCHAR(100) NULL,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_notification_channel_name (name),
  INDEX idx_notification_channels_enabled (enabled, min_severity)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS notification_log (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  channel_id  BIGINT NULL,
  channel_name VARCHAR(100) NULL,
  alert_id    BIGINT NULL,
  event       VARCHAR(20) NOT NULL,
  status      ENUM('sent','failed','skipped') NOT NULL,
  detail      VARCHAR(500) NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_notification_log_created (created_at),
  INDEX idx_notification_log_channel (channel_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('notifications.manage', 'Operations', 'Manage Notification Channels',
   'Create, edit, test and delete alert notification channels (email, Slack, Teams, webhook)', 0);

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code = 'notifications.manage';
