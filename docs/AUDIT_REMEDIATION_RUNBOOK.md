# CloudOps audit remediation: runbook

Covers everything changed after the October 2026 audit (`CloudOps_Audit_Report.md`). Drop this into `docs/` in the repo.
State as of 4 Oct 2026: dev and prod run `origin/main` = `daaf475` or later; the round described in section 1b is applied on top.

## 1. What changed, by audit ID

| Area | IDs | Change |
|---|---|---|
| Data integrity | A2 | False `resource_id_column_width_drift` events stopped (only a column *narrower* than expected counts). Health-score recompute retries on MySQL 1213/1205, writes in key order, and deletes recovered rows by primary key. |
| Alert correctness | A3, A5, F1 | Alerts store the breaching reading (`breach_value`/`breach_threshold`). Availability metrics (HealthyHost, StatusCheckFailed, DaysToExpiry, failed backups/jobs, ...) are never auto-tuned to a learned band. Settings > Metric thresholds shows alert coverage and can apply recommended defaults. Per-mount disk thresholds are never blank. |
| Security | E2, E3, E7 | nginx security headers, CSP **enforced**, optional HSTS. Origin check on state-changing requests. External ID required for IAM-role onboarding. Console links must be http(s). Verified safe, no change: XSS (E6), SSRF guard on synthetic checks, forgot-password enumeration. |
| API quality | C2, C3, C5, C8, E8 | gzip, `/api/health/*`, stale-while-revalidate for `/api/live/accounts`, shared capacity-forecast cache, uniform error body with `request_id`, `X-Request-ID`, audit rows carry user agent + request id. |
| Operations | C9, D3, D9, G10 | Notification channels (Slack/Teams/webhook/email) with test button and delivery log. Daily retention for resolved alerts and the delivery log. Check Now 30 s cooldown per account. |
| Inventory | B4, F4 | Discovery registers **unattached EBS volumes** (counts agree with Overview) and raises a LOW finding for them. Metric polling skips them (no extra CloudWatch cost). |
| Findings | D7, B8 | One security finding per security group (worst severity, all open rules listed). Search, type filter, pagination. |
| Incidents / RCA | B3, F2, H1 | Incidents are named for and start at the earliest breach. "Why?" text states duration and recurrence for long-running alerts and gives `confidence_reason`. |
| UI | B1, B2, B6, B14, B15, C4, G1, G5, F5 | Page-not-found screen, one timestamp format, alert row overflow menus, stacked cards on phones, honest forecast rounding, ALB/NLB names, pollers pause in hidden tabs, pages load on demand (main bundle 1.19 MB to 0.49 MB), login video only on wide screens. |

### 1b. Later round

| Area | IDs | Change |
|---|---|---|
| Alerts API | D4 | `alerts.value` (legacy) and `current_value` were both returned. Migration **081** copies old values into `current_value`; `/api/alerts` no longer returns `value`. The legacy column stays in the table. `status`/`state` and `acked`/`silenced` are *not* duplicates (raw vs derived, flag vs timestamp) and are unchanged. |
| Authorization | E5 | `scripts/rbac_matrix.py` generates `docs/RBAC_MATRIX.md` from the code (every route and what protects it). A CI test pins the public routes, the state-changing routes open to any signed-in user, and every hand-written guard. `scripts/rbac_smoke.py` checks a live server (GET only; see section 6). `GET /ws/status` was unauthenticated and is now `operations.view`. |
| Removed | B9 | The Overview "Finish setting up CloudOps" card and `/api/setup/status` were removed on request (the monitoring team shares admin access). |
| Verified, no change | G9, B7 (drawer) | The collector already uses adaptive retries on every AWS client, 500-query GetMetricData batches, a parallel account pool and MySQL leader election. The alert drawer is already a full-height overlay (`min(560px, 100vw)`). |

## 2. New settings (all optional, defaults shown)

| Variable | Default | Effect |
|---|---|---|
| `CSRF_ORIGIN_CHECK` | `true` | Reject cross-origin POST/PUT/PATCH/DELETE. SAML ACS and `/api/webhooks/*` are exempt. |
| `REQUIRE_EXTERNAL_ID` | `true` | IAM-role onboarding and test-role need an External ID. `false` opts out. |
| `ALERT_RETENTION_DAYS` | `400` | Resolved alerts older than this are deleted daily. `0` disables. Values 1-89 are raised to 90. |
| `NOTIFICATION_LOG_RETENTION_DAYS` | `90` | Delivery-log retention. `0` disables. |

New permission: `notifications.manage` (admin by default). Migrations added: **077** (alert breach snapshot), **078** (audit log user agent/request id), **079** (audit_logs index), **080** (notification channels), **081** (copy legacy `alerts.value` into `current_value`). All are additive and idempotent; there are no down-migrations.

New endpoints: `/api/health/{live,ready,detail}`, `/api/notifications/channels` (+ `/{id}/test`, `/log`), `/api/settings/thresholds/{coverage,apply-defaults}`, `/api/admin/accounts/{id}/export`.

## 3. Deploying safely (lessons from the first rollout)

1. **One session writes to `main` at a time.** On 4 Oct a second session pushed `530f94c` between two of these deploys. Before any new work: `git pull origin main`, then `git log --oneline -3`.
2. **Never run `git am` for a patch that is already applied.** Check `git log --oneline -1` first. A failed `git am` leaves `.git/rebase-apply`; clear it with `git am --quit` (keeps HEAD) after confirming `git status --short` is empty.
3. **Chain steps with `&&`** so a failure stops the rest, and delete the patch file only after success:
   ```
   git am /tmp/FILE.mbox \
    && python3 -m py_compile $(git diff --name-only HEAD~N HEAD | grep '\.py$') \
    && (cd frontend && npm install --silent && npm run build) \
    && sudo systemctl restart monitoring-hub && sleep 15 \
    && systemctl status monitoring-hub | grep -i active
   ```
4. Migrations: `migrate.py status`, then `migrate.py apply --all-pending`, **before** restarting.
5. After any full `deploy.sh`, nginx is regenerated: re-run `sudo ./deploy/apply_security_headers.sh --enforce-csp` (and the TLS script once TLS exists).
6. `apply_security_headers.sh` with **no flags returns the CSP to report-only**. Always pass `--enforce-csp` to stay enforced.

Handy DB helper (add to `~/.bashrc` on each server):
```
q() { local E=/opt/monitoring-hub/app/.env; mysql -u"$(grep -m1 '^DB_USER=' $E | cut -d= -f2-)" -p"$(grep -m1 '^DB_PASSWORD=' $E | cut -d= -f2-)" -h"$(grep -m1 '^DB_HOST=' $E | cut -d= -f2-)" "$(grep -m1 '^DB_NAME=' $E | cut -d= -f2-)" -e "$1" 2>/dev/null; }
```
(It hides errors; if a query prints nothing, remove `2>/dev/null` and retry.)

## 4. Rollback

| What | How |
|---|---|
| nginx headers/CSP/TLS | `cp -p /etc/nginx/sites-available/monitoring-hub.bak.<timestamp> /etc/nginx/sites-available/monitoring-hub && sudo nginx -t && sudo systemctl reload nginx` (the script prints the exact line) |
| CSP only | `sudo ./deploy/apply_security_headers.sh` (report-only) |
| Origin check misbehaving | `CSRF_ORIGIN_CHECK=false` in `.env`, restart |
| External ID requirement | `REQUIRE_EXTERNAL_ID=false`, restart |
| Retention | `ALERT_RETENTION_DAYS=0`, restart |
| A bad code change | `git revert <commit>` and redeploy (migrations stay; they are additive) |

## 5. Still open

| Item | Status |
|---|---|
| **TLS** (A1) | Deferred until DNS exists; sequence in section 7. Interim: restrict the AWS security group for ports 80/22 to the office or VPN ranges. |
| **Default users** (E4) | Deferred with TLS. Data supports it: `editor`, `viewer`, `HCSCLOUDOPS` have zero logins (audit log complete from 4 Sep). Use Access Control > Users > Deactivate (reversible, ends sessions, audited). Change `admin`'s password via the user menu > Change Password. Leave `Avisha`. |
| Unattached EBS volumes (13 on U4RAD) | Owned by the monitoring and client teams. The app lists them (Security Findings > "Unattached EBS volume") with size and creation date. |
| `metric_history_unitfix_bak` (127 MB, prod) | Procedure in section 8. |
| Phone layout check | Deferred. Alerts and Security Findings use a stacked-card layout at 640 px and below, not yet verified on a real device. |
| B7 (resource list at ~1100 px) | Needs a visual check; not reproducible from code. |
| Scaling ceiling (G9) | Single leader process. Watch `/api/health/detail` (collector `stale_accounts`, `oldest_sync_age_seconds`) and the scheduler tier durations in the journal; when a tier takes longer than its interval, move to per-account workers or CloudWatch Metric Streams. Not needed at the current size (2 accounts). |

## 6. Useful checks

```
# unattached volumes with sizes
q "SELECT r.resource_id, r.name, JSON_UNQUOTE(JSON_EXTRACT(r.tags,'$._ebs_size_gib')) AS gib FROM resources r WHERE r.resource_type='ebs' AND JSON_UNQUOTE(JSON_EXTRACT(r.tags,'$._ebs_state'))='available' AND r.last_seen_at > NOW() - INTERVAL 30 MINUTE ORDER BY CAST(JSON_UNQUOTE(JSON_EXTRACT(r.tags,'$._ebs_size_gib')) AS UNSIGNED) DESC"

# alert coverage: blank thresholds that have a recommended default (also in Settings > Metric thresholds)
q "SELECT COUNT(*) FROM thresholds WHERE warning_value=1000000 AND critical_value=5000000 AND comparison='>' AND enabled=1"

# health and headers
curl -s http://127.0.0.1/api/health/live; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/api/health/ready
curl -sI http://127.0.0.1/ | grep -i -E "content-security|x-frame|x-content"
```

RBAC matrix, live check and regeneration:
```
python3 scripts/rbac_matrix.py --write docs/RBAC_MATRIX.md      # after adding or changing any route (CI fails if stale)
python3 scripts/rbac_smoke.py --base http://127.0.0.1 --roles viewer,editor    # DEV only, with throw-away test users
```
`rbac_smoke.py` signs in as each role, then calls every GET route that role must NOT reach and expects 401/403 (refused before the endpoint runs, so nothing changes). It never sends POST/PUT/PATCH/DELETE. `--positive` also calls permitted routes, which really execute.

## 7. DNS day: enabling TLS (dev first, then prod)

Prerequisites: an A record for the server's name, `dig +short <name>` returns the server's public IP, and TCP **443 and 80** open in the AWS security group.

1. `sudo ./deploy/enable_tls.sh --domain <name> --email <you@company>`
   (no domain: `--self-signed --cn <public IP>`; browsers will warn once).
2. Browse `https://<name>/`, confirm the app loads and live updates work.
3. In `.env` set `COOKIE_SECURE=true` and `PUBLIC_APP_URL=https://<name>` (plus `SSO_SP_ACS_URL` and `CORS_ALLOWED_ORIGINS` if used).
4. `sudo ./deploy/enable_tls.sh --domain <name> --email <you@company> --redirect`, then `sudo systemctl restart monitoring-hub`.
5. Re-apply headers with HSTS: `sudo ./deploy/apply_security_headers.sh --enforce-csp --hsts` (never run it bare).
6. Verify: `curl -sI https://<name>/ | grep -i strict-transport`, `curl -sI http://<name>/ | head -3` shows a 301 to https, log in, and in DevTools > Application > Cookies the `mh_session` cookie shows **Secure** and **HttpOnly**.
7. Confirm certificate renewal is scheduled: `systemctl list-timers | grep certbot`.
8. Remove the temporary IP restriction on the security group if you added one, then do the user cleanup (section 5).
Rollback: the script prints the timestamped backup path; `COOKIE_SECURE=false` plus the restore line brings HTTP back.

## 8. Dropping `metric_history_unitfix_bak` (prod)

A leftover manual copy of `metric_history` (1.49 M rows, 127 MB) from an earlier unit fix. Nothing in the repo references it. Take a compressed dump first, verify it, then drop. The block refuses to proceed if the table is under 14 days old or if any view or foreign key refers to it, and asks you to type DROP. Keep the dump about 30 days, then delete it.

```
bash -euo pipefail <<'EOF'
E=/opt/monitoring-hub/app/.env
DBU=$(grep -m1 '^DB_USER=' $E | cut -d= -f2-);     DBP=$(grep -m1 '^DB_PASSWORD=' $E | cut -d= -f2-)
DBH=$(grep -m1 '^DB_HOST=' $E | cut -d= -f2-);     DBN=$(grep -m1 '^DB_NAME=' $E | cut -d= -f2-)
my() { MYSQL_PWD="$DBP" mysql -N -B -u"$DBU" -h"$DBH" "$DBN" -e "$1"; }
T=metric_history_unitfix_bak
age=$(my "SELECT COALESCE(DATEDIFF(NOW(), create_time), -1) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='$T'")
[ -n "$age" ] || { echo "$T not found (already dropped?)"; exit 0; }
[ "$age" -ge 0 ] || { echo "creation time unknown: check by hand before dropping"; exit 1; }
[ "$age" -ge 14 ] || { echo "$T is only $age day(s) old: wait until it is at least 14 days old"; exit 1; }
deps=$(my "SELECT (SELECT COUNT(*) FROM information_schema.views WHERE table_schema=DATABASE() AND view_definition LIKE '%unitfix%') + (SELECT COUNT(*) FROM information_schema.key_column_usage WHERE table_schema=DATABASE() AND referenced_table_name='$T')")
[ "$deps" = "0" ] || { echo "something depends on $T ($deps reference(s)): stopping"; exit 1; }
rows=$(my "SELECT COUNT(*) FROM $T")
echo "rows: $rows   range: $(my "SELECT CONCAT(MIN(metric_timestamp),' to ',MAX(metric_timestamp)) FROM $T")"
OUT=$HOME/${T}_$(date +%Y%m%d).sql.gz
MYSQL_PWD="$DBP" mysqldump -u"$DBU" -h"$DBH" --single-transaction --quick "$DBN" "$T" | gzip > "$OUT"
gzip -t "$OUT"; ls -lh "$OUT"
inserts=$(zcat "$OUT" | grep -c '^INSERT INTO' || true)
echo "INSERT statements in dump: $inserts"
{ [ "$rows" -eq 0 ] || [ "$inserts" -gt 0 ]; } || { echo "the dump holds no data although the table has $rows rows: NOT dropping"; exit 1; }
read -r -p "Dump verified. Type DROP to delete $T: " ans < /dev/tty
[ "$ans" = "DROP" ] || { echo "not dropped"; exit 1; }
my "DROP TABLE $T"
echo "dropped. still present? -> '$(my "SHOW TABLES LIKE '$T'")'"
EOF
```
