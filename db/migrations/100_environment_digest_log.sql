-- db/migrations/100_environment_digest_log.sql
--
-- Workstream C: the weekly digest of NEW unknown resources (app/env_digest.py).
--
-- One row per (account, ISO week). The row is INSERTed before anything is sent and the unique key makes a second
-- attempt in the same week a no-op, so a scheduler restart or a leadership change cannot send it twice.
-- status: claimed (about to send) | sent | no_recipients | failed | empty.
-- Nothing is sent unless ENV_DIGEST_ENABLED=true; this table is only written when that flag is on.

CREATE TABLE IF NOT EXISTS environment_digest_log (
  id               BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id   BIGINT NOT NULL,
  week_key         CHAR(8) NOT NULL,
  status           VARCHAR(20) NOT NULL DEFAULT 'claimed',
  new_unknown      INT NOT NULL DEFAULT 0,
  total_unknown    INT NOT NULL DEFAULT 0,
  recipients       INT NOT NULL DEFAULT 0,
  detail           VARCHAR(300) NULL,
  created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_env_digest_week (aws_account_id, week_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
