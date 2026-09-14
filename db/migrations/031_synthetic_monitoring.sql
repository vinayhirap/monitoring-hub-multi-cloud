-- db/migrations/031_synthetic_monitoring.sql
--
-- AIOps roadmap: synthetic/uptime (blackbox) monitoring, 2026-09-14.
--
-- Everything this app does today is PASSIVE -- it waits for a metric
-- to breach a threshold. It has no way to catch "the load balancer
-- reports healthy but the app actually returns 500s", a DNS
-- misconfiguration, or a soon-to-expire TLS cert, because none of
-- those show up as a CloudWatch/Azure Monitor/GCP metric. Synthetic
-- checks are active probes (HTTP/TCP/DNS) run FROM this app AGAINST a
-- target URL/host, on a schedule -- the same category of feature as
-- Datadog Synthetics / Pingdom / UptimeRobot, at a much smaller scope.
--
-- INTEGRATION DESIGN: a synthetic_checks row auto-creates/upserts a
-- matching `resources` row (resource_type = 'synthetic_check',
-- resource_id = 'synthetic-<check id>') the first time it runs -- see
-- app/collector/synthetic.py. This is deliberate: it means a failing
-- check becomes a normal `alerts` row against a normal `resources` row,
-- which for free (zero changes to any of these files) flows through:
--   - app/collector/correlate.py       (topology-based incident grouping)
--   - app/collector/health_score.py    (resource health scoring)
--   - app/collector/escalation.py      (existing escalation policies)
--   - app/collector/rca.py             (explain_alert / RCA)
--   - app/llm/summarizer.py            (LLM-polished summaries)
--   - app/api/alerts.py                (the existing Alerts page/API)
-- A synthetic check's outage is just another alert as far as every
-- one of those systems is concerned -- no special-casing needed
-- anywhere else in the app.
CREATE TABLE IF NOT EXISTS synthetic_checks (
  id                          BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id              BIGINT NOT NULL,
  name                        VARCHAR(255) NOT NULL,
  check_type                  ENUM('http', 'tcp', 'dns') NOT NULL DEFAULT 'http',
  -- http: full URL (https://api.example.com/health)
  -- tcp:  host:port (db.example.com:5432)
  -- dns:  hostname only (example.com)
  target                      VARCHAR(500) NOT NULL,
  expected_status_code        INT NULL,               -- http only, default 200 if NULL
  expected_keyword            VARCHAR(255) NULL,       -- http only, optional response-body substring check
  timeout_seconds             INT NOT NULL DEFAULT 10,
  interval_seconds            INT NOT NULL DEFAULT 300,
  -- A single failed probe is often just a network blip -- require this
  -- many CONSECUTIVE failures before writing a real alert, same
  -- "don't cry wolf on noise" philosophy as baseline.py's sigma-clipping
  -- and threshold_tuning.py's chronic-breach gates elsewhere in this app.
  consecutive_failure_threshold INT NOT NULL DEFAULT 2,
  enabled                     TINYINT(1) NOT NULL DEFAULT 1,
  environment                 VARCHAR(50) NOT NULL DEFAULT 'prod',
  next_check_at                DATETIME NULL,
  last_checked_at               DATETIME NULL,
  consecutive_failures         INT NOT NULL DEFAULT 0,
  current_status               ENUM('unknown', 'up', 'down') NOT NULL DEFAULT 'unknown',
  created_by                   VARCHAR(100) NULL,
  created_at                   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_synthetic_checks_due (enabled, next_check_at),
  INDEX idx_synthetic_checks_account (aws_account_id)
);

-- History of every probe run -- powers uptime % and response-time
-- charts. Pruned by app/collector/synthetic.py's own retention job
-- (same pattern as metrics_writer.py's prune_metric_history), NOT kept
-- forever -- one row per check per interval adds up fast at a 2-5 min
-- cadence across many checks.
CREATE TABLE IF NOT EXISTS synthetic_check_results (
  id                BIGINT AUTO_INCREMENT PRIMARY KEY,
  check_id          BIGINT NOT NULL,
  checked_at        DATETIME NOT NULL,
  success           TINYINT(1) NOT NULL,
  response_time_ms  INT NULL,
  status_code       INT NULL,
  error_message     VARCHAR(500) NULL,
  INDEX idx_synthetic_results_check_time (check_id, checked_at),
  CONSTRAINT fk_synthetic_results_check FOREIGN KEY (check_id)
    REFERENCES synthetic_checks(id) ON DELETE CASCADE
);

INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('synthetic.view',   'Monitoring', 'View Synthetic Checks',   'View uptime/synthetic check configuration and history', 0),
  ('synthetic.manage', 'Monitoring', 'Manage Synthetic Checks', 'Create, edit, pause, or delete uptime/synthetic checks', 0);

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'viewer', id FROM permissions WHERE code = 'synthetic.view';
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code IN ('synthetic.view', 'synthetic.manage');
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code IN ('synthetic.view', 'synthetic.manage');
