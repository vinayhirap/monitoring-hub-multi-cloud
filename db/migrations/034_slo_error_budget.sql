-- db/migrations/034_slo_error_budget.sql
--
-- SLO / error-budget tracking (2026-09-14).
--
-- An SLO (Service Level Objective) is a promise like "99.9% of the
-- time, this service is healthy". An error budget is the flip side:
-- 99.9% over 30 days allows ~43 minutes of "bad time" before the
-- promise is broken. This turns a wall of individual alerts into a
-- single number a non-engineer can act on ("we've used 60% of this
-- month's error budget") instead of "CPU hit 95% for 3 minutes,
-- twice" -- the same shift Google's SRE practice and every serious
-- observability vendor (Datadog, Honeycomb, Grafana) charges extra
-- for.
--
-- TWO MEASUREMENT MODES, same table:
--   1. synthetic_check_id set -- "bad time" = failed probes (see
--      app/collector/synthetic.py's synthetic_check_results), i.e. a
--      real external-availability SLO ("99.9% of health-check probes
--      succeeded").
--   2. resource_id (+ optional metric_name) set -- "bad time" = total
--      duration of CRITICAL alerts.status on that resource/metric
--      within the window, i.e. an infra-health SLO that works for ANY
--      resource, even one with no synthetic check configured.
-- Exactly one of the two must be set -- enforced in
-- app/api/slo.py, not a DB constraint (MySQL CHECK constraints on
-- cross-column XOR are awkward and this app's existing convention,
-- see app/api/synthetic.py's create_check, is app-layer validation).
--
-- DELIBERATELY NO SEPARATE "evaluations" table -- error-budget
-- consumption is computed ON DEMAND from alerts/synthetic_check_results
-- at read time (see app/api/slo.py's GET /api/slo), same "pure
-- computation over already-collected data" cost profile as
-- app/api/deploy_risk.py. A daily materialized snapshot would only be
-- worth the complexity at a scale (thousands of SLOs, dashboards
-- polled every few seconds) this app isn't at.
CREATE TABLE IF NOT EXISTS slo_definitions (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id      BIGINT NOT NULL,
  name                VARCHAR(255) NOT NULL,
  synthetic_check_id  BIGINT NULL,
  resource_id         VARCHAR(512) NULL,   -- resources.resource_id, NOT resources.id -- matches alerts.resource_id's own convention
  metric_name         VARCHAR(100) NULL,   -- NULL = any CRITICAL alert on resource_id counts as a breach
  target_pct          DECIMAL(5,3) NOT NULL,  -- e.g. 99.900
  window_days         INT NOT NULL DEFAULT 30,
  enabled             TINYINT(1) NOT NULL DEFAULT 1,
  created_by          VARCHAR(100) NULL,
  created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_slo_account (aws_account_id),
  CONSTRAINT fk_slo_synthetic_check FOREIGN KEY (synthetic_check_id)
    REFERENCES synthetic_checks(id) ON DELETE CASCADE
);

INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('slo.view',   'Monitoring', 'View SLOs',   'View SLO definitions and error-budget status', 0),
  ('slo.manage', 'Monitoring', 'Manage SLOs', 'Create, edit, or delete SLO definitions', 0);

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'viewer', id FROM permissions WHERE code = 'slo.view';
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code IN ('slo.view', 'slo.manage');
INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code IN ('slo.view', 'slo.manage');
