# Rollout runbook: environment classification, priority, routing

Flags (all default OFF, read from `.env` at call time, so edit + restart is the whole change):

| Flag | Changes |
|---|---|
| `ENV_AWARE_CLASSIFICATION` | environment label and WARNING/INFO severity of NEWLY raised alerts use the classification (name/tag/override/account default). CRITICAL is unchanged; unknown and ignore stay prod-like. |
| `PRIORITY_ENABLED` | P1-P4 stored and shown on alerts. (phase 4b patch 1, migration 101) |
| `PRIORITY_ROUTING_ENABLED` | channel `min_priority`/digest and priority escalation policies apply. (phase 4b) |
| `DIGEST_INTERVAL_MINUTES` | digest flush interval for channels set to `digest`. (phase 4b) |

Sections for the 4b flags are written from the design notes and were NOT checked against code; re-read them
against `docs/PRIORITY_AND_ROUTING.md` once patch 0014 is on GitHub. Always do DEV first, then PROD. One flag per
step, with at least a full business day between PROD steps.

> **WARNING (PROD).** PROD labels today depend mostly on ACCOUNT DEFAULTS (about 206 of 331 resources), only 1 resource
> has a tag and none has an `Environment` tag. Turning on `ENV_AWARE_CLASSIFICATION` downgrades warning-level alerts
> to INFO on every resource the defaults (or a name token such as `dev`/`test`) call non-prod. If a default
> mislabels a real production resource, its warnings become INFO. Review the report and the defaults first.

SQL below uses the standard command, run in `/opt/monitoring-hub/app`:

```
mysql -u"$(grep -m1 '^DB_USER=' .env | cut -d= -f2-)" \
  -p"$(grep -m1 '^DB_PASSWORD=' .env | cut -d= -f2-)" \
  -h"$(grep -m1 '^DB_HOST=' .env | cut -d= -f2-)" \
  "$(grep -m1 '^DB_NAME=' .env | cut -d= -f2-)" \
  -e "YOUR SQL HERE;"
```

## 0. Preconditions (each box, read-only)

DEV and PROD, separately:

```
cd /opt/monitoring-hub/app && git log --oneline -1 && /opt/monitoring-hub/venv/bin/python3 migrate.py status | grep -i pending
```
Expect: the commit you deployed and no pending migrations (083 and 086 applied).

```
mysql ... -e "SELECT env_source, env_resolved, COUNT(*) n FROM resources WHERE last_seen_at > UTC_TIMESTAMP() - INTERVAL 48 HOUR GROUP BY env_source, env_resolved ORDER BY n DESC;"
```
Expect: PROD about 331 rows spread over account/name/related/tag/unknown. Work the Environments page "Needs classification" queue down, set account defaults deliberately, then re-run:
`/opt/monitoring-hub/venv/bin/python3 scripts/backfill_environments.py` (dry run; expect `rows that change : 0` once synced).

Impact report (read-only, safe on PROD):
```
/opt/monitoring-hub/venv/bin/python3 scripts/flag_impact_report.py --csv /tmp/flag_impact.csv
```
Read: "severity would differ", the by-account/environment tables and the 20 most affected resources. Every
WARNING->INFO line must be a resource you really consider non-prod. The 4b sections say NOT AVAILABLE until patch 0014 is deployed.

Baseline of open alerts (save the output):
```
mysql ... -e "SELECT severity, environment, COUNT(*) n FROM alerts WHERE status IN ('active','acknowledged') GROUP BY severity, environment ORDER BY n DESC;"
```

## 1. ENV_AWARE_CLASSIFICATION

DEV, then (after sign-off) PROD, same commands on each:
```
cd /opt/monitoring-hub/app && grep -n '^ENV_AWARE_CLASSIFICATION' .env
```
Expect no output. Then:
```
echo 'ENV_AWARE_CLASSIFICATION=true' >> .env && sudo systemctl restart monitoring-hub
systemctl status monitoring-hub | grep -i active
```
Check again a minute later. Verify:
```
journalctl -u monitoring-hub --since "10 min ago" | grep -i -E "error|traceback|Alert evaluation complete"
mysql ... -e "SELECT severity, environment, COUNT(*) n FROM alerts WHERE triggered_at > UTC_TIMESTAMP() - INTERVAL 30 MINUTE GROUP BY severity, environment;"
```
Expect: no new errors; new alerts carry `prod`/`uat`/`dev`/`unknown`; `unknown` alerts are WARNING, never INFO.
Open alerts are NOT relabelled in place at the current tip; they keep old values until they resolve and re-fire.

Success: no evaluator errors for 24 h; every new INFO alert is on a resource you consider non-prod; no real incident was
demoted (check the Alerts page filtered by INFO). Rollback: `sed -i '/^ENV_AWARE_CLASSIFICATION=/d' .env && sudo systemctl restart monitoring-hub`. Data stays; alerts raised meanwhile keep their labels.

## 2. PRIORITY_ENABLED (needs phase 4b patch 1, migration 101)

Same pattern: confirm absent with `grep -n '^PRIORITY_ENABLED' .env`, append `PRIORITY_ENABLED=true`, restart, verify
`SELECT priority, COUNT(*) FROM alerts WHERE status IN ('active','acknowledged') GROUP BY priority;` (expect values 1-4, none NULL after the evaluator's next cycle; run `scripts/backfill_alert_priority.py` dry-run first). Watch the Alerts page P-chips and the Overview "Firing by priority" strip. Rollback as above.

## 3. PRIORITY_ROUTING_ENABLED (needs patch 0014)

Precondition: channels' `min_priority`/`delivery` reviewed and priority escalation policies created. Append
`PRIORITY_ROUTING_ENABLED=true`, restart. Watch `journalctl -u monitoring-hub --since "30 min ago" | grep -i -E "notif|escalat|digest"`
and send one test alert per channel. Success: P1/P2 reach the expected channels immediately, digest channels receive one
batch per interval. Rollback: remove the line, restart. Set `DIGEST_INTERVAL_MINUTES` only after this step is stable.
