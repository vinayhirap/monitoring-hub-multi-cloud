# ⚠️ ARCHIVED — NOT USED IN THIS DEPLOYMENT (2026-09-26)
#
# This whole VictoriaMetrics + tiered-YACE stack is not run anywhere in
# this environment -- dev and prod both rely entirely on the direct
# cloud-API collection path (CloudWatch/Azure Monitor/GCP Monitoring ->
# app/collector -> MySQL, see app/aws/collector_direct.py and the
# describe-poll/multicloud-collector threads) instead. Moved here
# (out of deploy/, preserving git history via `git mv`) rather than
# deleted, in case this deployment model is revisited later. Nothing
# in this directory is referenced by any live script, systemd unit, or
# app code path -- see ../MANIFEST.md for how that was confirmed.
#
# The 6 files in old-generated-configs/ are one-time, real debug
# output from an earlier manual run (account name/id redacted before
# archiving) -- not templates, and not regenerated/consumed by
# anything. fetch-configs.sh is what actually generates a live
# yace-<tier>.yml, if this stack is ever brought back into use.
#
# ---
#
# # Tiered YACE deployment (cost-optimization fix #2)

Why this exists: a single YACE process has exactly one global
`--scraping-interval` — the per-job `period`/`length` values in a YACE config
only control the CloudWatch query *window*, not how often YACE calls the AWS
API. So tiering metrics into critical/standard/trend in the Metric Catalog
does nothing for cost unless you actually run 3 separate YACE processes, each
with the matching scrape interval. This directory does that.

## One-time setup, per AWS account/region

1. On the regional monitoring server for that account (per the distributed
   architecture — CloudWatch → YACE → local VictoriaMetrics, one stack per
   account/region), copy this whole `deploy/yace-tiered/` directory over.

2. Fetch the 3 tier configs from the central FastAPI backend:
   ```bash
   chmod +x fetch-configs.sh
   ./fetch-configs.sh http://<backend-host> <account_id> <bearer-token>
   ```
   This writes `yace-critical.yml`, `yace-standard.yml`, `yace-trend.yml` —
   the exact filenames `docker-compose.yml` expects. (You can also just
   click the 3 tier download buttons in Settings → Metrics to Monitor in the
   app and scp the files over instead of using the script.)

3. If this account is same-account (no `role_arn` on the account row), drop
   an AWS credentials file at `./credentials`. If it's cross-account
   (role_arn set), the generated configs already embed `roleArn`/`externalId`
   per job — you don't need a credentials file at all, just an instance
   profile / base credentials with `sts:AssumeRole` on that role.

4. Start everything:
   ```bash
   docker compose up -d
   ```

## What's running afterward

| Container | Role | Poll interval |
|---|---|---|
| `yace-critical` | CPUUtilization, StatusCheckFailed-class metrics | 60s |
| `yace-standard` | Extended-tier default metrics | 300s |
| `yace-trend` | Non-default / rarely-alerted metrics | 900s |
| `vmagent` | Scrapes all 3 YACE `/metrics` into VM | matches each tier |
| `victoriametrics` | Local store, queried by central FastAPI/Grafana | — |

## Verify it's working

```bash
# VM has data from all 3 tiers?
curl http://localhost:8428/api/v1/label/__name__/values | grep aws_

# Each YACE is up and scraping?
curl http://localhost:5001/metrics | head    # critical
curl http://localhost:5002/metrics | head    # standard
curl http://localhost:5003/metrics | head    # trend

# Confirm actual metric names for services this session added VM-first
# reads for (ECS/Lambda/ALB) before trusting the guesses in
# app/aws/collector_direct.py — the code falls back to boto3 safely either
# way, but you'll want to know once it's actually saving calls:
curl http://localhost:8428/api/v1/label/__name__/values | grep -E "aws_ecs|aws_lambda"
```

## Redeploying after a metric-selection change

Whenever the enabled metrics change in Settings → Metrics to Monitor, re-run
`fetch-configs.sh`, then:
```bash
docker compose restart yace-critical yace-standard yace-trend
```

## Point the central app at this VM instance

Set `VM_URL` (in the central FastAPI backend's env) to
`http://<this-server-host>:8428`. Note: today the backend has a single
global `VM_URL` for all accounts (see the architecture-gap note in the
project handoff doc — `aws_accounts` has no `vm_endpoint` column yet). If
you're running this per-account/region as intended, that gap needs closing
before multi-account VM endpoints work correctly; it's tracked as an open
item, not part of this cost-optimization patch.
