-- 081_alerts_value_into_current_value.sql
--
-- Audit D4. `alerts` carried two columns for the same reading: the legacy `value` (pre-012) and `current_value`.
-- Every writer today (alert evaluator, synthetic checks, multivariate anomaly) sets only current_value, and the API
-- returned both, so a consumer could not tell which to trust. Old rows are copied across here so `current_value` is
-- the single source; the API no longer returns `value`.
--
-- Non-destructive: the legacy column is left in place (dropping it is a separate, later decision). Idempotent: only
-- fills rows where current_value is still NULL, so re-running changes nothing.

UPDATE alerts
   SET current_value = value
 WHERE current_value IS NULL
   AND value IS NOT NULL;
