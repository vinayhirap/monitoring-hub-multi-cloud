-- 083_waf_blocked_requests_anomaly_only.sql
--
-- Phase 3 (AWS WAFv2). BlockedRequests used to default to a STATIC warning 1 / critical 5 ('>'): six blocked requests in a
-- period was a CRITICAL alert, which on an internet-facing WebACL is routine scanner noise. The default is now the
-- anomaly-only placeholder (1000000 / 5000000 / '>', see app/threshold_defaults.py PLACEHOLDER_THRESHOLD): the evaluator
-- alerts only on a sustained, confident spike vs the ACL's own baseline, above the absolute floor, and never above WARNING.
--
-- This moves ONLY rows still carrying the old untouched default (1 / 5 / '>', static). A row anyone edited (different
-- numbers, or use_dynamic switched on) is left exactly as it is. Idempotent: after one run nothing matches any more.
-- No schema change. Open CRITICAL BlockedRequests alerts de-escalate on the next evaluation (alert_evaluator.py,
-- "DE-ESCALATION") or resolve if the reading is not an anomaly.

UPDATE thresholds t
  JOIN metric_catalog mc ON mc.id = t.metric_id
   SET t.warning_value  = 1000000,
       t.critical_value = 5000000
 WHERE mc.provider    = 'aws'
   AND mc.service     = 'wafv2'
   AND mc.metric_name = 'BlockedRequests'
   AND t.warning_value  = 1
   AND t.critical_value = 5
   AND t.comparison     = '>'
   AND COALESCE(t.use_dynamic, 0) = 0;
