-- db/migrations/123_host_agent_services.sql
--
-- Host agent: store the service states the agent already reports (the "watch_services" Configure field).
--
-- Until now POST /api/host-agents/ingest validated the `services` array and then dropped it, so a name typed into
-- Configure -> "Watched services" was sent to the agent, polled with systemctl / launchctl / sc.exe, and never seen again.
--
-- One row per (host agent, watched service name) holding the LATEST state only (no history table: a service flapping
-- every minute must not grow a table; state_since says when the current state began).
--
--   state        running | stopped | failed | unknown   (the agent contract's four values)
--   state_since  when the state last CHANGED, taken from the agent's own report time (clamped by the hub)
--   observed_at  when the agent last observed this state; ingest only ever moves it forward, so a late or replayed
--                payload can never overwrite a newer reading
--
-- ON DELETE CASCADE to host_agents: the rows describe the host and are meaningless without it (same as
-- host_agent_tokens in 120). Pruning of services that are no longer watched is done by ingest and by PATCH config,
-- not by a retention job: the table is bounded by 100 watched names per host.
--
-- Inert until HOST_AGENT_ENABLED=true. No permissions are seeded: reading uses the existing hostagents.view and the
-- list of watched names is still changed only with hostagents.manage. Idempotent, and nothing here is MySQL-8-only, so
-- it runs on MySQL 8 and MariaDB.

CREATE TABLE IF NOT EXISTS host_agent_services (
  host_agent_id BIGINT       NOT NULL,
  service_name  VARCHAR(100) NOT NULL,
  state         ENUM('running','stopped','failed','unknown') NOT NULL,
  state_since   DATETIME     NOT NULL,
  observed_at   DATETIME     NOT NULL,
  PRIMARY KEY (host_agent_id, service_name),
  KEY idx_hasvc_state (host_agent_id, state),
  CONSTRAINT fk_host_agent_services_agent FOREIGN KEY (host_agent_id) REFERENCES host_agents (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
