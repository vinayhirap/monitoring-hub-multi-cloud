-- db/migrations/026_aiops_phase1.sql
--
-- AIOps roadmap Phase 1 (2026-09-14): topology-based alert correlation,
-- resource health scoring, and REAL cloud-resource-level RCA data.
--
-- IMPORTANT DISTINCTION this migration exists to fix:
-- op_events (022) is this APP's own operational health (collector
-- cycle failures, alert-eval errors). audit_logs (schema.sql) is
-- actions taken INSIDE this app (threshold edits, user management).
-- NEITHER is a record of what happened on the actual AWS/Azure/GCP
-- resources themselves. cloud_events below is the first table that IS
-- that -- sourced from AWS CloudTrail's LookupEvents API (free under
-- ReadOnlyAccess, no trail/S3 needed, AWS always retains 90 days of
-- management events per account at no extra charge). See
-- app/aws/cloudtrail_collector.py for the collector.
--
-- ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 on every table, matching every
-- other migration in this repo.

-- ── cloud_events: real AWS-resource-level activity (CloudTrail) ────
CREATE TABLE IF NOT EXISTS cloud_events (
  id             BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id BIGINT       NOT NULL,
  event_id       VARCHAR(64)  NOT NULL,   -- CloudTrail's own EventId (GUID)
  event_name     VARCHAR(128) NOT NULL,   -- e.g. AuthorizeSecurityGroupIngress, StopInstances
  event_source   VARCHAR(128) NOT NULL,   -- e.g. ec2.amazonaws.com
  event_time     TIMESTAMP    NOT NULL,
  username       VARCHAR(255) NULL,       -- CloudTrail's own actor field
  aws_region     VARCHAR(32)  NULL,
  resource_ids   JSON         NULL,       -- [{"type":"AWS::EC2::Instance","id":"i-..."}]
  raw_event      JSON         NULL,       -- bounded excerpt of CloudTrailEvent, for the RCA detail view
  created_at     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_cloud_event (aws_account_id, event_id),
  KEY idx_cloud_events_time (aws_account_id, event_time),
  KEY idx_cloud_events_name (event_name, event_time),
  CONSTRAINT fk_cloud_events_account FOREIGN KEY (aws_account_id)
    REFERENCES aws_accounts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── incidents: topology-correlated groups of active alerts ─────────
CREATE TABLE IF NOT EXISTS incidents (
  id                   BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id       BIGINT       NOT NULL,
  title                VARCHAR(255) NOT NULL,
  severity             VARCHAR(20)  NOT NULL,
  status               VARCHAR(20)  NOT NULL DEFAULT 'active',  -- active | resolved
  primary_resource_id  VARCHAR(512) NULL,      -- RCA-ranked probable root cause (app/collector/rca.py)
  probable_cause       TEXT         NULL,      -- human-readable RCA summary, always framed as "probable"
  started_at           TIMESTAMP    NOT NULL,
  resolved_at          TIMESTAMP    NULL,
  last_seen_at         TIMESTAMP    NOT NULL,
  created_at           TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_incidents_account_status (aws_account_id, status),
  KEY idx_incidents_started (started_at),
  CONSTRAINT fk_incidents_account FOREIGN KEY (aws_account_id)
    REFERENCES aws_accounts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS incident_alerts (
  incident_id BIGINT    NOT NULL,
  alert_id    BIGINT    NOT NULL,
  added_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (incident_id, alert_id),
  KEY idx_incident_alerts_alert (alert_id),
  CONSTRAINT fk_incident_alerts_incident FOREIGN KEY (incident_id)
    REFERENCES incidents(id) ON DELETE CASCADE,
  CONSTRAINT fk_incident_alerts_alert FOREIGN KEY (alert_id)
    REFERENCES alerts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── resource_health: computed 0-100 score, only for currently- ─────
-- breaching resources (see app/collector/health_score.py's docstring
-- for why a healthy resource simply has no row here rather than a
-- row pinned at 100).
CREATE TABLE IF NOT EXISTS resource_health (
  resource_id    VARCHAR(512) NOT NULL PRIMARY KEY,
  aws_account_id BIGINT       NOT NULL,
  health_score   TINYINT      NOT NULL,
  score_reason   JSON         NULL,
  computed_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_resource_health_account (aws_account_id, health_score),
  CONSTRAINT fk_resource_health_account FOREIGN KEY (aws_account_id)
    REFERENCES aws_accounts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── RBAC: read-only permission for the new Incidents view ──────────
-- Same read/write split precedent as topology.manage (024) -- this is
-- the read side only (there is no write/manage action on incidents in
-- this phase; incidents are entirely system-generated).
INSERT IGNORE INTO permissions (code, category, label, description, is_internal) VALUES
  ('incidents.view', 'Observability', 'View Incidents', 'View correlated incidents, resource health scores, and probable-root-cause analysis', 0);

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'viewer', id FROM permissions WHERE code = 'incidents.view';

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'editor', id FROM permissions WHERE code = 'incidents.view';

INSERT IGNORE INTO role_permissions (role, permission_id)
SELECT 'admin', id FROM permissions WHERE code = 'incidents.view';
