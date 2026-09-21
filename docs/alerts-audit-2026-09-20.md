# Alerts audit — 2026-09-20

## One definition of an alert (app/alert_rules.py)
Every open alert is exactly one derived state; only `firing` is ever counted as
critical/warning, on every screen:

| state | meaning |
|---|---|
| firing | active, fresh reading, not muted, not in a maintenance window |
| stale | active but no fresh reading inside its collection-tier window (core 20 min, extended 3 h, daily tier 50 h) |
| suppressed | muted (`muted_until` in the future) or maintenance-silenced |
| acknowledged | a human took ownership |
| resolved | closed (see `resolution_reason`) |

Unit is **alert rows** everywhere. Sources of truth:
`GET /api/alerts/counts` (tab badges), `GET /api/alerts?tab=` (tab lists),
`GET /api/alerts/summary` (Overview/Services), `GET /api/alerts/by-resource` (row badges).

## Behaviour changes
* Placeholder thresholds (1,000,000 / 5,000,000 / `>`) are **anomaly-only**: need a confident baseline, a sustained breach, well above the resource's own norm, never above WARNING. Editing either number in Settings makes it an ordinary static threshold.
* Dynamic thresholds may relax a static threshold freely but tighten it by at most 50 %; percentage bands cap at 99.9.
* Acknowledged alerts are evaluated (resolve on recovery, no duplicates, re-open on escalation to CRITICAL).
* Auto-resolve reasons: `recovered`, `manual`, `bulk_clear`, `threshold_disabled`, `resource_gone`, `account_inactive`, `instance_stopped`, `no_data_expired`, `placeholder_threshold`, `duplicate`, `anomaly_cleared`, `check_deleted`.
* Mute is enforced (was write-only). Clear resolves instead of deleting. Ack cannot resurrect a resolved alert.
* Settings "Check Now" is a read-only preview; the scheduled evaluator is the only writer of threshold alerts.
* Every alert writer sets `aws_account_id` (synthetic checks and the isolation-forest detector did not — their alerts were invisible).
* Public status page, escalation, SLO, health score and maintenance silencing use the same firing definition and are account-scoped. Health scores are recomputed after every evaluation.
* WebSocket pushes are filtered per account.

## Deploy (dev, then prod)
1. Read-only preview on prod: `db/diagnostics/alerts_audit_preview.sql` (see file header for the command).
2. Pull, then **migrate before restarting** (051 is additive/idempotent; the new code writes its columns):
   `migrate.py status` → `migrate.py apply --all-pending` → `sudo systemctl restart monitoring-hub`
3. Frontend: `cd frontend && npm install --silent && npm run build`
4. Verify (should be equal): Overview banner critical/warning = Alerts page "Critical" badge = sum of Services tiles.
   `SELECT resolution_reason, COUNT(*) FROM alerts WHERE resolved_at > UTC_TIMESTAMP() - INTERVAL 1 HOUR GROUP BY 1;`

## Known limits
* Auto-tune only ever switches static → dynamic (no automatic revert); the new guard rails bound the harm.
* Reports, NL search, deploy-risk, correlation and RCA still use their own "active" filters.
* The evaluator assumes the MySQL session time zone is UTC (preview query 1 shows it).
