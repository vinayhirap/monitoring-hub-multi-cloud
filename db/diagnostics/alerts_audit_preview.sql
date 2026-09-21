-- db/diagnostics/alerts_audit_preview.sql
-- READ-ONLY. Run BEFORE deploying the 2026-09-20 alerts audit to see exactly
-- what the first evaluation cycles will change. Changes nothing.
--
--   mysql -u"$(grep -m1 '^DB_USER=' .env | cut -d= -f2-)" \
--     -p"$(grep -m1 '^DB_PASSWORD=' .env | cut -d= -f2-)" \
--     -h"$(grep -m1 '^DB_HOST=' .env | cut -d= -f2-)" \
--     "$(grep -m1 '^DB_NAME=' .env | cut -d= -f2-)" < db/diagnostics/alerts_audit_preview.sql

SELECT '1. time zone (evaluator/rules assume the DB session is UTC)' AS section;
SELECT @@global.time_zone AS global_tz, @@session.time_zone AS session_tz, NOW() AS now_, UTC_TIMESTAMP() AS utc_now;

SELECT '2. alerts by status' AS section;
SELECT status, COUNT(*) AS n FROM alerts GROUP BY status;

SELECT '3. alerts with NULL aws_account_id (invisible today; 051 attributes the unambiguous ones)' AS section;
SELECT metric_name, status, COUNT(*) AS n FROM alerts WHERE aws_account_id IS NULL GROUP BY metric_name, status;

SELECT '4. thresholds still carrying the 1,000,000 / 5,000,000 placeholder (become anomaly-only)' AS section;
SELECT t.resource_type, mc.metric_name, COUNT(*) AS thresholds
FROM thresholds t JOIN metric_catalog mc ON mc.id = t.metric_id
WHERE t.warning_value = 1000000 AND t.critical_value = 5000000 AND t.comparison = '>'
GROUP BY t.resource_type, mc.metric_name ORDER BY thresholds DESC LIMIT 40;

SELECT '5. OPEN alerts on placeholder thresholds (these will resolve as placeholder_threshold)' AS section;
SELECT r.resource_type, a.metric_name, UPPER(a.severity) AS severity, COUNT(*) AS open_alerts
FROM alerts a
JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
JOIN thresholds t ON t.aws_account_id = a.aws_account_id AND t.resource_type = r.resource_type AND t.enabled = 1
JOIN metric_catalog mc ON mc.id = t.metric_id AND mc.metric_name = a.metric_name
WHERE a.status IN ('active','acknowledged')
  AND t.warning_value = 1000000 AND t.critical_value = 5000000 AND t.comparison = '>'
GROUP BY r.resource_type, a.metric_name, UPPER(a.severity) ORDER BY open_alerts DESC;

SELECT '6. OPEN alerts with no enabled threshold (will resolve as threshold_disabled after 30 min unconfirmed)' AS section;
SELECT r.resource_type, a.metric_name, COUNT(*) AS open_alerts
FROM alerts a
JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
WHERE a.status IN ('active','acknowledged')
  AND a.metric_name NOT IN ('multivariate_anomaly','synthetic_uptime')
  AND NOT EXISTS (SELECT 1 FROM thresholds t JOIN metric_catalog mc ON mc.id = t.metric_id
                  WHERE t.aws_account_id = a.aws_account_id AND t.resource_type = r.resource_type
                    AND t.enabled = 1 AND mc.metric_name = a.metric_name)
GROUP BY r.resource_type, a.metric_name ORDER BY open_alerts DESC;

SELECT '7. OPEN alerts whose resource discovery has not seen for >24h (resource_gone)' AS section;
SELECT r.resource_type, COUNT(*) AS open_alerts
FROM alerts a
JOIN resources r ON r.resource_id = a.resource_id AND r.aws_account_id = a.aws_account_id
JOIN aws_accounts aa ON aa.id = a.aws_account_id AND aa.provider = 'aws'
WHERE a.status IN ('active','acknowledged') AND r.last_seen_at IS NOT NULL
  AND r.last_seen_at < DATE_SUB(NOW(), INTERVAL 24 HOUR)
GROUP BY r.resource_type;

SELECT '8. OPEN alerts with no data for >72h (no_data_expired)' AS section;
SELECT COUNT(*) AS n FROM alerts
WHERE status IN ('active','acknowledged')
  AND COALESCE(last_seen_at, triggered_at) < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 72 HOUR);

SELECT '9. duplicate open alerts (same account+resource+metric) that 051 will collapse' AS section;
SELECT COUNT(*) AS groups_with_duplicates FROM (
  SELECT 1 FROM alerts WHERE status IN ('active','acknowledged') AND aws_account_id IS NOT NULL
  GROUP BY aws_account_id, resource_id, metric_name HAVING COUNT(*) > 1) d;

SELECT '10. resource_id shared by more than one account (the cross-account collision class)' AS section;
SELECT resource_id, COUNT(DISTINCT aws_account_id) AS accounts FROM resources
GROUP BY resource_id HAVING accounts > 1 LIMIT 20;
