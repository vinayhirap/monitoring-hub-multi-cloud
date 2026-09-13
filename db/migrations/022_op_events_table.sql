-- db/migrations/022_op_events_table.sql
--
-- Structured logging aggregation, roadmap phase 5 (2026-09-13).
--
-- DELIBERATELY NOT a full log-aggregation stack (Loki/ELK). This app
-- just removed one always-on binary (VictoriaMetrics) in favor of
-- operational simplicity -- adding another one now would cut against
-- that decision. Instead: a narrow, structured table for the specific
-- operational events that are actually useful to search when something
-- goes wrong -- collector cycle failures, alert-evaluation errors,
-- discovery failures -- not a firehose of every log line the app
-- produces. This gets most of the debugging value of full log
-- aggregation (the 2026-08-26 RCA needed to FIND specific collector
-- failures, not tail raw stdout) at zero infra cost.
--
-- Written by app/collector/op_log.py's log_event(), which ALSO still
-- calls Python's normal logger -- this table is additive, existing
-- server-log behavior is unchanged.
CREATE TABLE IF NOT EXISTS op_events (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  event_type    VARCHAR(80)  NOT NULL,   -- e.g. 'collector_cycle_failed', 'alert_eval_failed', 'discovery_failed'
  severity      VARCHAR(10)  NOT NULL DEFAULT 'ERROR',  -- INFO | WARNING | ERROR
  aws_account_id BIGINT      NULL,
  resource_id   VARCHAR(512) NULL,
  message       TEXT         NOT NULL,
  detail        JSON         NULL,       -- exception type/traceback snippet, tier, etc.
  created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_op_events_type_time (event_type, created_at),
  KEY idx_op_events_account   (aws_account_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
