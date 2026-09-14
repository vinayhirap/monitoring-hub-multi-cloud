-- db/migrations/028_baseline_stl_method.sql
--
-- AIOps roadmap #9 (STL seasonal decomposition upgrade, 2026-09-14).
--
-- app/collector/baseline.py's hour-of-day/day-of-week bucketing (see
-- its own docstring) is a real, working dynamic-threshold model, but
-- it treats each (hour, weekday) bucket independently -- it has no
-- notion of a smooth daily/weekly SHAPE, so a metric with strong
-- overlapping daily+weekly seasonality (e.g. request-count traffic:
-- busy at noon, quiet at 3am, AND busy weekdays, quiet weekends) gets
-- a noisier mean/stddev per bucket than a proper decomposition would
-- give, because each bucket is only ever averaged against itself,
-- never informed by the smooth trend the neighboring buckets share.
--
-- app/collector/baseline_stl.py adds an OPTIONAL upgrade pass, using
-- statsmodels' STL (Seasonal-Trend decomposition using LOESS) to
-- separate trend/seasonal/residual components from a resource+metric's
-- full time series, then re-derives each (hour, weekday) bucket's
-- mean from the trend+seasonal fit and stddev from the residual --
-- structurally the same table, same columns, same consumer
-- (alert_evaluator.py's _dynamic_bounds(), completely unchanged).
--
-- This column is purely for transparency/audit ("which method produced
-- this bucket's numbers") -- nothing reads it to change behavior.
-- Defaulting every existing row to 'sigma_clip' is accurate: that is
-- genuinely how every row currently in this table was computed.
ALTER TABLE metric_baseline
  ADD COLUMN computed_by ENUM('sigma_clip', 'stl') NOT NULL DEFAULT 'sigma_clip'
    AFTER sample_count;
