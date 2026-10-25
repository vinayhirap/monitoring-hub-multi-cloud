-- db/migrations/126_cloud_cost_azure_gcp.sql
--
-- Actual (billed) cost of CloudOps's own paid metric calls on Azure and GCP, so the Cloud Cost page works the same way
-- it does for AWS the day an Azure subscription or GCP project is onboarded.
--
--   cloud_cost_actuals    one row per (provider, billed account, region, SKU/meter, day). Upserted on every refresh, so
--                         re-reading an overlapping window never double counts. billed account = Azure subscription id
--                         or GCP project id. amount_usd is what the cloud billed (credits and free tier already applied).
--   cloud_cost_fetch_log  one row per refresh attempt per monitored account: outcome, how many requests it took and what
--                         asking cost (GCP: BigQuery bytes billed), so a missing permission or export is visible.
--
-- Inert until ACTUAL_COST_ENABLED=1 and an Azure/GCP account exists. Idempotent; MySQL 8 and MariaDB.

CREATE TABLE IF NOT EXISTS cloud_cost_actuals (
  provider        VARCHAR(10)   NOT NULL,
  billed_account  VARCHAR(100)  NOT NULL,
  region          VARCHAR(60)   NOT NULL DEFAULT '',
  sku             VARCHAR(160)  NOT NULL,
  usage_date      DATE          NOT NULL,
  amount_usd      DECIMAL(14,6) NOT NULL DEFAULT 0,
  quantity        DECIMAL(20,4) NULL,
  unit            VARCHAR(40)   NULL,
  fetched_via     INT           NULL,
  fetched_at      TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (provider, billed_account, region, sku, usage_date),
  KEY idx_cloud_cost_date (usage_date)
);

CREATE TABLE IF NOT EXISTS cloud_cost_fetch_log (
  id              BIGINT        NOT NULL AUTO_INCREMENT,
  provider        VARCHAR(10)   NOT NULL,
  aws_account_id  INT           NOT NULL,
  account_name    VARCHAR(255)  NULL,
  status          VARCHAR(24)   NOT NULL,
  requests        INT           NOT NULL DEFAULT 0,
  rows_written    INT           NOT NULL DEFAULT 0,
  check_cost_usd  DECIMAL(10,6) NULL,
  error           VARCHAR(500)  NULL,
  window_days     INT           NULL,
  fetched_at      TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_cloud_cost_fetch (provider, aws_account_id, fetched_at)
);
