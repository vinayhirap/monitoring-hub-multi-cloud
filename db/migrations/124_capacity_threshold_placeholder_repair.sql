-- db/migrations/124_capacity_threshold_placeholder_repair.sql
--
-- Repair capacity thresholds that were left on the "no static line" placeholder (warning 1000000 / critical 5000000 / '>').
--
-- Why: ensure_disk_mount_metric_registered() clones the account's disk_used_percent row for each new mount. When that base
-- row was itself the placeholder, the clone inherited it (seen on prod as disk_used_percent__boot, "Warn 1000000" in
-- Settings, and the row could never alert). The code now replaces the placeholder at clone time (audit F1), but INSERT-only
-- registration never revisits rows that already exist, so rows created earlier stay broken.
--
-- Disk and memory *used %* are capacity metrics: the absolute level is the signal and they alert on the static line only
-- (migration 073, threshold_defaults.CAPACITY_PERCENT_METRICS), so a placeholder on them is always wrong. Shipped default:
-- 80 / 90 / '>' (threshold_defaults.DEFAULT_THRESHOLDS).
--
-- Touches ONLY rows that are EXACTLY the placeholder, so a value a person chose is never overwritten. use_dynamic, enabled,
-- evaluation_period and every other column are left alone. Pseudo / ephemeral mounts (snap, run, dev, loop, proc, sys, tmp:
-- the list in app/threshold_coverage.py) are skipped; they are not actionable and sit at 100% by design.
--
-- Idempotent (a second run matches nothing). Nothing here is MySQL-8-only.
UPDATE thresholds t
JOIN metric_catalog mc ON mc.id = t.metric_id
SET t.warning_value = 80, t.critical_value = 90
WHERE t.warning_value = 1000000 AND t.critical_value = 5000000 AND t.comparison = '>'
  AND (   mc.metric_name = 'mem_used_percent'
       OR mc.metric_name = 'disk_used_percent'
       OR (    mc.metric_name LIKE 'disk\_used\_percent\_\_%'
           AND mc.metric_name NOT LIKE 'disk\_used\_percent\_\_snap%'
           AND mc.metric_name NOT LIKE 'disk\_used\_percent\_\_run%'
           AND mc.metric_name NOT LIKE 'disk\_used\_percent\_\_dev%'
           AND mc.metric_name NOT LIKE 'disk\_used\_percent\_\_loop%'
           AND mc.metric_name NOT LIKE 'disk\_used\_percent\_\_proc%'
           AND mc.metric_name NOT LIKE 'disk\_used\_percent\_\_sys%'
           AND mc.metric_name NOT LIKE 'disk\_used\_percent\_\_tmp%'));
