-- db/migrations/043_metric_history_dedup_key.sql
--
-- Adds a UNIQUE key on metric_history (resource_id, metric_name,
-- metric_timestamp), de-duplicating any existing violating rows first.
--
-- Why this is needed now (found live, 2026-09-16, AuroGov Mumbai):
-- app/collector/metrics/extended.py's slow_extended-tier GetMetricData
-- lookback window was just widened, first to ~24h (to actually span
-- the gap between polls -- see that file's _LOOKBACK_MINUTES docstring)
-- and then to ~48h+buffer specifically to cover AWS's own documented
-- up-to-48-hour delivery delay for S3's daily storage metrics.
--
-- A window this much wider than the poll interval (24h) GUARANTEES
-- overlapping time ranges between consecutive slow_extended runs --
-- the same datapoint from yesterday's run will be returned again by
-- today's GetMetricData call. write_metric_history_batch() has always
-- been a plain INSERT with no uniqueness to catch this (see that
-- function's own comment: "every call ADDS rows"), because the old
-- 16-minute window (the bug this whole change fixes) made the
-- collision vanishingly rare in practice. The new, correctly-sized
-- windows make it the NORMAL case, every day, for every
-- slow_extended-tier resource/metric -- without this key,
-- metric_history would grow a full extra duplicate row per metric per
-- day, forever, and every chart reading that table would show doubled
-- (but identical-valued, so visually invisible until you count rows)
-- history for these services.
--
-- Existing duplicate rows are possible even before this change (e.g.
-- an overlapping manual re-run, a retried GMD batch after a partial
-- failure) -- deleted here first, keeping the highest id (the most
-- recently written copy) per group. Same "keep one, delete the rest"
-- shape apply_metrics_dedup_fix.py already used for the analogous
-- problem in the `metrics` table (see that script's own docstring);
-- done here as a plain tracked migration instead of a standalone
-- script since metric_history's dedup key is a one-column-set,
-- one-statement job with no ALTER-already-applied edge case to guard
-- (migrate.py's own tracking table already makes re-running this file
-- a no-op).
DELETE h FROM metric_history h
INNER JOIN (
  SELECT resource_id, metric_name, metric_timestamp, MAX(id) AS keep_id
  FROM metric_history
  GROUP BY resource_id, metric_name, metric_timestamp
  HAVING COUNT(*) > 1
) dupes
  ON h.resource_id = dupes.resource_id
 AND h.metric_name = dupes.metric_name
 AND h.metric_timestamp = dupes.metric_timestamp
 AND h.id <> dupes.keep_id;

ALTER TABLE metric_history
  ADD UNIQUE KEY uniq_metric_history_resource_metric_ts (resource_id, metric_name, metric_timestamp);
