-- db/migrations/020_metric_baseline_dynamic_thresholds.sql
--
-- Dynamic thresholds / anomaly detection, phase 1 of the roadmap
-- discussed 2026-09-13. Deliberately NOT a ML model -- same class of
-- seasonal-aware moving-stats band CloudWatch Anomaly Detection and
-- Azure Monitor Dynamic Thresholds use: mean/stddev bucketed by
-- hour-of-day and day-of-week, computed from data this app already
-- collects (metric_history). No new external service, no new cloud API
-- calls, no recurring cost.
--
-- metric_baseline is populated by app/collector/baseline.py's nightly
-- recompute_baselines() job (wired into app/collector/scheduler.py's low
-- tier, same place prune_metric_history() already runs) -- NOT written
-- by the request path, so reads of this table are always cheap.
--
-- resource_id here matches alerts.resource_id / resources.resource_id
-- (the AWS/Azure/GCP resource identifier string), NOT resources.id, so
-- app/collector/alert_evaluator.py can look it up with the same
-- aws_resource_id it already has in hand -- no extra join needed.
CREATE TABLE IF NOT EXISTS metric_baseline (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  resource_id   VARCHAR(512) NOT NULL,
  metric_name   VARCHAR(150) NOT NULL,
  hour_of_day   TINYINT      NOT NULL,   -- 0-23, HOUR(metric_timestamp)
  day_of_week   TINYINT      NOT NULL,   -- 0-6,  WEEKDAY(metric_timestamp) (0=Monday)
  mean_value    DOUBLE       NOT NULL,
  stddev_value  DOUBLE       NOT NULL DEFAULT 0,
  sample_count  INT          NOT NULL,
  updated_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_baseline_bucket (resource_id, metric_name, hour_of_day, day_of_week),
  KEY idx_baseline_lookup (resource_id, metric_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Opt-in per threshold row -- static thresholds keep working exactly as
-- before for anything NOT explicitly switched on. dynamic_k is the
-- number of standard deviations either side of the baseline mean that
-- counts as a breach (default 3.0 -- wide enough to avoid false
-- positives on a first rollout; tune down once false-positive rate is
-- observed in practice).
ALTER TABLE thresholds
  ADD COLUMN use_dynamic TINYINT(1) NOT NULL DEFAULT 0 AFTER enabled,
  ADD COLUMN dynamic_k   DOUBLE     NOT NULL DEFAULT 3.0 AFTER use_dynamic;
