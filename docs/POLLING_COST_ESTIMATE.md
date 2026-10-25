# Environment-based polling: CloudWatch cost estimate (Phase 4c)

Status: built, tested, deployed to DEV and PROD with the flag OFF. Section 5 is a modelled illustration;
section 6 holds the real PROD numbers (taken 2026-10-17) and the decision.

> **Two-environment model.** The app now has only PROD and NON-PROD (migration `130_two_environment_model.sql`).
> Sections 1 to 4 describe the current policy. Sections 5 and 6 were produced under the old prod/uat/dev model
> (uat slowed x3, dev hourly) and are kept as dated, historical numbers: with `POLL_UAT_MULTIPLIER` removed, former UAT
> resources follow the hourly NON-PROD cadence, so the saving is larger than shown. Re-run
> `scripts/polling_plan_report.py` after migrating to refresh them. Behaviour change for former UAT/SIT resources: see
> `docs/ENV_POLLING_ROLLOUT.md`.

## 1. What changes (flag `ENV_POLLING_ENABLED`, default off)

| Environment | Critical-class metrics (the P1 set) | Everything else |
|---|---|---|
| PROD, and anything unplaced (fail-safe) | unchanged | unchanged |
| NON-PROD (dev, test, SIT, UAT, QA, staging, preprod, sandbox) | at least every `POLL_NONPROD_CRITICAL_INTERVAL_SECONDS` (default 600) | at least every `POLL_NONPROD_INTERVAL_SECONDS` (default 3600), or not collected at all with `POLL_NONPROD_NONCRITICAL_MODE=off` |

The `POLL_DEV_*` names are accepted as fallbacks for the `POLL_NONPROD_*` names; `POLL_UAT_MULTIPLIER` no longer exists.

Never slowed whatever the environment: wafv2, any metric whose base interval is 24 h or more, the free Describe-API
metrics (EC2 status check, ELB healthy hosts), and non-AWS providers.

A slowed resource is skipped on the ticks where it is not due, and on the tick where it is due the
GetMetricData window is widened by the skipped time. CloudWatch bills per **metric requested**, not per
datapoint returned, and the query Period is unchanged, so charts keep the same number of points and the
dynamic baselines see the same density. What gets worse is freshness (the newest point is up to one slowed
interval later), and the alert engine is told so (section 4).

## 2. Formula

For one group (a resource type in a region, on one tier, in one environment: PROD or NON-PROD):

```
collections/day = 86400 / effective_interval
calls/collection = number of GetMetricData requests
                 = ceil(plain queries / 500) + ceil(SEARCH queries / 5)
metrics/collection (billed unit) = queries sent
calls/day   = calls/collection   x collections/day
metrics/day = metrics/collection x collections/day
```

`effective_interval` is `max(base, 3600)` for NON-PROD non-critical, `max(base, 600)` for NON-PROD critical-class
(section 1). PROD groups keep `base`. `scripts/polling_plan_report.py` evaluates exactly this by running the
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
  `(effective - base)` minutes for that resource's environment and metric. A NON-PROD EC2 CPU alert (polled hourly)
  stays current for the longer interval, so it does not show "no data" between polls; a real outage still does.
- **Required cycles** (`alert_evaluator._evaluate_row`) are multiplied by the slow-down (an hourly NON-PROD metric: x12 of the 5-minute cycle). The
  evaluator re-reads the cached last value every 5 minutes; without this, one slowed reading would count as several
  cycles and open an alert from a single datapoint. This is also the longer "alert for" duration for NON-PROD. Resolving
  needs the same multiple of healthy cycles.
- **NON-PROD** evaluates critical-class metrics only (`POLL_NONPROD_EVAL_CRITICAL_ONLY`, default true; formerly Dev only). An alert already open
  on a NON-PROD non-critical metric stops being refreshed, turns stale, and is closed by the existing 72 h no-data expiry.
- **NON-PROD critical-class metrics are slowed only to 10 minutes.** NON-PROD CRITICAL is P3 in the 4b matrix, so the
  P1-set metrics (RDS CPU/connections/memory, ALB 5xx/requests) are collected at least every 10 minutes
  (`POLL_NONPROD_CRITICAL_INTERVAL_SECONDS`), never slower. Former UAT used to keep prod cadence for these; it no longer does.
- **Baselines** (`metric_baseline`, STL): history density is unchanged because the window is widened. One exception:
  a Sum metric with no datapoints stores a single zero for the newest bucket only, so a slowed, idle Sum metric has
  gaps in history instead of zeros between polls. Dynamic baselines read non-zero history, so this is cosmetic on charts.

## 5. Illustrative result (MODELLED, not measured; old three-label model)

Fleet shaped like the 2026-10-13 prod snapshot (310 resources: 40 EC2, 70 EBS, 45 S3, 102 log groups, 15 KMS,
11 event rules, 5 ACM, 3 RDS, 3 ELB, 8 Lambda, 6 SQS, 2 NAT) with the extended metrics that are on by default,
labels assigned at random in the stated shares (old model: uat:dev = 2:1; under the two-environment model all of these are NON-PROD and take the hourly cadence), defaults for all other settings.

| Non-prod share of resources | Billed metrics/day, off -> on | Saved | GetMetricData requests/day, off -> on |
|---|---|---|---|
| 0% | 123,299 -> 123,299 | 0.0% | 6,150 -> 6,150 |
| 10% | 123,299 -> 116,651 | 5.4% | 6,150 -> 6,756 |
| 25% | 123,299 -> 106,291 | 13.8% | 6,150 -> 7,471 |
| 40% | 123,299 -> 91,003 | 26.2% | 6,150 -> 7,534 |
| 60% | 123,299 -> 78,427 | 36.4% | 6,150 -> 6,398 |

At 30% non-prod: 123,299 -> 99,883 billed metrics/day with dev non-critical slowed to hourly, 97,915 with
`POLL_NONPROD_NONCRITICAL_MODE=off` (a further 1.6 points: the daily and hourly tiers are already cheap).

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

### PROD result (2026-10-17, flag off; old model)

Calibration, measured `metric_api_usage` over 24 h versus the report's flag-off column:

| | Billed metrics/day | GetMetricData requests/day |
|---|---|---|
| measured (critical 10,800, standard 50,688, low 19,095, extended 744, slow_extended 366) | 81,693 | 7,365 |
| model, flag off | 77,110 (6% under) | 6,037 (a floor) |

Fleet by environment (334 resources, old labels): prod 89, dev 26, uat 10, unknown/unclassified 209 (under the two-environment
model: NON-PROD 36, PROD 298 including the 209 unplaced). **Every uat and dev label
comes from a name or an attached parent; none from the account default**, so scenarios A and B are identical.
The 209 unplaced resources are PROD and never slowed. Only 22 of them (12 EBS, 10 EC2) are on the 5-minute tier.

| Scenario | Billed metrics/day off -> on | Saved | Requests/day off -> on |
|---|---|---|---|
| A. labels as stored (old model: uat x3) | 77,110 -> 65,158 | 15.5% (dev 85.8%, uat 44.4%, prod-like 0%) | 6,037 -> 7,476 (+24%) |
| B. defaults cleared (lower bound) | 77,110 -> 65,158 | 15.5% | 6,037 -> 7,476 |

Per 30 days: 2,313,300 -> 1,954,740 modelled billed metrics, a saving of about 358,560.
At the published GetMetricData list rate of $0.01 per 1,000 metrics requested (US East; confirm the ap-south-1 rate on
the bill) that is **about $3.6 a month, against a total GetMetricData cost of about $25 a month** (measured 81,693/day).
If a free monthly allowance applies to GetMetricData the saving is smaller still.

### Decision

Not enabled (decision taken under the old model; revisit with regenerated two-environment numbers). About $4 a month did not justify slower alerting on non-prod resources, non-critical NON-PROD metrics
no longer evaluated, and 24% more requests. The flag stays off; the code is tested and costs nothing while off.
Revisit if the fleet or the non-prod share grows (section 5 sensitivity), or after classifying the 22 unplaced
EC2/EBS resources. To find the real CloudWatch spend, group the bill by usage type in Cost Explorer and compare the
`APS1-CW:GMD-Metrics` line with custom metrics (CWAgent), logs ingestion and dashboards.

## 7. Not built

NON-PROD off-hours quiet. It needs an account time zone and a configurable window. Proposal: `aws_accounts.timezone`
(IANA name) and `aws_accounts.dev_quiet_start` / `dev_quiet_end` (local `HH:MM`), read in `env_polling.effective_interval`
for NON-PROD only, behind its own flag `ENV_POLLING_DEV_QUIET_ENABLED`, with the window treated as "not due" and the first
poll after it widening the look-back like any other slowed poll. It would need migration 092.
