-- db/migrations/122_host_agent_metric_catalog.sql
--
-- Phase C of the host agent: metric_catalog entries for the agent signals that have NO existing counterpart (see
-- docs/HOST_AGENT_METRICS.md). mem_used_percent and disk_used_percent[__slug] already exist (CWAgent) and are reused.
--
-- Inert: every catalog row is enabled = 0 / is_default = 0, so nothing is collected, listed as selected or alerted
-- because of this file. Threshold rows are seeded for the new resource type 'host' with enabled = 0 (the evaluator joins
-- thresholds.enabled = 1), so an operator turns alerting on per account deliberately.
--
-- Not seeded on purpose: host_load1/5/15 (a sensible limit depends on the core count), byte/rate metrics, uptime, process
-- count (no universal threshold).
--
-- Idempotent (INSERT IGNORE on metric_catalog UNIQUE (provider, namespace, metric_name) from 065 and thresholds
-- UNIQUE (aws_account_id, resource_type, metric_id)); nothing here is MySQL-8-only, so it runs on MySQL 8 and MariaDB.
-- Per-mount / per-interface series (host_disk_inodes_used_percent__<slug>, host_net_*__<iface>) are stored under the
-- base entry's name plus a suffix, the same convention as disk_used_percent__<slug>; only the base is cataloged.

INSERT IGNORE INTO metric_catalog
    (service, namespace, display_service, metric_name, statistic, unit, default_interval, category, description,
     is_default, enabled, provider)
VALUES
  ('host', 'HostAgent', 'Host Agent', 'host_cpu_percent',               'Average', 'Percent',        60, 'extended', 'Guest OS CPU utilization reported by the host agent (not hypervisor CPUUtilization)', 0, 0, 'aws'),
  ('host', 'HostAgent', 'Host Agent', 'host_load1',                     'Average', 'Count',          60, 'extended', '1-minute load average',                         0, 0, 'aws'),
  ('host', 'HostAgent', 'Host Agent', 'host_load5',                     'Average', 'Count',          60, 'extended', '5-minute load average',                         0, 0, 'aws'),
  ('host', 'HostAgent', 'Host Agent', 'host_load15',                    'Average', 'Count',          60, 'extended', '15-minute load average',                        0, 0, 'aws'),
  ('host', 'HostAgent', 'Host Agent', 'host_mem_available_bytes',       'Average', 'Bytes',          60, 'extended', 'Memory available to new processes',            0, 0, 'aws'),
  ('host', 'HostAgent', 'Host Agent', 'host_swap_used_percent',         'Average', 'Percent',        60, 'extended', 'Swap space in use',                             0, 0, 'aws'),
  ('host', 'HostAgent', 'Host Agent', 'host_disk_inodes_used_percent',  'Average', 'Percent',        60, 'extended', 'Inodes in use per mount (series suffixed __<mount>, root unsuffixed)', 0, 0, 'aws'),
  ('host', 'HostAgent', 'Host Agent', 'host_disk_read_bytes_per_s',     'Average', 'Bytes/Second',   60, 'extended', 'Disk read throughput (rate computed by the agent)',  0, 0, 'aws'),
  ('host', 'HostAgent', 'Host Agent', 'host_disk_write_bytes_per_s',    'Average', 'Bytes/Second',   60, 'extended', 'Disk write throughput (rate computed by the agent)', 0, 0, 'aws'),
  ('host', 'HostAgent', 'Host Agent', 'host_net_rx_bytes_per_s',        'Average', 'Bytes/Second',   60, 'extended', 'Network receive rate per interface (series suffixed __<iface>)',  0, 0, 'aws'),
  ('host', 'HostAgent', 'Host Agent', 'host_net_tx_bytes_per_s',        'Average', 'Bytes/Second',   60, 'extended', 'Network transmit rate per interface (series suffixed __<iface>)', 0, 0, 'aws'),
  ('host', 'HostAgent', 'Host Agent', 'host_proc_count',                'Average', 'Count',          60, 'extended', 'Number of processes',                           0, 0, 'aws'),
  ('host', 'HostAgent', 'Host Agent', 'host_uptime_seconds',            'Average', 'Seconds',        60, 'extended', 'Seconds since boot',                            0, 0, 'aws');

-- Default thresholds, DISABLED, for existing accounts, resource_type 'host'.
INSERT IGNORE INTO thresholds
    (aws_account_id, resource_type, metric_id, warning_value, critical_value, comparison, evaluation_period, enabled)
SELECT a.id, 'host', mc.id, 85, 95, '>', 5, 0
FROM aws_accounts a JOIN metric_catalog mc
  ON mc.namespace = 'HostAgent' AND mc.metric_name = 'host_cpu_percent';

INSERT IGNORE INTO thresholds
    (aws_account_id, resource_type, metric_id, warning_value, critical_value, comparison, evaluation_period, enabled)
SELECT a.id, 'host', mc.id, 50, 80, '>', 5, 0
FROM aws_accounts a JOIN metric_catalog mc
  ON mc.namespace = 'HostAgent' AND mc.metric_name = 'host_swap_used_percent';

INSERT IGNORE INTO thresholds
    (aws_account_id, resource_type, metric_id, warning_value, critical_value, comparison, evaluation_period, enabled)
SELECT a.id, 'host', mc.id, 80, 90, '>', 5, 0
FROM aws_accounts a JOIN metric_catalog mc
  ON mc.namespace = 'HostAgent' AND mc.metric_name = 'host_disk_inodes_used_percent';
