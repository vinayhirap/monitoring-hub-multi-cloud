# Environment-based polling: CloudWatch cost estimate (Phase 4c)

Status: the model, the report script and the numbers below are built and tested against a scratch
MySQL. **No number here was measured on prod.** The prod rows are marked PENDING until the two
queries in section 6 are run on prod.

## 1. What changes (flag `ENV_POLLING_ENABLED`, default off)

| Environment | Critical-class metrics (the P1 set) | Everything else |
|---|---|---|
| prod, unknown, ignore, unclassified | unchanged | unchanged |
| uat | unchanged (2 min) | base interval x `POLL_UAT_MULTIPLIER` (default 3) |
| dev | every `POLL_DEV_CRITICAL_INTERVAL_SECONDS` (default 600) | every `POLL_DEV_INTERVAL_SECONDS` (default 3600), or not collected at all with `POLL_DEV_NONCRITICAL_MODE=off` |

Never slowed whatever the environment: wafv2, any metric whose base interval is 24 h or more, the free Describe-API
metrics (EC2 status check, ELB healthy hosts), and non-AWS providers.

A slowed resource is skipped on the ticks where it is not due, and on the tick where it is due the
GetMetricData window is widened by the skipped time. CloudWatch bills per **metric requested**, not per
datapoint returned, and the query Period is unchanged, so charts keep the same number of points and the
dynamic baselines see the same density. What gets worse is freshness (the newest point is up to one slowed
interval later), and the alert engine is told so (section 4).

## 2. Formula

For one group (a resource type in a region, on one tier, in one environment class):

```
collections/day = 86400 / effective_interval
calls/collection = number of GetMetricData requests
                 = ceil(plain queries / 500) + ceil(SEARCH queries / 5)
metrics/collection (billed unit) = queries sent
calls/day   = calls/collection   x collections/day
metrics/day = metrics/collection x collections/day
```

`effective_interval` is `base x 3` for uat non-critical, `max(base, 3600)` for dev non-critical, and so on
(section 1). Prod-like groups keep `base`. `scripts/polling_plan_report.py` evaluates exactly this by running the
real selection and the real query builders with the CloudWatch call replaced by a recorder (so it cannot drift
from the collectors) and splitting groups the way the collector does (one request group per look-back).

**Today's baseline** (flag off) puts every resource of a type/region into the same requests, so the baseline call
count is that of the whole group, shared out to the environments by billed metrics.

## 3. Assumptions and limits

- EC2 CWAgent memory and disk: one metric per running instance each (reported on their own `(assumed)` rows).
  Real counts depend on which instances run the agent and how many mounts they report.
- Billing unit: a MetricStat query is one metric. A SEARCH expression is billed per metric it matches, counted
  as one here (a floor).
- GetMetricData pages (more than 100,800 datapoints) are not modelled; calls are a floor.
- Steady state: every tier runs on schedule, no restarts. After a restart every slowed resource is collected once.
- **Billed metrics fall, request count can rise.** Slowed resources are requested in their own groups (wider
  window), so the number of GetMetricData requests can go UP while the number of billed metrics goes DOWN.
  AWS bills the metrics, not the requests; judge the saving on billed metrics.
- Look at the current per-metric GetMetricData rate in AWS CloudWatch pricing and multiply. No price is assumed.
- Free tier and Azure/GCP are not part of this estimate (Azure and GCP are not slowed).

## 4. How alerting stays correct for a slowed resource

All of this is behind the same flag and does nothing for prod-like resources.

- **Evaluation window and staleness limit** (`alert_rules.eval_window_sql`, `stale_minutes_sql`) grow by
  `(effective - base)` minutes for that resource's environment and metric. A UAT EC2 CPU alert (polled every 15 min)
  goes stale after 30 min instead of 20, so it does not show "no data" between polls; a real outage still does.
- **Required cycles** (`alert_evaluator._evaluate_row`) are multiplied by the slow-down (UAT standard: x3). The
  evaluator re-reads the cached last value every 5 minutes; without this, one slowed reading would count as three
  cycles and open an alert from a single datapoint. This is also the longer "alert for" duration for UAT. Resolving
  needs the same multiple of healthy cycles.
- **DEV** evaluates critical-class metrics only (`POLL_DEV_EVAL_CRITICAL_ONLY`, default true). An alert already open
  on a DEV non-critical metric stops being refreshed, turns stale, and is closed by the existing 72 h no-data expiry.
- **UAT critical-class metrics are not slowed.** UAT CRITICAL is P2 in the 4b matrix, so the P1-set metrics
  (RDS CPU/connections/memory, ALB 5xx/requests) keep prod cadence on UAT. Their detection latency does not change.
- **Baselines** (`metric_baseline`, STL): history density is unchanged because the window is widened. One exception:
  a Sum metric with no datapoints stores a single zero for the newest bucket only, so a slowed, idle Sum metric has
  gaps in history instead of zeros between polls. Dynamic baselines read non-zero history, so this is cosmetic on charts.

## 5. Illustrative result (MODELLED, not measured)

Fleet shaped like the 2026-10-13 prod snapshot (310 resources: 40 EC2, 70 EBS, 45 S3, 102 log groups, 15 KMS,
11 event rules, 5 ACM, 3 RDS, 3 ELB, 8 Lambda, 6 SQS, 2 NAT) with the extended metrics that are on by default,
labels assigned at random in the stated shares (uat:dev = 2:1), defaults for all other settings.

| Non-prod share of resources | Billed metrics/day, off -> on | Saved | GetMetricData requests/day, off -> on |
|---|---|---|---|
| 0% | 123,299 -> 123,299 | 0.0% | 6,150 -> 6,150 |
| 10% | 123,299 -> 116,651 | 5.4% | 6,150 -> 6,756 |
| 25% | 123,299 -> 106,291 | 13.8% | 6,150 -> 7,471 |
| 40% | 123,299 -> 91,003 | 26.2% | 6,150 -> 7,534 |
| 60% | 123,299 -> 78,427 | 36.4% | 6,150 -> 6,398 |

At 30% non-prod: 123,299 -> 99,883 billed metrics/day with DEV non-critical slowed to hourly, 97,915 with
`POLL_DEV_NONCRITICAL_MODE=off` (a further 1.6 points: the daily and hourly tiers are already cheap).

The saving is almost entirely the 5-minute tier (EC2/EBS/Lambda/SQS), because that is where the billed metrics are.

## 6. What to run on PROD (read only), then fill the table

Scenario A, labels as stored today; scenario B, account-default labels cleared. B is the **safe lower bound** of
the savings: 206 of 331 resources get their label only from the account default today, and the true
non-prod share is unknown until real tags exist. A saving that depends on those defaults being right should not be
counted on.

PROD, in `/opt/monitoring-hub/app`:

```
mysql -u"$(grep -m1 '^DB_USER=' .env | cut -d= -f2-)" \
  -p"$(grep -m1 '^DB_PASSWORD=' .env | cut -d= -f2-)" \
  -h"$(grep -m1 '^DB_HOST=' .env | cut -d= -f2-)" \
  "$(grep -m1 '^DB_NAME=' .env | cut -d= -f2-)" \
  -e "SELECT COALESCE(env_resolved,'unclassified') AS env, resource_type, monitoring_tier, COUNT(*) AS n FROM resources WHERE (instance_state IS NULL OR instance_state <> 'terminated') AND resource_type <> 'eni' GROUP BY 1,2,3 ORDER BY n DESC LIMIT 60;"
```

```
/opt/monitoring-hub/venv/bin/python3 scripts/polling_plan_report.py
/opt/monitoring-hub/venv/bin/python3 scripts/polling_plan_report.py --scenario defaults-cleared
```

Measured "before" (calibration of the model), PROD:

```
mysql ... -e "SELECT JSON_UNQUOTE(detail->'$.tier') t, SUM(detail->'$.calls') calls, SUM(detail->'$.units') units FROM op_events WHERE event_type = 'metric_api_usage' AND JSON_UNQUOTE(detail->'$.provider') = 'aws' AND created_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY GROUP BY t;"
```

(Run it in the standard mysql wrapper.) Compare `units` per tier with the report's flag-off column. A large gap
means the CWAgent assumption or the extended selection differs from reality; send both and the model is corrected.

| Scenario | Billed metrics/day off | on | Saved | Status |
|---|---|---|---|---|
| A. labels as stored | PENDING | PENDING | PENDING | waiting for the prod report |
| B. defaults cleared (lower bound) | PENDING | PENDING | PENDING | waiting for the prod report |

## 7. Not built

DEV off-hours quiet. It needs an account time zone and a configurable window. Proposal: `aws_accounts.timezone`
(IANA name) and `aws_accounts.dev_quiet_start` / `dev_quiet_end` (local `HH:MM`), read in `env_polling.effective_interval`
for DEV only, behind its own flag `ENV_POLLING_DEV_QUIET_ENABLED`, with the window treated as "not due" and the first
poll after it widening the look-back like any other slowed poll. It would need migration 092.
