-- db/migrations/111_aws_actual_cost.sql
--
-- Actual AWS CloudWatch charges, read from the Cost Explorer API (ce:GetCostAndUsage) so the Cloud API cost page can
-- show what AWS really billed next to CloudOps's own list-price estimate.
--
--   aws_cost_actuals    one row per (billed AWS account, usage type, day). Upserted on every refresh, so re-fetching
--                       an overlapping window (or two monitored accounts that can both see the same billed account,
--                       e.g. a payer and a member) never double counts.
--   aws_cost_fetch_log  one row per refresh attempt per monitored account: outcome, Cost Explorer requests made
--                       (each one is billed $0.01 by AWS) and the error class, so a missing permission is visible.
--
-- Inert until ACTUAL_COST_ENABLED=1 is set and an account's role has ce:GetCostAndUsage. Idempotent; MySQL 8 and
-- MariaDB. Also makes the already-seeded usage.manage permission real: it now gates the manual refresh endpoint.

CREATE TABLE IF NOT EXISTS aws_cost_actuals (
  billed_account_id VARCHAR(20)   NOT NULL,
  usage_type        VARCHAR(120)  NOT NULL,
  usage_date        DATE          NOT NULL,
  amount_usd        DECIMAL(14,6) NOT NULL DEFAULT 0,
  estimated         TINYINT(1)    NOT NULL DEFAULT 0,
  fetched_via       INT           NULL,
  fetched_at        TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (billed_account_id, usage_type, usage_date),
  KEY idx_cost_actuals_date (usage_date)
);

CREATE TABLE IF NOT EXISTS aws_cost_fetch_log (
  id              BIGINT       NOT NULL AUTO_INCREMENT,
  aws_account_id  INT          NOT NULL,
  account_name    VARCHAR(255) NULL,
  status          VARCHAR(24)  NOT NULL,
  ce_requests     INT          NOT NULL DEFAULT 0,
  rows_written    INT          NOT NULL DEFAULT 0,
  error           VARCHAR(500) NULL,
  fetched_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_cost_fetch_account (aws_account_id, fetched_at)
);
