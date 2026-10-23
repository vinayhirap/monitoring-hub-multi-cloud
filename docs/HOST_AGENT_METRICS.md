# Host agent: metric pipeline and mapping

Scope: `POST /api/host-agents/ingest` (Phase C). Everything is inert unless `HOST_AGENT_ENABLED=true`.

## 1. How an EC2 CPU / memory / disk value reaches alerts today (agentless)

```
scheduler tier tick ──> app/collector/metrics/runner.py  _collect_account()
   STS role session ──> CloudWatch GetMetricData (paid, $0.01 / 1000 metrics requested), batches of <= 500 queries
        cpuutilization (AWS/EC2, standard tier, Period 300)          _collect_core / polling_model.AWS_CORE_METRICS
        mem_used_percent (CWAgent, standard tier, Period 60)         _collect_ec2_cwagent_mem   (ListMetrics discovery cached 4 h)
        disk_used_percent[__slug] (CWAgent, low tier, Period 60)     _collect_ec2_cwagent_disk  (one query per real mount)
   _execute_gmd() ──> write_metrics_batch()          metrics(resource_id, metric_name, metric_value, metric_timestamp)
                                                      UNIQUE (resource_id, metric_name): last-value cache, timestamp = server UTC now
                  ──> write_metric_history_batch()   metric_history(...) UNIQUE (resource_id, metric_name, metric_timestamp), 7 d retention
alert_evaluator.py  SELECT ... FROM metrics m JOIN resources r ON r.id = m.resource_id
                      JOIN aws_accounts aa (status 'active')
                      JOIN metric_catalog mc ON <db name of mc.metric_name> = m.metric_name
                      JOIN thresholds t ON t.metric_id = mc.id AND t.resource_type = r.resource_type
                                       AND t.aws_account_id = r.aws_account_id AND t.enabled = 1
                    WHERE m.metric_timestamp >= UTC_NOW - eval window of the metric's cadence class
```
Consequences used by this phase: the join is by metric NAME, then `thresholds` narrows by `(account, resource_type, metric_id)`;
`thresholds` is UNIQUE `(aws_account_id, metric_id)`, i.e. one resource_type per metric per account. Non-root mounts are the
synthetic names `disk_used_percent__<slug>` registered on first sight by `disk_mounts.ensure_disk_mount_metric_registered`.

## 2. Resource mapping

| `host.instance_id` | Result | `host_agents.resource_id` |
|---|---|---|
| matches `resources(account, 'ec2', instance_id)` and no other active agent holds it | attach to that row; discovery keeps owning name/tags/state | `i-...` |
| null / no match / held by another agent | create-or-update `resources` type **`host`**, `resource_id = host:<hostname>`, name = hostname, tags `{_host_agent, _hostname, _os, _arch, _agent_version}`, `last_seen_at = NOW()` on every sample ingest, `instance_state` NULL | `host:<hostname>` |

Why a new type `host`: no existing type fits a server that is not a discovered EC2 instance, and reusing `ec2` would make
the EC2 pages and collector treat a non-instance id as an instance id. Existing `resource_type` filters are allowlists of real
types (core collector, EC2 pages: never select `host`) or `NOT IN` lists (harmless); alert cadence treats unknown AWS types
as `extended`. An agent that starts as `host` is re-attached to the EC2 row on a later ingest once discovery has it
(history already written under the `host` resource is not merged).

## 3. Metric mapping (contract name -> stored `metric_name`)

| Contract | Stored | Existing counterpart | Catalog |
|---|---|---|---|
| `mem_used_percent` | `mem_used_percent` | CWAgent memory (same name, same meaning) | existing row, existing `ec2` thresholds |
| `disk_used_percent{mount}` | `disk_used_percent` (`/`, `C:`) / `disk_used_percent__<slug>` | CWAgent disk, same slug rules (`disk_mounts.py`) | existing; non-root mounts of EC2-attached hosts registered via `ensure_disk_mount_metric_registered` |
| `cpu_percent` | `host_cpu_percent` | none: `cpuutilization` is hypervisor CPU, and writing guest CPU under it would interleave two series | migration 122 |
| `load1` `load5` `load15` | `host_load1` `host_load5` `host_load15` | none | 122 |
| `mem_available_bytes` | `host_mem_available_bytes` | none | 122 |
| `swap_used_percent` | `host_swap_used_percent` | none | 122 |
| `disk_inodes_used_percent{mount}` | `host_disk_inodes_used_percent[__<slug>]` | none | 122 (base only) |
| `disk_read_bytes_per_s` `disk_write_bytes_per_s` | `host_disk_read_bytes_per_s` `host_disk_write_bytes_per_s` | none (EC2 DiskRead/Write are instance-store only) | 122 |
| `net_rx_bytes_per_s{iface}` `net_tx_bytes_per_s{iface}` | `host_net_rx_bytes_per_s__<iface>` `host_net_tx_bytes_per_s__<iface>` | none (`networkin/out` are hypervisor, per instance) | 122 (base only) |
| `proc_count` `uptime_seconds` | `host_proc_count` `host_uptime_seconds` | none | 122 |
| `services[]` | **not persisted** | no table or metric in the contract | gap |

Validation (per sample, counted in `rejected`): unknown name; non-finite / non-numeric / bool value; percent outside 0-100;
negative value; `ts` > 10 min in the future or > 24 h old; missing or invalid `mount` / `iface` label; pseudo mount paths
(`/proc`, `/snap/...`, `/run...`, ...); more than 32 distinct mounts/interfaces per metric per payload. Whole payload: not JSON /
not an object / schema != 1 / bad header fields -> 400; body > `HOST_AGENT_MAX_BODY_BYTES` or > 500 metrics or > 200 services -> 413.

Migration 122 adds catalog rows (`service='host'`, `namespace='HostAgent'`, **enabled = 0**, `is_default = 0`) and disabled default
thresholds for `resource_type='host'` on existing accounts: `host_cpu_percent` 85/95, `host_swap_used_percent` 50/80,
`host_disk_inodes_used_percent` 80/90 (all `>`). Alerting for these needs an operator to enable the threshold.

## 4. `HOST_AGENT_PREFER_OVER_CWAGENT` (default false)

Effective only with `HOST_AGENT_ENABLED=true`. Each accepted ingest that carries `mem_used_percent` / `disk_used_percent*` writes a
marker `hostagent:feed:<resources.id>` (TTL `HOST_AGENT_STALE_SECONDS`) in `app/utils/shared_cache`. The collector then skips:

* mem: the instance entirely in `_collect_ec2_cwagent_mem` (no ListMetrics, no GetMetricData);
* disk: only the mounts the agent reports in `_collect_ec2_cwagent_disk` (the other mounts keep coming from CWAgent; the 4 h
  cached ListMetrics discovery still runs).

Marker expiry (agent stopped) resumes CWAgent on the next cycle; its look-back (mem 10 min, disk 20 min) back-fills. Redis down
means no marker is visible and nothing is skipped. With the flag off the runner is byte-for-byte the old behaviour (same list
object, no cache read). `cpuutilization` is never skipped (CloudWatch parity is kept).

Estimated saving per fully-covered instance: mem 288 + disk 96 x mounts metric requests per day, e.g. 20 instances with one
mount = 7,680 requests/day = about $0.077/day (about $2.30/month). The real benefit is 60 s resolution, not the bill. The
collector logs the skipped query count per cycle at INFO.
