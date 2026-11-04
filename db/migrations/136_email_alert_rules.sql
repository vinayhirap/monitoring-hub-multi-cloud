-- db/migrations/136_email_alert_rules.sql
--
-- Email & Reports centre, patch 0004: alert and incident email rules (the storage and the API; sending arrives in 0005).
--
--   email_alert_rules   which events (alert opened / escalated / resolved, incident opened / resolved) at or above which
--                       severity, for which account (NULL = every account), are mailed to which recipient list
--   email_rule_sends    one row per mail a rule sent: the memory behind the throttle, the hourly cap and "once per person"
--
-- No new permission: emailcenter.view / emailcenter.manage from migration 135 cover it.
-- Idempotent: CREATE TABLE IF NOT EXISTS only. Nothing MySQL-8-only, so it runs on MySQL 8 and MariaDB.
-- recipient_key is sha1(lower(address)) so the unique key stays short whatever the address length.

CREATE TABLE IF NOT EXISTS email_alert_rules (
  id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
  name               VARCHAR(120) NOT NULL,
  enabled            TINYINT(1) NOT NULL DEFAULT 1,
  events             VARCHAR(120) NOT NULL,                  -- comma-separated: alert_opened, alert_escalated, alert_resolved, incident_opened, incident_resolved
  min_severity       VARCHAR(10) NOT NULL DEFAULT 'WARNING', -- WARNING | CRITICAL
  account_id         BIGINT NULL,                            -- NULL = every account
  recipient_list_id  BIGINT NULL,
  extra_emails       TEXT NULL,
  created_by         VARCHAR(100) NULL,
  created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_email_alert_rule_name (name),
  INDEX idx_email_alert_rules_enabled (enabled, account_id),
  INDEX idx_email_alert_rules_list (recipient_list_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS email_rule_sends (
  id             BIGINT AUTO_INCREMENT PRIMARY KEY,
  rule_id        BIGINT NOT NULL,
  recipient_key  CHAR(40) NOT NULL,                          -- sha1 of the lower-cased address
  kind           VARCHAR(10) NOT NULL,                       -- alert | incident
  object_id      BIGINT NOT NULL,                            -- alerts.id or incidents.id
  event          VARCHAR(20) NOT NULL,
  resource_key   CHAR(40) NULL,                              -- sha1 of account + resource, for the per-resource throttle
  sent_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_rule_send_once (recipient_key, kind, object_id, event),
  INDEX idx_rule_sends_window (rule_id, recipient_key, sent_at),
  INDEX idx_rule_sends_sent (sent_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
