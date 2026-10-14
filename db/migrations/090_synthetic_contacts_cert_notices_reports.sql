-- 090_synthetic_contacts_cert_notices_reports.sql
--
-- Synthetic checks: per-check alert contacts, per-check certificate thresholds, certificate-notice state, a daily
-- roll-up so reports can look back further than the raw probe retention, and the SYNTHETIC report scope.
--
--   synthetic_checks.cert_warn_days / cert_crit_days   NULL = the global defaults (30 / 7, SYNTHETIC_CERT_*_DAYS)
--   synthetic_check_contacts                           who is told, per check, about an outage and / or a certificate
--   synthetic_notification_state                       one row per certificate per check: which stage was last sent and
--                                                      when, so each stage is sent once and reminders are paced
--   synthetic_daily_stats                              one row per check per UTC day; raw results are pruned after 30 days
--                                                      but a monthly / quarterly report needs the numbers, kept 400 days
--   report_jobs / reports .scope_type                  + 'SYNTHETIC'
--
-- Idempotent: every change is guarded, safe to re-run. Apply BEFORE restarting the backend.

-- 1. per-check certificate thresholds (days before expiry)
SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'synthetic_checks' AND column_name = 'cert_warn_days');
SET @s := IF(@c = 0, 'ALTER TABLE synthetic_checks ADD COLUMN cert_warn_days INT NULL', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = DATABASE() AND table_name = 'synthetic_checks' AND column_name = 'cert_crit_days');
SET @s := IF(@c = 0, 'ALTER TABLE synthetic_checks ADD COLUMN cert_crit_days INT NULL', 'SELECT 1');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 2. contacts (email addresses; one list per check, each address can be limited to outages or to certificates)
CREATE TABLE IF NOT EXISTS synthetic_check_contacts (
  id             BIGINT AUTO_INCREMENT PRIMARY KEY,
  check_id       BIGINT NOT NULL,
  email          VARCHAR(254) NOT NULL,
  label          VARCHAR(100) NULL,                    -- "Network team", "Site owner"
  notify_outage  TINYINT(1) NOT NULL DEFAULT 1,
  notify_cert    TINYINT(1) NOT NULL DEFAULT 1,
  created_by     VARCHAR(100) NULL,
  created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_synthetic_contact (check_id, email),
  CONSTRAINT fk_synthetic_contact_check FOREIGN KEY (check_id)
    REFERENCES synthetic_checks(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. certificate notice state. subject_key = the certificate's notAfter ("2027-02-01 12:00:00"): a renewed
--    certificate is a new key, so its stages start again and the old row is how a "renewed" notice is recognised.
CREATE TABLE IF NOT EXISTS synthetic_notification_state (
  check_id       BIGINT NOT NULL,
  kind           VARCHAR(16) NOT NULL,                 -- 'cert'
  subject_key    VARCHAR(40) NOT NULL,
  stage          VARCHAR(12) NOT NULL,                 -- warn | crit | expired
  last_sent_at   DATETIME NOT NULL,
  sent_count     INT NOT NULL DEFAULT 1,
  PRIMARY KEY (check_id, kind, subject_key),
  CONSTRAINT fk_synthetic_state_check FOREIGN KEY (check_id)
    REFERENCES synthetic_checks(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. daily roll-up of probe results
CREATE TABLE IF NOT EXISTS synthetic_daily_stats (
  check_id        BIGINT NOT NULL,
  day             DATE NOT NULL,
  probes          INT NOT NULL,
  ok              INT NOT NULL,
  avg_ms          INT NULL,
  p95_ms          INT NULL,
  max_ms          INT NULL,
  min_cert_days   INT NULL,
  PRIMARY KEY (check_id, day),
  CONSTRAINT fk_synthetic_daily_check FOREIGN KEY (check_id)
    REFERENCES synthetic_checks(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5. report scope
ALTER TABLE report_jobs
  MODIFY COLUMN scope_type ENUM('ACCOUNT','RESOURCE','INCIDENT','CLIENT','SYNTHETIC') NOT NULL;
ALTER TABLE reports
  MODIFY COLUMN scope_type ENUM('ACCOUNT','RESOURCE','INCIDENT','CLIENT','SYNTHETIC') NOT NULL;
