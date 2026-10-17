-- db/migrations/099_environment_compliance_snapshots.sql
--
-- Workstream C: a daily snapshot of tag compliance and of how each environment was decided, per account and
-- resource type, so the Environments page can show a trend instead of only "now".
--
-- One row per (UTC day, account, resource type); a second run on the same day overwrites that day's row.
-- not_checked is its own column on purpose: it means "CloudOps does not read this type's own AWS tags", and
-- must never be folded into "missing". unclassified counts rows env_sync has not evaluated yet.
-- Numeric keys only (no string joins), so the table's collation never meets another table's.

CREATE TABLE IF NOT EXISTS environment_compliance_snapshots (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  snapshot_date   DATE NOT NULL,
  aws_account_id  BIGINT NOT NULL,
  resource_type   VARCHAR(20) NOT NULL,
  total           INT NOT NULL DEFAULT 0,
  tag_ok          INT NOT NULL DEFAULT 0,
  tag_nonstandard INT NOT NULL DEFAULT 0,
  tag_invalid     INT NOT NULL DEFAULT 0,
  tag_missing     INT NOT NULL DEFAULT 0,
  tag_not_checked INT NOT NULL DEFAULT 0,
  tag_pending     INT NOT NULL DEFAULT 0,
  src_manual      INT NOT NULL DEFAULT 0,
  src_tag         INT NOT NULL DEFAULT 0,
  src_name        INT NOT NULL DEFAULT 0,
  src_related     INT NOT NULL DEFAULT 0,
  src_account     INT NOT NULL DEFAULT 0,
  src_unknown     INT NOT NULL DEFAULT 0,
  src_pending     INT NOT NULL DEFAULT 0,
  warnings        INT NOT NULL DEFAULT 0,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_env_compliance_day (snapshot_date, aws_account_id, resource_type),
  INDEX idx_env_compliance_account (aws_account_id, snapshot_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
