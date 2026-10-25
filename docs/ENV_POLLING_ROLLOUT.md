# ENV_POLLING_ENABLED: turning it on and proving it is correct

Phase C. `ENV_POLLING_ENABLED` stays ON. This runbook turns it on, shows what to watch, and gives the exact
rollback. Nothing here changes collector behaviour; the code was already in the tree (Phase 4c).

> **Two-environment model.** The app now has exactly two environments, PROD and NON-PROD (migration
> `123_two_environment_model.sql`). The former UAT multiplier is gone: `POLL_UAT_MULTIPLIER` is REMOVED and
> **former UAT / SIT / QA resources now follow the NON-PROD policy**. This is a behaviour change for them: their
> critical-class metrics were unchanged and their other metrics ran at 3x the base interval; they now poll
> critical-class metrics at least every 10 minutes and everything else at least hourly (or not at all with
> `POLL_NONPROD_NONCRITICAL_MODE=off`), and they stop evaluating non-critical alerts. Apply migration 123 before
> turning this flag on, and tell the owners of former UAT/SIT resources. Measured figures in sections 2, 6 and 7 were
> taken on 2026-10-17 to 2026-10-22 under the old prod/uat/dev/unknown model and are kept as the baseline; re-run
> `scripts/polling_plan_report.py` after migrating for the NON-PROD numbers.

**State today:** `ENV_POLLING_ENABLED` is UNSET on PROD** (`ENV_AWARE_CLASSIFICATION=true` is set). Env polling is
therefore OFF there, so **this is the first time it is turned on in PROD**. Older docs that say "built, off, decided
not to enable" (`POLLING_COST_ESTIMATE.md` section 6, `POLLING_AUDIT_2026-10-19.md` sections 1 and 3) describe the
earlier decision, which this phase reverses. Those two files still need a one-line update (not done here).

Claim labels used below: **[code]** read in the source, **[test]** proved by a test in this patch,
**[unverified]** not provable from the sandbox; it is what the PROD run must show.

## 1. What the flag changes

The flag takes effect only when `ENV_POLLING_ENABLED` AND `ENV_AWARE_CLASSIFICATION` are both true [code, test].
With either off every resource is polled exactly as before [test: identity over all 218 AWS metrics, 9 env values].

| Environment of the resource | Critical-class metric (2 min tier, P1 set, Describe metrics) | Any other metric |
|---|---|---|
| PROD, and anything unplaced (fail-safe) | unchanged | unchanged |
| NON-PROD (dev, test, SIT, UAT, QA, staging, preprod, sandbox) | at least every 10 min (`POLL_NONPROD_CRITICAL_INTERVAL_SECONDS`=600) | at least every 60 min (`POLL_NONPROD_INTERVAL_SECONDS`=3600); not collected if `POLL_NONPROD_NONCRITICAL_MODE=off` |

"At least" means never faster than the metric's own base interval: a metric already polled every 15 min stays at 15
min if the NON-PROD setting is shorter. The `POLL_DEV_*` names are still read as fallbacks when the matching
`POLL_NONPROD_*` name is unset; `POLL_UAT_MULTIPLIER` is ignored.

Never slowed, whatever the settings [test]: wafv2, metrics already on a 24 h or slower tier, the free Describe-API
metrics (EC2 status checks, ALB healthy hosts), Azure and GCP, and anything never faster than its base.
Hostile values (`0`, `-5`, `99`, `abc`) are clamped or ignored, never faster than base [test].

Do not set any `POLL_*` variable on PROD. The defaults are the tested choice.

Side effects on alerts [code]:
- The evaluation window and the "no data" limit of NON-PROD alerts grow with the interval, so an alert does not
  flip to Stale between its polls. Slowed NON-PROD metrics need proportionally more cycles to open or resolve an alert
  (an hourly metric needs 12 five-minute cycles' worth, so one reading is never counted several times).
- NON-PROD non-critical metrics are no longer evaluated (`POLL_NONPROD_EVAL_CRITICAL_ONLY=true`). An alert already open on one
  is not refreshed: it shows Stale and is closed as `no_data_expired` 72 h after it was last seen (168 h for the
  daily class). It does not resolve by recovery. The verify script lists these before you turn the flag on (section 4).
- A resource's first poll after a service restart or leader handover is immediate (staggered); later polls widen the
  look-back so no gap is left. [test: simulated 24 h, a restart adds at most one extra collection per slowed resource.]
  The in-memory schedule state is lost on restart: for about one slowed interval a restart can cost one extra poll. [unverified on PROD]

## 2. Fleet and expected effect (PROD, measured 2026-10-17, flag off, old model)

Historical, old four-label model. 334 resources: prod 89, uat 10, dev 26, unknown 209. Under the two-environment model
the 36 uat+dev resources are NON-PROD and the 209 unknown are PROD (never slowed) until classified. 22 of the unknown are on the 5-minute tier
(10 EC2, 12 EBS), see section 7.

| | Billed metrics/day | GetMetricData requests/day |
|---|---|---|
| measured before (critical 10,800; standard 50,688; low 19,095; extended 744; slow_extended 366) | 81,693 | 7,365 |
| model, flag off | 77,110 | 6,037 |
| model, flag on (old model: dev hourly, uat x3) | 65,158 (-15.5%: dev -85.8%, uat -44.4%, prod-like 0%) | 7,476 (+24%) |
| model, flag on (two-environment model) | not yet regenerated: run `scripts/polling_plan_report.py` after migration 123. Former uat now saves more than the -44.4% above because it follows the hourly NON-PROD cadence. | |

CloudWatch bills per metric requested, not per request, so billed metrics fall while the request count rises.
Saving is about 358,560 metrics a month, about $3.6 at $0.01 per 1,000 (US East list; **confirm the ap-south-1 rate
on the bill**, nothing here is a quote). Expectation, not a promise: the model is 6% under the measurement.

## 3. DEV server: check the logic (no PROD access, no paid collection)

On DEV leave the paid collection as it is. These three are read-only or offline:

```
cd /opt/monitoring-hub/app
/opt/monitoring-hub/venv/bin/python3 scripts/env_polling_verify.py --config-only
/opt/monitoring-hub/venv/bin/python3 -m pytest tests/test_env_polling_rollout.py tests/test_env_polling.py -q
/opt/monitoring-hub/venv/bin/python3 -c "from dotenv import load_dotenv; load_dotenv('.env'); import runpy, sys; sys.argv=['scripts/polling_plan_report.py']; runpy.run_path('scripts/polling_plan_report.py', run_name='__main__')"
```
Expect: `OK: no rule violated`, all tests passed, and a plan whose flag-on column is below flag-off.
`polling_plan_report.py` does not read `.env` itself (it fails with `DB_PASSWORD is not set`), so it is started through the
`load_dotenv` wrapper above; `env_polling_verify.py` and `flag_impact_report.py` load `.env` on their own.
Do not set `ENV_POLLING_ENABLED` on the DEV server just to test: it would then call CloudWatch on the new schedule, and the
point of Phase C is to verify it, not to spend on it.

## 4. PROD (prod): before you turn it on

Run each, save the output. All read-only.

1. Deployed commit and migrations (083, 086 and 123 must be applied; 123 is the two-environment conversion):
```
cd /opt/monitoring-hub/app && git log --oneline -1 && /opt/monitoring-hub/venv/bin/python3 migrate.py status | grep -i pending
```
2. The flag really is unset:
```
cd /opt/monitoring-hub/app && grep -E '^(ENV_POLLING_ENABLED|ENV_AWARE_CLASSIFICATION|POLL_)' .env
```
Expect `ENV_AWARE_CLASSIFICATION=true` and nothing else. Any `POLL_*` line: stop and read it first.
3. Baseline, 7 days before today, per tier (calls and billed metrics per day):
```
mysql -u"$(grep -m1 '^DB_USER=' .env | cut -d= -f2-)" -p"$(grep -m1 '^DB_PASSWORD=' .env | cut -d= -f2-)" -h"$(grep -m1 '^DB_HOST=' .env | cut -d= -f2-)" "$(grep -m1 '^DB_NAME=' .env | cut -d= -f2-)" -e "SELECT JSON_UNQUOTE(detail->'$.tier') tier, ROUND(SUM(detail->'$.calls')/7) calls_per_day, ROUND(SUM(detail->'$.units')/7) billed_per_day FROM op_events WHERE event_type='metric_api_usage' AND JSON_UNQUOTE(detail->'$.provider')='aws' AND created_at >= UTC_TIMESTAMP() - INTERVAL 7 DAY AND created_at < UTC_TIMESTAMP() - INTERVAL 1 DAY GROUP BY tier ORDER BY tier;"
```
4. What the scheduler and logs look like now:
```
mysql -u"$(grep -m1 '^DB_USER=' .env | cut -d= -f2-)" -p"$(grep -m1 '^DB_PASSWORD=' .env | cut -d= -f2-)" -h"$(grep -m1 '^DB_HOST=' .env | cut -d= -f2-)" "$(grep -m1 '^DB_NAME=' .env | cut -d= -f2-)" -e "SELECT event_type, COUNT(*) n FROM op_events WHERE created_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY AND severity IN ('ERROR','WARNING') GROUP BY event_type ORDER BY n DESC LIMIT 15;"
mysql -u"$(grep -m1 '^DB_USER=' .env | cut -d= -f2-)" -p"$(grep -m1 '^DB_PASSWORD=' .env | cut -d= -f2-)" -h"$(grep -m1 '^DB_HOST=' .env | cut -d= -f2-)" "$(grep -m1 '^DB_NAME=' .env | cut -d= -f2-)" -e "SELECT event_type, MAX(created_at) last_run FROM op_events WHERE event_type LIKE 'scheduler_tier_%_completed' GROUP BY event_type;"
mysql -u"$(grep -m1 '^DB_USER=' .env | cut -d= -f2-)" -p"$(grep -m1 '^DB_PASSWORD=' .env | cut -d= -f2-)" -h"$(grep -m1 '^DB_HOST=' .env | cut -d= -f2-)" "$(grep -m1 '^DB_NAME=' .env | cut -d= -f2-)" -e "SELECT environment, severity, status, COUNT(*) n FROM alerts WHERE status IN ('active','acknowledged') GROUP BY environment, severity, status ORDER BY environment, severity;"
```
5. Forecast for the alert side and a proof the flag is currently off:
```
cd /opt/monitoring-hub/app && /opt/monitoring-hub/venv/bin/python3 scripts/env_polling_verify.py --expect off --only config --only alerts
```
Expect `env polling is OFF`, `no rule violated`. Read every `FALSE_STALE` line: each is an open NON-PROD alert that will
stop being refreshed. Close or accept them before turning on. A `STALE_LIMIT_TIGHT` line is a FAIL: stop and report it.
6. The modelled after-numbers for your fleet:
```
cd /opt/monitoring-hub/app && /opt/monitoring-hub/venv/bin/python3 -c "from dotenv import load_dotenv; load_dotenv('.env'); import runpy, sys; sys.argv=['scripts/polling_plan_report.py']; runpy.run_path('scripts/polling_plan_report.py', run_name='__main__')"
```

## 5. PROD (prod): turn it on

Only after DEV section 3 passed and you confirm. You edit `.env` by hand:
```
cd /opt/monitoring-hub/app && cp -p .env .env.bak.$(date +%Y%m%d%H%M%S)
printf '\nENV_POLLING_ENABLED=true\n' >> .env
grep -E '^(ENV_POLLING_ENABLED|ENV_AWARE_CLASSIFICATION)=' .env
sudo systemctl restart monitoring-hub
systemctl show monitoring-hub -p ActiveEnterTimestamp --value
```
Write down the timestamp: it is the restart time used below. The grep must show each variable once.

## 6. PROD (prod): verify, then the 24 h before/after

Right after the restart (a minute or two):
```
journalctl -u monitoring-hub --since "5 min ago" | grep -i -E "env polling|error|traceback"
cd /opt/monitoring-hub/app && /opt/monitoring-hub/venv/bin/python3 scripts/env_polling_verify.py --expect on --only config
```
Expect `env polling is ON` and no traceback.

**After 4 hours** (the hourly NON-PROD cadence needs several cycles and the verdicts need uptime), with N = minutes
since the restart:
```
cd /opt/monitoring-hub/app && /opt/monitoring-hub/venv/bin/python3 scripts/env_polling_verify.py --expect on --uptime-min N
```
Verdicts per (resource type, environment, base tier) [code]: `OK`; `SLOWER` (older than promised: collection is late, FAIL);
`FASTER` (NON-PROD still polled at the old rate: the flag is not being applied, FAIL); `STILL_COLLECTED` (NON-PROD not-collected
mode only); `UNPROVEN` / `TOO_EARLY` / `FEW` (not enough information to judge, never a failure). A series that is old while the same
resource and the same group have fresh readings is listed under `NO RECENT DATAPOINT` and is not counted: CloudWatch had no value
(idle EBS volume without VolumeQueueLength, non-gp2 volume without BurstBalance). Found on PROD 2026-10-22: series up to 42 days old.
Run the cadence check no earlier than 4 hours after the restart; at 0 minutes nothing slowed can be judged. The age is the time
since the last collection, from `metrics.metric_timestamp`, which is the write time. The call count below is the definitive proof.

**After 24 h** compare with the section 4 baseline:
```
mysql -u"$(grep -m1 '^DB_USER=' .env | cut -d= -f2-)" -p"$(grep -m1 '^DB_PASSWORD=' .env | cut -d= -f2-)" -h"$(grep -m1 '^DB_HOST=' .env | cut -d= -f2-)" "$(grep -m1 '^DB_NAME=' .env | cut -d= -f2-)" -e "SELECT JSON_UNQUOTE(detail->'$.tier') tier, SUM(detail->'$.calls') calls, SUM(detail->'$.units') billed_metrics FROM op_events WHERE event_type='metric_api_usage' AND JSON_UNQUOTE(detail->'$.provider')='aws' AND created_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY GROUP BY tier ORDER BY tier;"
mysql -u"$(grep -m1 '^DB_USER=' .env | cut -d= -f2-)" -p"$(grep -m1 '^DB_PASSWORD=' .env | cut -d= -f2-)" -h"$(grep -m1 '^DB_HOST=' .env | cut -d= -f2-)" "$(grep -m1 '^DB_NAME=' .env | cut -d= -f2-)" -e "SELECT MIN(created_at) first_hour, COUNT(*) rows_24h FROM op_events WHERE event_type='metric_api_usage' AND created_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY;"
```
The second shows the window really covers 24 h of post-restart hours (an hour is written per provider and tier at most once an hour).

| Tier | Baseline (7 d average, measured) | Expected after | Source of the expectation |
|---|---|---|---|
| critical | 10,800 billed | about equal; NON-PROD critical-class only moves to 10 min | `polling_plan_report.py` |
| standard | 50,688 billed | down by the NON-PROD (hourly) share of the 5-min metrics | `polling_plan_report.py` |
| low | 19,095 billed | down by the NON-PROD share | `polling_plan_report.py` |
| extended | 744 billed | down for NON-PROD (hourly or equal) | `polling_plan_report.py` |
| slow_extended | 366 billed | unchanged (daily) | [code] |
| all tiers | 81,693 billed, 7,365 calls | about 15% fewer billed (old-model figure 65,158; expect a larger saving now that former UAT is hourly), calls up about 24% (7,476, old model) | section 2 |

Fill the "after" figures from step 6 of section 4 for your fleet, then compare. Acceptance: total billed metrics down
between 8% and 20% against the 7-day average, calls up by no more than 40%, PROD and unplaced resources unchanged,
verify script `no hard finding`. [unverified: the PROD numbers; the sandbox has no PROD data. The model and the
simulation are verified.]

Alert checks (same day and after 72 h):
```
mysql -u"$(grep -m1 '^DB_USER=' .env | cut -d= -f2-)" -p"$(grep -m1 '^DB_PASSWORD=' .env | cut -d= -f2-)" -h"$(grep -m1 '^DB_HOST=' .env | cut -d= -f2-)" "$(grep -m1 '^DB_NAME=' .env | cut -d= -f2-)" -e "SELECT environment, severity, status, COUNT(*) n FROM alerts WHERE status IN ('active','acknowledged') GROUP BY environment, severity, status ORDER BY environment, severity;"
mysql -u"$(grep -m1 '^DB_USER=' .env | cut -d= -f2-)" -p"$(grep -m1 '^DB_PASSWORD=' .env | cut -d= -f2-)" -h"$(grep -m1 '^DB_HOST=' .env | cut -d= -f2-)" "$(grep -m1 '^DB_NAME=' .env | cut -d= -f2-)" -e "SELECT DATE(resolved_at) day, COUNT(*) n FROM alerts WHERE resolution_reason='no_data_expired' AND resolved_at >= UTC_TIMESTAMP() - INTERVAL 7 DAY GROUP BY day ORDER BY day;"
mysql -u"$(grep -m1 '^DB_USER=' .env | cut -d= -f2-)" -p"$(grep -m1 '^DB_PASSWORD=' .env | cut -d= -f2-)" -h"$(grep -m1 '^DB_HOST=' .env | cut -d= -f2-)" "$(grep -m1 '^DB_NAME=' .env | cut -d= -f2-)" -e "SELECT event_type, COUNT(*) n FROM op_events WHERE created_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY AND severity IN ('ERROR','WARNING') GROUP BY event_type ORDER BY n DESC LIMIT 15;"
```
Expect: no new error event types versus section 4; NON-PROD alert counts not jumping; the `no_data_expired`
closures on NON-PROD non-critical alerts you listed in section 4 (and nothing else) as they pass 72 h.

### Found on PROD (2026-10-22) and fixed before turning on: NON-PROD certificate-expiry alert

`env_polling_verify.py` flagged one open alert: 11462, U4RAD, ACM certificate `test.u4rad.com`, classified NON-PROD (then labelled dev) by the name
token `test`, metric `daystoexpiry`. That metric is daily and never slowed, but the alert gate (`evaluate_for_alerts`)
only asked "NON-PROD and not critical-class", so with the flag ON the alert would stop being evaluated (Stale, then closed as
`no_data_expired`). Fix (patch pcC-0003): `evaluate_for_alerts(..., base_interval=)` returns True for any metric whose
base interval is 24 h or more; every sub-daily NON-PROD non-critical metric is still skipped. [test]
After the patch the verify script reports no `FALSE_STALE` for that alert.

## 7. The unplaced EC2/EBS on the 5-minute tier (22 on 2026-10-17, 15 on 2026-10-22)

On PROD 2026-10-22 the verifier listed 15 (12 EBS, 3 EC2), all in AuroGov Mumbai; the EBS volumes belong to 10 instances.
They are polled at the full rate and stay so until they are classified: an unplaced resource is treated as PROD,
on purpose (fail-safe), and sits in the Needs-classification queue. Classify them and the saving grows. List them (read-only, writes only the CSV you name):
```
cd /opt/monitoring-hub/app && /opt/monitoring-hub/venv/bin/python3 scripts/env_polling_verify.py --only unknown --csv-unknown /tmp/unknown_5min.csv
mysql -u"$(grep -m1 '^DB_USER=' .env | cut -d= -f2-)" -p"$(grep -m1 '^DB_PASSWORD=' .env | cut -d= -f2-)" -h"$(grep -m1 '^DB_HOST=' .env | cut -d= -f2-)" "$(grep -m1 '^DB_NAME=' .env | cut -d= -f2-)" -e "SELECT r.resource_type, COALESCE(a.account_name,'?') account, COUNT(*) n FROM resources r LEFT JOIN aws_accounts a ON a.id=r.aws_account_id WHERE r.resource_type IN ('ec2','ebs') AND (r.instance_state IS NULL OR r.instance_state <> 'terminated') AND (r.env_resolved IS NULL OR r.env_resolved IN ('unknown','')) GROUP BY 1,2 ORDER BY n DESC;"
```
For each row the script prints the account, type, name, state, what is missing, and what would be the interval if it
became NON-PROD (60 min for non-critical metrics). Rules the classifier uses [code]:

- A tag `Environment`, `Env`, `Stage`, `Deployment_Environment`, `AppEnv`, `Env_Type` (case-insensitive) with
  `prod|production|prd` (PROD) or `nonprod|dev|development|test|testing|sit|uat|qa|stage|staging|preprod|sandbox` (NON-PROD).
- Or a whole-token match in the NAME (`web-dev-01`). Names holding two different tier words, or none, stay unplaced (PROD, in the queue)
  (`DevOps-Jenkins` has no tier word).
- An EBS volume follows its attached EC2 (and shows that instance's tags). If the instance is unknown, tag the
  INSTANCE; the volumes then follow. A detached volume has no parent: tag it directly.
- Account default and a manual override (Environments page) also classify.
- The script's `suggestion` column (from sibling names, e.g. `payroll-batch-09` next to `payroll-batch-01..03` that are
  NON-PROD) is a hint, not a fact. Classify by what the owner says, never by the hint alone.

Caution: a wrong NON-PROD label slows a real production resource. When unsure, leave it in the queue (it stays PROD). Do not classify
anything because it would save money. [verified-by-test for the logic; which 22 they are: unverified until run on PROD]

## 8. Roll back

When to roll back (any one): the verify script shows `SLOWER` or a `STALE_LIMIT_TIGHT` FAIL; billed metrics did not fall
and calls rose more than 40%; a real incident was detected late on a NON-PROD resource; evaluator errors rose.
```
cd /opt/monitoring-hub/app && sed -i '/^ENV_POLLING_ENABLED=/d' .env
grep -c '^ENV_POLLING_ENABLED=' .env
sudo systemctl restart monitoring-hub
```
The grep must print `0`. Or restore the copy: `cp -p .env.bak.<timestamp> .env`, then restart. Data stays. The next poll
of every resource is at the base interval again [test: flag OFF identity]. NON-PROD alerts that went Stale in the meantime
are evaluated again and refresh themselves.

## 9. What this cannot prove

- Real CloudWatch behaviour and PROD billing: only the PROD run and the invoice can.
- Collector behaviour across a restart or leader handover is simulated, not run against AWS.
- The scratch-DB tests ran on MariaDB 10.11 (MySQL 8 is not available in the sandbox) with a collation substituted;
  the SQL used is plain SELECT.
- `scripts/report_stale_metrics.py --account` may have the same `%` formatting issue the verify script escapes for. [unverified]

## 10. Files

`scripts/env_polling_verify.py` (read-only: SELECTs in a read-only transaction, exit 0 no hard finding / 1 FAIL / 2 could not run),
`tests/test_env_polling_rollout.py`, `tests/test_env_polling_rollout_integration.py` (needs `MH_TEST_DB=1`),
this file.
