-- db/migrations/073_capacity_metrics_static_only.sql
--
-- disk_used_percent / mem_used_percent (and per-mount disk_used_percent__*)
-- are capacity metrics: the absolute level is the signal, so they alert on
-- the static warning/critical line only. app/collector/alert_evaluator.py
-- already ignores use_dynamic for them; this puts the stored flag back to
-- what is actually enforced so the Settings UI is truthful.
--
-- Why this happened (prod 2026-09-29): threshold_tuning.py switched account
-- 10's disk row (id 506, 2026-09-22) and account 7's mem row (id 268,
-- 2026-09-16) to dynamic because ONE instance in each account sat above the
-- 80 line; the account-wide flip then made healthy ~65% disks flap.
--
-- Idempotent: safe to re-run. Reversible for the two known rows with
--   UPDATE thresholds SET use_dynamic = 1 WHERE id IN (506, 268);
UPDATE thresholds t
JOIN metric_catalog mc ON mc.id = t.metric_id
SET t.use_dynamic = 0
WHERE t.use_dynamic = 1
  AND (mc.metric_name = 'mem_used_percent'
       OR mc.metric_name = 'disk_used_percent'
       OR mc.metric_name LIKE 'disk\_used\_percent\_\_%');
