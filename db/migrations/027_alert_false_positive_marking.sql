-- db/migrations/027_alert_false_positive_marking.sql
--
-- Closes the loop on this session's false-alert-reduction work: the
-- system already self-corrects chronic/flapping thresholds
-- automatically (app/collector/threshold_tuning.py), but there was no
-- way for a HUMAN to directly say "this specific alert wasn't
-- genuine" and have that recorded -- both as an audit trail (who
-- decided this alert was noise, and when) and as a stronger signal
-- than the automatic detector alone (a person confirming it beats a
-- statistical inference).
--
-- Deliberately three separate columns, not a single boolean, matching
-- this app's own existing convention on `alerts` (resolved_at,
-- escalated_at, escalated_to_group_id are already separate "did X
-- happen, by/to whom, when" triples rather than one flag) -- this is
-- both an audit trail and machine-readable, not just a checkbox.

ALTER TABLE alerts
  ADD COLUMN marked_false_positive    TINYINT(1) NOT NULL DEFAULT 0 AFTER escalated_to_group_id,
  ADD COLUMN false_positive_marked_by VARCHAR(255) NULL             AFTER marked_false_positive,
  ADD COLUMN false_positive_marked_at TIMESTAMP NULL                AFTER false_positive_marked_by;

-- Supports app/collector/threshold_tuning.py's new manually-confirmed
-- path: "how many times has THIS resource+metric been marked false
-- positive, ever (not just this one still-active alert)" -- a
-- resource whose past alerts on this metric were repeatedly marked
-- false positive is strong evidence the threshold is wrong for it,
-- even faster than waiting for the automatic chronic-mean/chronic-
-- noise paths' own confidence bars.
CREATE INDEX idx_alerts_false_positive_lookup
  ON alerts (resource_id, metric_name, marked_false_positive);
