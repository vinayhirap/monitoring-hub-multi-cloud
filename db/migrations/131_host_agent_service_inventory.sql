-- db/migrations/131_host_agent_service_inventory.sql
--
-- Host agent: the list of services that EXIST on a host, so Configure -> "Add service" can offer a real picker instead of
-- blind free text (a typo such as "nginxS" was accepted and then reported "unknown" forever).
--
-- Separate from host_agent_services (migration 123), which holds the state of the WATCHED names only. This table is a
-- periodic snapshot of everything the OS service manager knows about (systemd units, Windows services, launchd labels),
-- sent by the agent on its first report after start and then every ~30 minutes, not every minute.
--
--   service_name  exactly the name the agent can watch (systemd unit without ".service", Windows service key name,
--                 launchd label); the hub only stores names that already satisfy the watch-list syntax rule
--   state         running | stopped | failed | unknown  (same four values as host_agent_services)
--   startup       systemd unit-file state (enabled / disabled / static / indirect / generated) or "system" for macOS
--                 Apple labels; empty when the OS exposes nothing cheap (Windows). Used only to separate the services a
--                 person installed or enabled from OS plumbing in the picker.
--   description   short human text (systemd Description, Windows display name); may be empty
--   scanned_at    when the agent took the snapshot. A snapshot is the COMPLETE set: ingest deletes rows older than the
--                 newest scan, and never lets an older payload overwrite a newer one.
--
-- Bounded at 1000 rows per host by ingest. ON DELETE CASCADE with the host (same as 123). Inert until
-- HOST_AGENT_ENABLED=true. Idempotent; nothing here is MySQL-8-only.

CREATE TABLE IF NOT EXISTS host_agent_service_inventory (
  host_agent_id BIGINT       NOT NULL,
  service_name  VARCHAR(100) NOT NULL,
  state         ENUM('running','stopped','failed','unknown') NOT NULL,
  startup       VARCHAR(24)  NOT NULL DEFAULT '',
  description   VARCHAR(160) NOT NULL DEFAULT '',
  scanned_at    DATETIME     NOT NULL,
  PRIMARY KEY (host_agent_id, service_name),
  KEY idx_hasi_scanned (host_agent_id, scanned_at),
  CONSTRAINT fk_host_agent_inventory_agent FOREIGN KEY (host_agent_id) REFERENCES host_agents (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
