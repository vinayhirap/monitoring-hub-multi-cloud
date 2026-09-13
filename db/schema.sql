-- =========================
-- CLEAN SCHEMA (MySQL 8)
-- =========================

DROP TABLE IF EXISTS audit_logs;
DROP TABLE IF EXISTS alerts;
DROP TABLE IF EXISTS thresholds;
DROP TABLE IF EXISTS metrics;
DROP TABLE IF EXISTS resources;
DROP TABLE IF EXISTS aws_accounts;
DROP TABLE IF EXISTS environments;
DROP TABLE IF EXISTS users;

-- USERS
CREATE TABLE users (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  username VARCHAR(100) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  role VARCHAR(20) NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ENVIRONMENTS
CREATE TABLE environments (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(50) NOT NULL UNIQUE,
  tag_key VARCHAR(50) NOT NULL,
  tag_value VARCHAR(50) NOT NULL
);

-- AWS ACCOUNTS
CREATE TABLE aws_accounts (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  account_id VARCHAR(20) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL,
  role_arn VARCHAR(255) NOT NULL,
  external_id VARCHAR(100),
  region_default VARCHAR(50),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- RESOURCES
CREATE TABLE resources (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  aws_account_id BIGINT NOT NULL,
  resource_type VARCHAR(20) NOT NULL,
  resource_id VARCHAR(100) NOT NULL,
  name VARCHAR(255),
  tags JSON,
  environment_id BIGINT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- METRICS -- LAST-VALUE CACHE ONLY.
-- One row per (resource_id, metric_name), upserted by
-- app/collector/metrics_writer.py from VictoriaMetrics. This is NOT a
-- time-series store -- all history lives in VictoriaMetrics. This table
-- exists purely so alert_evaluator.py can join against `thresholds` in
-- SQL without a VM round trip per check. See db/migrations/004_metrics_last_value_only.sql.
CREATE TABLE metrics (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  resource_id BIGINT NOT NULL,
  metric_name VARCHAR(100) NOT NULL,
  metric_value DOUBLE,
  metric_timestamp DATETIME,
  UNIQUE KEY uniq_metrics_resource_metric (resource_id, metric_name)
);

-- THRESHOLDS
CREATE TABLE thresholds (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  metric_name VARCHAR(100) NOT NULL,
  environment_id BIGINT NOT NULL,
  warning DOUBLE NOT NULL,
  critical DOUBLE NOT NULL
);

-- ALERTS
CREATE TABLE alerts (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  resource_id BIGINT NOT NULL,
  metric_name VARCHAR(100),
  value DOUBLE,
  severity VARCHAR(20),
  status VARCHAR(20),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- AUDIT LOGS
CREATE TABLE audit_logs (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  actor VARCHAR(100),
  action VARCHAR(100),
  payload JSON,
  ip_address VARCHAR(45),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- BOOTSTRAP
INSERT INTO environments (name, tag_key, tag_value)
VALUES
  ('prod', 'environment', 'prod'),
  ('uat', 'environment', 'uat');

INSERT INTO thresholds (metric_name, environment_id, warning, critical)
SELECT 'cpu', id, 70, 90 FROM environments WHERE name='prod';

INSERT INTO thresholds (metric_name, environment_id, warning, critical)
SELECT 'cpu', id, 80, 95 FROM environments WHERE name='uat';