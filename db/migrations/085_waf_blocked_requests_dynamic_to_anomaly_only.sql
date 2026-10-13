-- 085_waf_blocked_requests_dynamic_to_anomaly_only.sql
--
-- Phase 3 follow-up. 084 moved WAF BlockedRequests rows still on the old static default (1 / 5) to the anomaly-only
-- placeholder, and deliberately skipped rows with use_dynamic = 1. Those rows can still reach CRITICAL from a volume
-- reading alone, which is not wanted for WAF volume metrics. On PROD there were 2 such rows (use_dynamic = 1, 1 / 5).
--
-- This switches ONLY wafv2 BlockedRequests rows that are use_dynamic = 1 AND still carry the old default numbers (1 / 5, '>')
-- to the anomaly-only placeholder (1000000 / 5000000, use_dynamic = 0). A dynamic row whose numbers anyone changed is left
-- alone. Idempotent. No schema change.
UPDATE thresholds t
  JOIN metric_catalog mc ON mc.id = t.metric_id
   SET t.warning_value = 1000000, t.critical_value = 5000000, t.use_dynamic = 0
 WHERE mc.provider = 'aws' AND mc.service = 'wafv2' AND mc.metric_name = 'BlockedRequests'
   AND t.use_dynamic = 1 AND t.warning_value = 1 AND t.critical_value = 5 AND t.comparison = '>';
