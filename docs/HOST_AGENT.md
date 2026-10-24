# Host agent (push-based host metrics)

Status: phases A to F are merged on `main` (agent, server foundation, ingest, heartbeat watchdog, Host Agents page, rollout).
End-user guide: Help Center, section "Host agents (install on a server)" (`app/help_content/host_agents.py`).

Status legend used in this document:

- **[IN REPO]** exists on `main` and is covered by tests.
- **[DESIGN]** designed, not implemented: agent auto-update and the agent-side signature check (`docs/HOST_AGENT_DISTRIBUTION.md` section 5).

This is **not** the AI agent (`app/agent/*`, `/api/agent/*`). Everything here is `host_agent` / `host-agents`.

## 1. Architecture

```
 server (no inbound ports)                         hub (monitoring-hub, behind nginx + TLS)
 ┌────────────────────────┐   HTTPS POST /api/host-agents/ingest   ┌───────────────────────────────────┐
 │ cloudops-agent          │ ─────────────────────────────────────▶ │ nginx: body limit, per-IP limits,  │
 │  collects every 60 s    │   Authorization: Bearer cah_…          │        keepalive upstream          │
 │  dedicated non-root user│ ◀───────────────────────────────────── │ FastAPI app/api/host_agents.py     │
 └────────────────────────┘   {accepted,rejected,config_version,…}  │  authenticate_agent() → host+acct  │
        ▲ one-time                                                   │  → samples into the hub DB         │
        │ POST /enroll {enrollment_key,…} → {agent_token, config}    └───────────────────────────────────┘
```

- Push only: the agent dials out over HTTPS; nothing listens on the host.
- Identity: enrollment key (`cak_…`, admin-created, scoped to one AWS account row, single-use by default) is exchanged once for an agent token (`cah_…`) bound to one host. Only SHA-256 hashes are stored. Both are shown once.
- No CloudWatch / cloud API calls are made by this feature. The hub's existing agentless pollers are unchanged.
- Flag: `HOST_AGENT_ENABLED` (default `false`). When false **every** `/api/host-agents/*` route answers 404, before authentication. **[IN REPO]**
- Hub code: `app/hostagents/{settings,auth}.py`, `app/api/host_agents.py` (enroll, keys, list, revoke, config) **[IN REPO]**; ingest `app/hostagents/ingest.py` **[IN REPO]**; heartbeat watchdog `app/collector/host_agent_watchdog.py` **[IN REPO]**; state derivation: online (online ≤ `HOST_AGENT_STALE_SECONDS`, stale ≤ 5×, offline beyond, revoked).
- Current readings: `GET /api/host-agents/{id}/metrics` (`hostagents.view`, scoped by the caller's accessible accounts; 404 for an unknown or out-of-scope host) returns the latest stored value per agent metric of one host, with unit, timestamp and `age_seconds`. It is a database read only (no cloud call, no write, nothing computed or estimated): the host's `host_agents.resource_id` is resolved to its `resources` row (`host:<hostname>` = type `host`, otherwise `ec2`), the rows of `metrics` whose names a host agent can feed are returned, and only when `metrics` holds none of them the newest point of the last 24 h of `metric_history` stands in (`source` says which). When the host is attached to an EC2 resource `shared_series` is true: `mem_used_percent` and `disk_used_percent*` are the names the CloudWatch agent collector also writes, so the stored value can come from either source. The Host Agents panel uses it for the Uptime line of the host details (patch 0012 removed the Current metrics table: every other metric is a chart with its latest value) and reloads it every 30 s. **[IN REPO]**

## 2. Hub configuration reference (`.env`, read at call time; restart the service after changing)

| Variable | Default | Meaning |
|---|---|---|
| `HOST_AGENT_ENABLED` | `false` | Master switch. Off = routes 404, nothing authenticates. |
| `HOST_AGENT_STALE_SECONDS` | `180` | `online` window; `stale` up to 5×; `offline` beyond. |
| `HOST_AGENT_MAX_BODY_BYTES` | `262144` | Ingest body cap (413 above). **Keep nginx `client_max_body_size` for `/api/host-agents/ingest` equal to this** (`deploy/nginx.conf`: `256k`). |
| `HOST_AGENT_INGEST_RATE_PER_MIN` | `30` | Per token. |
| `HOST_AGENT_ENROLL_RATE_PER_HOUR` | `20` | Per client IP (Redis; fails open if Redis is down — nginx `ha_enroll` is the second fence). |
| `HOST_AGENT_CREDENTIAL_RETENTION_DAYS` | `90` (floor 30, `0` = off) | Daily pruning of revoked tokens and spent/revoked/expired enrollment keys (`app/collector/retention.py`). |

Per-host config (admin `PATCH /api/host-agents/{id}/config`, bumps `config_version`; agent picks it up on its next ingest response):

```json
{"interval_seconds": 60, "watch_services": [], "disk_exclude_fstypes": ["tmpfs","devtmpfs","overlay","squashfs","proc","sysfs"], "max_batch_metrics": 500}
```

`interval_seconds` 15–300, `watch_services` ≤ 100 names (`^[A-Za-z0-9][A-Za-z0-9_.@:-]*$`). Lower intervals multiply DB growth linearly (see `docs/HOST_AGENT_COST.md`).

Charts (patch 0011): `GET /api/host-agents/{id}/history?hours=N` (`hostagents.view`, same scoping and 404 rules as `/metrics`) returns bucketed history per agent series plus the chart metadata the shared chart card (`MetricChartCard`, the one the EC2 / RDS / Lambda pages use) reads. DB read only: series are discovered the same way as `/metrics`, the buckets are computed by one `GROUP BY` over `metric_history` (epoch arithmetic with `TIMESTAMPDIFF`, so the session time zone cannot shift a bucket), bucket size comes from `bucket_seconds()` and `hours` is capped at the 30-day retention (`requested_hours` vs `effective_hours`). The `uptime_seconds` series is stored but not returned here (a counter that only climbs is not a useful line chart); the host drawer shows the current uptime from `/metrics` instead, and the drawer no longer renders a Current metrics table because every other metric is a chart with its latest value. Points are `{t, v, a, mn, mx, n}` (average, minimum, maximum, count; no sum, a gauge has none). Threshold lines use the same resolver as the EC2 charts (`metric_meta._effective_lines`, enabled rows only) and open alerts are matched by resource id and stored metric name; a host-only metric on an EC2-attached host has no `ec2` threshold and so no line. A bucket exists only where the agent delivered a reading: nothing is interpolated or extended, so an offline host's charts end at its last report.

Watched services (migration 123, patch 0009): the agent reports the state of every name in `watch_services` on the first payload of each pass. Ingest stores the latest state per name in `host_agent_services` (`running|stopped|failed|unknown`, `state_since` = last change, `observed_at` = last report, both from the agent's `sent_at` clamped to [now − 24 h, now + 10 min]; newest wins). A non-empty `services` array is the complete current set, so other names are deleted; an empty one deletes nothing. Services are ignored while the payload's `config_version` differs from the server's, so an agent on an old list cannot bring back a removed name. `PATCH config` deletes the states of removed names at once. `GET /api/host-agents/{id}/services` (`hostagents.view`) returns the config (`interval_seconds`, `watch_services`), the states, names still `pending` and a summary. No alert is raised for failed or stopped services.

Permissions: `hostagents.view` (viewer, editor, admin; scoped by `get_accessible_account_ids`), `hostagents.manage` (admin). Every create / revoke / config change / enrollment writes an audit row.

Agent-side flags and files are in `agent/README.md`. `--insecure-dev` allows `http://` (never use it outside DEV).

## 3. Install / upgrade / uninstall

Replace `<HUB_HOST>` with the hub's DNS name (never commit real hosts or IPs).

**Create an enrollment key** (admin): `POST /api/host-agents/enrollment-keys` body `{"aws_account_id": <id>, "label": "web fleet", "max_uses": 1, "expires_in_hours": 24}`. The key is in the response once. Prefer `max_uses` = number of hosts you are about to install and a short expiry.

**Install** (Linux, systemd) **[IN REPO]**. The Add host dialog shows this command with the new key filled in. By hand:

```
BASE=https://<HUB_HOST>/dl/host-agent
V=$(curl -fsSL "$BASE/manifest.json" | sed -n 's/.*"latest": *"\([^"]*\)".*/\1/p')
curl -fsSLO "$BASE/$V/install.sh"          # read it, and compare its SHA-256 with the release ticket
sudo CLOUDOPS_AGENT_ENROLL_KEY=cak_... bash install.sh --base-url "$BASE/$V" --url https://<HUB_HOST>
```

`--base-url` must be the version folder that holds `cloudops-agent-linux-<arch>` and `SHA256SUMS` (what `scripts/host_agent_release.sh stage` writes; **`stage` copies `install.sh` only if it is in the binaries directory, so put `agent/install.sh` there**). The binary is always checked against `SHA256SUMS` before anything is installed. The key travels in the environment variable `CLOUDOPS_AGENT_ENROLL_KEY` (the installer has no `CLOUDOPS_HUB_URL` / `CLOUDOPS_ENROLLMENT_KEY` variables). macOS and Windows: `install-macos.sh --base-url ... --url ... --key ...`, `install.ps1 -BaseUrl ... -Url ... -Key ...` (`docs/HOST_AGENT_PLATFORMS.md`). Do not paste enrollment keys into shared shell history or CI logs.

**Upgrade** **[DESIGN]**: stage a new immutable version (`scripts/host_agent_release.sh stage`), canary, widen. Auto-update design is in the distribution doc; until it is implemented upgrade = re-run the installer of the new version (token and config are kept).

**Uninstall (healthy host)**: revoke in the hub first, then remove locally:

```
POST /api/host-agents/{id}/revoke           # revokes the host and all its tokens; audited
sudo systemctl disable --now cloudops-agent # service name and paths confirmed against agent/install.sh
sudo rm -f /etc/systemd/system/cloudops-agent.service /usr/local/bin/cloudops-agent && sudo systemctl daemon-reload
# or: sudo bash agent/uninstall.sh [--purge]   (--purge also deletes /etc/cloudops-agent, /var/lib/cloudops-agent and the service user)
```

**Uninstall of a revoked host** (host still running the agent): a revoked host gets 401 on every ingest and stays `revoked` in the list. Stop and remove the agent as above; the host row stays as inventory. Re-enrolling the same hostname in the same account with a valid key makes it `active` again with a fresh token (this is how a revoked host is deliberately brought back; it also means **revoking a host does not stop someone who holds a valid enrollment key — revoke or let expire the keys as well**).

## 4. Troubleshooting

Hub-side commands run in `/opt/monitoring-hub/app`. Logs: `journalctl -u monitoring-hub --since "15 min ago" | grep -i "host_agents"`. Look at the hub's `/api/host-agents` list (`state`, `last_seen_at`) first.

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Agent cannot reach hub (timeouts, TLS errors) | outbound 443 blocked, DNS, hub security group lacks 443, self-signed cert | From the host: `curl -sS -o /dev/null -w '%{http_code}\n' https://<HUB_HOST>/api/health/live` (expect 200). Certificate errors: fix the cert; the agent refuses `http://` unless `--insecure-dev`. |
| `404` on enroll / ingest | `HOST_AGENT_ENABLED` is false on that hub, or wrong URL prefix | `grep -n '^HOST_AGENT' .env`; set `true`, restart. |
| `401` on enroll | key unknown, expired, used up, revoked, or mistyped | `GET /api/host-agents/enrollment-keys` shows `status`. Create a new key. All failures return the same message by design. |
| `401` on ingest | token revoked, host revoked, or host re-enrolled elsewhere (old token rotated) | List shows `state=revoked`, or re-enroll the host with a new key. |
| `413` | payload > `HOST_AGENT_MAX_BODY_BYTES` / nginx 256k | Lower `max_batch_metrics` / watch fewer services, or raise **both** limits together. |
| `429` | nginx `ha_ingest` / `ha_enroll` (per IP) or the app's per-token / per-IP limit | Many hosts behind one NAT IP can hit the per-IP zone: raise `rate` in `deploy/nginx.conf`. `Retry-After` is set by the app limiter. |
| Samples rejected (`rejected` > 0), host shows online | clock skew: sample `ts` > 10 min in the future or > 24 h in the past is dropped | On the host: `timedatectl` / `chronyc tracking`; fix NTP. The agent clock (`sent_at`) is compared with hub time. |
| Host flaps `online`↔`stale` | interval > stale window, or intermittent 429/5xx | Keep `interval_seconds` ≤ `HOST_AGENT_STALE_SECONDS`/2. |
| High agent CPU | very short interval, many watched services, huge mount list | `PATCH config` to a larger `interval_seconds`, shrink `watch_services`, extend `disk_exclude_fstypes`; `top -p $(pgrep cloudops-agent)`. |
| Hub CPU / DB growth after widening | too many hosts × short interval | `docs/HOST_AGENT_COST.md`; run `scripts/host_agent_loadtest.py` on DEV. |

## 5. PROD rollout runbook

Principles: flag off until a gate passes; DEV first; one change at a time; every step reversible in minutes. Deploy blocks follow the repo's standard formats.

### 0. Pre-checks (both environments)
1. Phases A to F merged (they are); `python3 -m pytest -q` green.
2. Migration 120 applied (`migrate.py status`). Redis running (rate limiter fails open without it).
3. nginx: apply `deploy/nginx.conf` changes to the **live** file (it is generated once by `deploy.sh`/`setup.sh`, then edited by `enable_tls.sh` / `apply_security_headers.sh`, so a git pull does not change it). Section 6 has the exact commands.
4. TLS works on the hub (`https://<HUB_HOST>/` loads); agents refuse plain HTTP.
5. Disk headroom for the rows estimate in `docs/HOST_AGENT_COST.md`.

### 1. DEV soak (minimum 7 days, includes a weekend)
Enable: `HOST_AGENT_ENABLED=true` in DEV `.env`, restart. Enroll 3–5 DEV hosts (mixed OS/arch if supported), then run the load test once: `python3 scripts/host_agent_loadtest.py --confirm-dev …` (≥ 50 hosts, 5 min).

Check daily; **all** must hold to pass:
- every enrolled host `online`, none flapping (`GET /api/host-agents`);
- `journalctl -u monitoring-hub --since "24 hours ago" | grep -i -E "host_agents|Traceback"` — no unexpected errors;
- ingest error rate < 1% and p95 latency < 250 ms in the load test (thresholds are a proposal; adjust after the first measurement);
- hub service CPU/RSS not trending up (`systemctl status monitoring-hub`, `top`), DB rows/day matches the estimate within 2×;
- retention ran: `SELECT COUNT(*) FROM host_agent_tokens WHERE revoked_at < NOW() - INTERVAL 100 DAY;` returns 0 once old data exists;
- revoke test: revoke one host, confirm 401 on its next push and `state=revoked`; re-enroll it, confirm recovery;
- RBAC check: a viewer sees only their accounts' hosts; a non-admin cannot create keys.

### 2. PROD canary (2–3 hosts, 48–72 h)
1. Deploy hub code (PROD block below), `HOST_AGENT_ENABLED=true`, restart.
2. Create one key, `max_uses` = 3, expiry 24 h, for the canary account. Install on 2–3 low-risk hosts.
3. Watch the same checklist as DEV for 48–72 h, plus agent CPU on the host (`top`) and no impact on the host's own services.
4. Revoke the key when the canary hosts are enrolled.

### 3. Widen
10 hosts → 25% of fleet → 100%, each step ≥ 24 h, gate = checklist green. Never exceed ~100 hosts before a PROD load measurement exists (capacity statement in the cost doc is an estimate). Use one key per batch, short expiry, `max_uses` = batch size.

### 4. Rollback
Fast (hub side, takes effect after restart; agents get 404 and back off):
```
cd /opt/monitoring-hub/app
sed -i 's/^HOST_AGENT_ENABLED=.*/HOST_AGENT_ENABLED=false/' .env
sudo systemctl restart monitoring-hub
systemctl status monitoring-hub | grep -i active
```
Then stop agents on the affected hosts so they do not keep retrying (`sudo systemctl stop cloudops-agent`). Revoke any still-valid enrollment keys (`DELETE /api/host-agents/enrollment-keys/{id}`; with the flag off the API is 404, so do this **before** flipping the flag if keys are outstanding, or re-enable briefly). Ingested history is not deleted by a rollback; it ages out with `metric_history` retention (30 days). Hub code rollback is a normal `git revert` + deploy; migration 120 tables are additive and can stay.

### Deploy blocks
DEV:
```
git log --oneline -1 ; git am 0006-host-agent-rollout.patch ; python3 -m py_compile app/collector/retention.py scripts/host_agent_loadtest.py && echo "compiles clean" ; rm 0006-host-agent-rollout.patch ; git push origin main
```
PROD:
```
git pull origin main ; /opt/monitoring-hub/venv/bin/python3 migrate.py status ; /opt/monitoring-hub/venv/bin/python3 migrate.py apply --all-pending ; sudo systemctl restart monitoring-hub ; systemctl status monitoring-hub | grep -i active
```
(No migration in phase F; `migrate.py` should report nothing pending beyond what you already applied.)

## 6. nginx: applying and testing `deploy/nginx.conf` on a live box

The repo file is a template. On a running box the live file is `/etc/nginx/sites-available/monitoring-hub` (TLS and security headers were added in place). **Do not overwrite it with the template**; add the three things below to the live file and keep a backup.

1. Backup: `sudo cp -p /etc/nginx/sites-available/monitoring-hub /etc/nginx/sites-available/monitoring-hub.bak.$(date +%Y%m%d_%H%M%S)`
2. Edit the live file: copy from the repo's `deploy/nginx.conf` (a) the block above `server {` (`limit_req_zone … ha_enroll/ha_ingest/ha_dl`, `limit_conn_zone ha_conn`, `upstream monitoring_hub_agents`) and (b) the three `location` blocks before `location /api/ {` (`= /api/host-agents/ingest`, `= /api/host-agents/enroll`, `^~ /dl/host-agent/`). Do not add `add_header` lines to them.
3. Test: `sudo nginx -t`
4. Reload (no dropped connections): `sudo systemctl reload nginx`
5. Verify (replace host):
```
curl -sk -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' --data '{}' https://<HUB_HOST>/api/host-agents/ingest        # 404 when disabled, 401 when enabled
head -c 300000 /dev/zero | curl -sk -o /dev/null -w '%{http_code}\n' -X POST --data-binary @- https://<HUB_HOST>/api/host-agents/ingest     # 413
curl -skI https://<HUB_HOST>/api/host-agents/enroll | grep -i -E 'x-frame|x-content|referrer'    # headers still present (single copy)
```
6. Rollback: `sudo cp -p <backup> /etc/nginx/sites-available/monitoring-hub && sudo nginx -t && sudo systemctl reload nginx`

Notes: `deploy/deploy.sh` regenerates the site file from the template, so after any full redeploy re-run `deploy/enable_tls.sh` and `deploy/apply_security_headers.sh` as before (both were tested against this template and leave the agent locations intact). If the hub ever sits behind a load balancer, `$binary_remote_addr` is the balancer's address; switch the zones to `$http_x_forwarded_for`-derived keys with a `real_ip` setup first.

## 7. Load test

`scripts/host_agent_loadtest.py` (standard library only; DEV only: refuses without `--confirm-dev`). Reports p50/p95/p99 latency, error rate, 429s, accepted/rejected samples and rows/min. See the script header for flags, and `docs/HOST_AGENT_COST.md` for how to read the result. It needs the ingest route (merged).
