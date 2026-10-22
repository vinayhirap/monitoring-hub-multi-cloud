-- db/migrations/121_host_agent_watchdog_state.sql
--
-- Phase D (host agent health and alerting): bookkeeping for app/collector/host_agent_watchdog.py.
--
-- One row per host agent: the state the watchdog last derived (online | stale | offline), since when, whether an
-- "offline" is expected because the EC2 instance itself is stopped (offline_reason = 'instance_stopped', no alert),
-- the alert row it currently owns, and the human text it raised (the alerts table has no message column).
-- Everything about alert lifecycle, dedupe and silencing stays in the existing `alerts` table.
--
-- Does NOT touch the Phase B tables (host_agents, host_agent_tokens, host_agent_enrollment_keys); it only reads
-- host_agents.  No FK to host_agents / aws_accounts on purpose (same reason as migration 120: the trail must survive
-- deletes; the watchdog removes the row itself when its host is revoked or deleted).
--
-- No permissions are seeded: Phase D adds no routes.  Inert until HOST_AGENT_ENABLED=true.  Idempotent, and uses
-- nothing MySQL-8-only, so it runs on MySQL 8 and MariaDB.

CREATE TABLE IF NOT EXISTS host_agent_watchdog_state (
  host_agent_id     BIGINT        NOT NULL,
  aws_account_id    BIGINT        NOT NULL,
  state             ENUM('online','stale','offline') NOT NULL,
  state_since       DATETIME      NOT NULL,
  offline_reason    VARCHAR(40)   NULL,
  alert_id          BIGINT        NULL,
  alert_resource_id VARCHAR(512)  NULL,
  detail            VARCHAR(500)  NULL,
  evaluated_at      DATETIME      NOT NULL,
  PRIMARY KEY (host_agent_id),
  KEY idx_hawstate_account_state (aws_account_id, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
