-- db/migrations/002c_metric_history_table.sql
--
-- A second gap in this repo's tracked migration history, found the same
-- way as 002b (replaying schema.sql + every migration from scratch): the
-- `metric_history` table has ZERO mentions anywhere in db/schema.sql or
-- db/migrations/ -- no CREATE, no ALTER, nothing -- despite being
-- actively written on every standard-tier collection cycle
-- (app/collector/metrics_writer.py's write_metric_history_batch(),
-- called from app/aws/collector_direct.py's Phase 1 GMD collector and
-- both app/providers/{azure,gcp}/metrics_collector.py's Phase 2/3
-- collectors), actively pruned daily
-- (app/collector/metrics_writer.py's prune_metric_history(), see
-- app/collector/scheduler.py's low tier), and actively read for every
-- resource-detail chart in the frontend
-- (app/aws/collector_direct.py's _metric_history_query_range()). On a
-- real, already-running deployment the table obviously already exists
-- (this app's charts work) -- this is the same class of "migration
-- never captured, only ever created by hand or by a since-lost one-off
-- script" gap 002b closed for metric_catalog, just for a table with NO
-- partial migration trail at all rather than one 003 half-referenced via
-- ALTER.
--
-- Column list, types, and the exact query pattern (equality filter on
-- resource_id + metric_name, range filter + ORDER BY on
-- metric_timestamp, DELETE ... WHERE metric_timestamp < ... for pruning)
-- reconstructed directly from write_metric_history_batch(),
-- prune_metric_history(), and _metric_history_query_range() -- not
-- guessed. Deliberately has NO unique key on (resource_id, metric_name)
-- the way `metrics` does -- unlike that table (a last-value cache, one
-- row per pair), this one is genuine multi-row time-series history by
-- design (see metrics_writer.py's own module docstring for the
-- distinction), so a duplicate/near-duplicate row here is expected
-- behavior, not a bug a unique key should prevent.

CREATE TABLE IF NOT EXISTS metric_history (
  id               BIGINT AUTO_INCREMENT PRIMARY KEY,
  resource_id      BIGINT        NOT NULL,
  metric_name      VARCHAR(150)  NOT NULL,
  metric_value     DOUBLE        NULL,
  metric_timestamp DATETIME      NOT NULL,

  -- Matches _metric_history_query_range()'s exact WHERE clause
  -- (resource_id + metric_name equality, metric_timestamp range,
  -- ORDER BY metric_timestamp) so every chart-loading query this table
  -- exists for can use a single covering-ish index instead of a table
  -- scan once history accumulates past a trivial row count.
  KEY idx_metric_history_lookup (resource_id, metric_name, metric_timestamp),

  -- Matches prune_metric_history()'s DELETE ... WHERE metric_timestamp < ...,
  -- run daily -- without this, pruning degrades to a full table scan as
  -- the table grows, on exactly the query that's supposed to be keeping
  -- it small.
  KEY idx_metric_history_timestamp (metric_timestamp),

  CONSTRAINT fk_metric_history_resource
    FOREIGN KEY (resource_id) REFERENCES resources(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
