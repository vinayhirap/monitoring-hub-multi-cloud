# Host agent: cost, capacity and retention

Every number is tagged **MEASURED** (observed in this repo/sandbox or in a repo document that says it was measured), **MODELLED** (arithmetic from stated inputs) or **ESTIMATED** (an assumption that must be replaced by a measurement). **No load test against a real hub has been run**: phase C (ingest) is not merged, so every capacity figure below is MODELLED/ESTIMATED. Prices are not assumed anywhere (same convention as `docs/POLLING_COST_ESTIMATE.md`): multiply the counts by the current AWS rates.

## 1. CloudWatch / GetMetricData: saved vs added

**Added CloudWatch calls: none.** The agent pushes to the hub; `app/hostagents/*` and `app/api/host_agents.py` contain no boto3/CloudWatch use (checked). Phase F adds none either.

**Saved today: zero.** The collector is unchanged; it still polls CWAgent memory/disk for every EC2 instance that has the CloudWatch agent. Savings exist only after a follow-up that makes the collector **skip** the CWAgent memory/disk queries for instances with an `online` host agent (not built; needs the host→`resources` mapping from phases C/D, and the ingested series must keep the names the thresholds and charts already use: `mem_used_percent`, and the per-mount disk naming of `app/collector/polling_model.py`, which phase C must map).

**What would be saved, per EC2 instance that moves from CWAgent to the host agent (MODELLED).** Billing unit is the metric requested (see `POLLING_COST_ESTIMATE.md` §2). Intervals from `app/collector/polling_model.py`: CWAgent memory on the `standard` tier (300 s), disk on `low` (900 s).

```
memory: 86400/300 x 1 metric            = 288 billed metrics/day
disk:   86400/900 x M mounts (M=2)      = 192 billed metrics/day
per instance                            = 480/day  = 14,400/month
```
For the 40 EC2 instances of the 2026-10-13 prod snapshot in `POLLING_COST_ESTIMATE.md` (assuming all run CWAgent, which that document marks as assumed): 19,200 billed metrics/day = 576,000/month. CPU, network and status-check metrics still come from CloudWatch (the agent does not replace the hypervisor view), so the saving is limited to memory and disk. Retiring CWAgent itself would also remove its custom-metric charges on the instance side (per metric-month plus PutMetricData); count `CWAgent` metrics in the account to size that.

**Gap to close for any saving:** collector skip logic + a documented fallback (agent offline for > `HOST_AGENT_STALE_SECONDS` → resume CloudWatch polling, otherwise a dead agent silently blinds memory/disk alerts).

## 2. What the agent adds

### DB growth (MODELLED)
Ingested samples are expected in `metric_history` (the existing time-series table, pruned daily at `METRIC_HISTORY_RETENTION_DAYS` = 30 days; see §3). **Confirm against phase C**; if C uses another table it needs its own pruning.

Rows per report: 11 fixed metrics + 2 per mount + 2 per interface. Typical host (2 mounts, 1 interface) = **17**; the load test sends 19.

| Interval | rows/host/day | rows/host/30 d | 100 hosts, 30 d |
|---|---|---|---|
| 60 s (default) | 24,480 | 734k | 73M |
| 30 s | 48,960 | 1.5M | 147M |
| 15 s (minimum) | 97,920 | 2.9M | 294M |

Bytes per row: ESTIMATED 100–150 B including both secondary indexes (`idx_metric_history_lookup`, `idx_metric_history_timestamp`; schema in `db/migrations/002c_metric_history_table.sql`). So 100 hosts at 60 s ≈ **7–11 GB** after 30 days, 25–40 GB at 15 s. The database runs on the hub box (`deploy/deploy.sh` installs MySQL locally). **Check free disk (`df -h /`) on PROD before the canary; no PROD disk number is available to this review.** Steady state is reached after 30 days; the first 30 days grow linearly.

`metric_history` has a single global retention. `app/collector/baseline.py` needs the 30 days for seasonality, so shortening it for agent rows is not free. A per-source retention (for example 7 days of 60 s samples) would cut agent storage by ~75%; it is **not implemented** and needs phase C to mark agent rows (new column or separate table). Listed as a gap.

### Network (MEASURED payload, MODELLED totals)
A 19-metric payload is **1,625 bytes** of JSON (measured from `scripts/host_agent_loadtest.py`; no compression). Per host: 2.3 MB/day; 100 hosts: 234 MB/day inbound to the hub. Inbound traffic to AWS is not billed; traffic leaving the monitored hosts may be, depending on whether they reach the hub through the internet or inside the VPC (not assumed).

### CPU on the hub (ESTIMATED)
Per ingest request: one token lookup, one host update, one multi-row insert. With no measurement, three assumed costs per request: 15 / 30 / 60 ms of CPU.

## 3. Retention coverage check

| Data | Where it is pruned | Status |
|---|---|---|
| Ingested samples | `metrics_writer.prune_metric_history` (daily, low tier, 30 days, batched) | covered **if** phase C writes to `metric_history` (**CONTRACT**, verify); `tests/test_host_agent_retention.py` pins the scheduler call |
| `host_agent_tokens` (revoked) | new `retention.prune_host_agent_credentials`, `HOST_AGENT_CREDENTIAL_RETENTION_DAYS` (90, floor 30) | **fixed in this patch**; active tokens never deleted |
| `host_agent_enrollment_keys` (revoked / expired / used up) | same function | **fixed in this patch**; usable keys never deleted |
| `host_agents` | kept (inventory; audit rows reference ids) | by design; revoked hosts remain listed |
| Audit rows | never pruned (`audit_logs` is the compliance trail) | unchanged; enroll/revoke/config rows are small |
| Redis rate-limit keys | expire by TTL | n/a |

## 4. Capacity for the current PROD box

Box: **t3.large, 2 vCPU / 8 GiB** (MEASURED fact from `deploy/ollama.md`, which also records CPU credits were exhausted when it was measured). AWS publishes a t3.large baseline of 30% per vCPU, i.e. about **0.6 vCPU sustained** once credits run out (published figure, not measured here). The same box runs nginx, 2 uvicorn workers, MySQL, Redis, the collector and, on PROD, optionally Ollama. So agent traffic must fit in a small slice: assume **0.1 vCPU (ESTIMATED budget, ≈ 1/6 of the sustained baseline)**.

```
hosts = (budget_vcpu / cpu_seconds_per_request) x interval_seconds
```

| CPU per ingest request (ESTIMATED) | requests/s at 0.1 vCPU | hosts at 60 s | hosts at 15 s |
|---|---|---|---|
| 15 ms | 6.7 | ~400 | ~100 |
| 30 ms | 3.3 | ~200 | ~50 |
| 60 ms | 1.7 | ~100 | ~25 |

Insert rate is not the constraint (100 hosts at 60 s = 28 rows/s). **Storage and the CPU slice are.** Recommendation (ESTIMATED, to be replaced by the DEV measurement below): plan for **≤ 100 hosts at 60 s on the current PROD box**, canary 2–3, and do not widen past 100 until a PROD-sized measurement exists. DEV is a different size from PROD unless the instance types match; compare before extrapolating.

### How to replace the estimates with measurements
1. DEV with the same instance type as PROD, flag on, idle otherwise.
2. `python3 scripts/host_agent_loadtest.py --confirm-dev --base-url https://<DEV_HOST> --enrollment-key cak_… --hosts 100 --duration 600 --save-tokens /tmp/ha_tokens.json` (raise `HOST_AGENT_ENROLL_RATE_PER_HOUR` on DEV for the run; revoke the `loadtest-*` hosts and delete the token file after).
3. Record from the output: p50/p95/p99 latency, error rate, `req/s`; from the box: `top` during the run (CPU of the two uvicorn workers + mysqld) to derive CPU-per-request = (CPU seconds consumed) / (requests); `SELECT COUNT(*) FROM metric_history;` before and after for real rows (the script's rows/min comes from the hub's `accepted` counts, not the DB).
4. Repeat at 200 and 400 hosts; stop at the first of: p95 > 250 ms, error rate > 1%, CPU credits falling.
5. Put the measured numbers into the table above and mark them MEASURED.

## 5. Open items
1. Collector skip logic for agent-covered instances (the only source of CloudWatch savings).
2. Per-source retention for agent samples.
3. Confirm phase C's target table and that `retention` covers it.
4. PROD disk size and the DEV-vs-PROD instance comparison.
