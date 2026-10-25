# app/hostagents/catalog.py
"""
Metric catalog + threshold registration for host-agent signals, so every metric an agent sends is selectable under
Settings -> Metrics to Monitor and editable under Settings -> Metric Thresholds exactly like an EC2 / CWAgent one.

Why this exists on top of migration 122
  * 122 only seeded accounts that existed when it ran, and only 3 of the 13 threshold rows, all for resource_type 'host'.
  * A host attached to a discovered EC2 instance stores its readings under resource_type 'ec2', and Settings (and the alert
    evaluator) join thresholds on (resource_type, metric): a 'host' threshold row never matched such a host, so its metrics
    were hidden in Settings ("no data") even while charts showed data.
So on the first report that stores samples, the account gets a catalog row and a threshold row for every metric under the
resource type those samples were written to. Per-mount / per-interface series (host_disk_inodes_used_percent__boot,
host_net_rx_bytes_per_s__ens5) clone the base row, the same convention app/collector/disk_mounts.py uses for disk mounts.

Rules
  * Idempotent: INSERT IGNORE on the catalog's UNIQUE (provider, namespace, metric_name) and the thresholds' UNIQUE
    (aws_account_id, resource_type, metric_id). A threshold a person already edited is never touched.
  * Thresholds are created DISABLED (enabled = 0), like migration 122: alerting on a new signal is a deliberate act in
    Settings, never a side effect of installing an agent. Values come from threshold_defaults.DEFAULT_THRESHOLDS.
  * Never fails a request: called after the ingest commit, every error is logged and swallowed, and a database outage
    while registering is caught too (the connection is taken inside the try).
  * Once per process per (account, resource_type) and per series name; a restart just repeats the idempotent inserts.
"""
import logging

from app.db import get_connection
from app.hostagents import metric_map
from app.threshold_defaults import DEFAULT_THRESHOLDS, PLACEHOLDER_THRESHOLD

logger = logging.getLogger(__name__)

NAMESPACE = "HostAgent"
SERVICE = "host"
DISPLAY_SERVICE = "Host Agent"

# stored base name -> (statistic unit, description). Same rows as migration 122.
CATALOG = {
    "host_cpu_percent":              ("Percent",      "Guest OS CPU utilization reported by the host agent (not hypervisor CPUUtilization)"),
    "host_load1":                    ("Count",        "1-minute load average"),
    "host_load5":                    ("Count",        "5-minute load average"),
    "host_load15":                   ("Count",        "15-minute load average"),
    "host_mem_available_bytes":      ("Bytes",        "Memory available to new processes"),
    "host_swap_used_percent":        ("Percent",      "Swap space in use"),
    "host_disk_inodes_used_percent": ("Percent",      "Inodes in use per mount (series suffixed __<mount>, root unsuffixed)"),
    "host_disk_read_bytes_per_s":    ("Bytes/Second", "Disk read throughput (rate computed by the agent)"),
    "host_disk_write_bytes_per_s":   ("Bytes/Second", "Disk write throughput (rate computed by the agent)"),
    "host_net_rx_bytes_per_s":       ("Bytes/Second", "Network receive rate per interface (series suffixed __<iface>)"),
    "host_net_tx_bytes_per_s":       ("Bytes/Second", "Network transmit rate per interface (series suffixed __<iface>)"),
    "host_proc_count":               ("Count",        "Number of processes"),
    "host_uptime_seconds":           ("Seconds",      "Seconds since boot"),
}

_DONE = set()          # (account_id, resource_type) whose base rows this process already registered
_SERIES_DONE = set()   # (account_id, resource_type, stored series name)

SQL_CATALOG = (
    "INSERT IGNORE INTO metric_catalog (service, namespace, display_service, metric_name, statistic, unit, "
    "default_interval, category, description, is_default, enabled, provider) "
    "VALUES (%s, %s, %s, %s, 'Average', %s, 60, 'extended', %s, 0, 0, 'aws')")
SQL_CATALOG_CLONE = (
    "INSERT IGNORE INTO metric_catalog (service, namespace, display_service, metric_name, statistic, unit, "
    "default_interval, category, description, is_default, enabled, provider) "
    "SELECT service, namespace, display_service, %s, statistic, unit, default_interval, category, %s, 0, 0, provider "
    "FROM metric_catalog WHERE namespace = %s AND metric_name = %s LIMIT 1")
SQL_CATALOG_ID = "SELECT id FROM metric_catalog WHERE namespace = %s AND metric_name = %s LIMIT 1"
SQL_THRESHOLD = (
    "INSERT IGNORE INTO thresholds (aws_account_id, resource_type, metric_id, warning_value, critical_value, "
    "comparison, evaluation_period, enabled) VALUES (%s, %s, %s, %s, %s, %s, 5, 0)")


def default_for(base: str):
    """(warning, critical, comparison) for a stored base name; the blank placeholder when none is defined."""
    return DEFAULT_THRESHOLDS.get(base) or PLACEHOLDER_THRESHOLD


def series_base(stored: str):
    """'host_net_rx_bytes_per_s__ens5' -> 'host_net_rx_bytes_per_s'; a plain base -> None (already registered); anything
    that is not a labelled host-agent series -> None. Pure."""
    if not isinstance(stored, str) or "__" not in stored:
        return None
    base = stored.split("__", 1)[0]
    if base not in CATALOG:
        return None
    contract = next((c for c, spec in metric_map.METRICS.items() if spec[0] == base), None)
    if contract is None or metric_map.METRICS[contract][1] is None:
        return None
    return base


def _register_base(cur, account_id, resource_type):
    for name, (unit, desc) in CATALOG.items():
        cur.execute(SQL_CATALOG, (SERVICE, NAMESPACE, DISPLAY_SERVICE, name, unit, desc))
        cur.execute(SQL_CATALOG_ID, (NAMESPACE, name))
        row = cur.fetchone()
        if not row:
            continue
        w, c, comp = default_for(name)
        cur.execute(SQL_THRESHOLD, (account_id, resource_type, row["id"], w, c, comp))


def _register_series(cur, account_id, resource_type, stored, base):
    suffix = stored[len(base) + 2:]
    cur.execute(SQL_CATALOG_CLONE, (stored, f"{CATALOG[base][1].split(' (')[0]} -- {suffix}", NAMESPACE, base))
    cur.execute(SQL_CATALOG_ID, (NAMESPACE, stored))
    row = cur.fetchone()
    if not row:
        return
    w, c, comp = default_for(base)
    cur.execute(SQL_THRESHOLD, (account_id, resource_type, row["id"], w, c, comp))


def ensure_host_metrics_registered(account_id, resource_type, stored_names=()):
    """Register the base catalog + thresholds for (account, resource_type) and any labelled series in `stored_names`.
    Returns True when it did work or had nothing to do, False when it failed (already logged). Never raises."""
    base_todo = (account_id, resource_type) not in _DONE
    series_todo = {}
    for n in stored_names:
        b = series_base(n)
        if b and (account_id, resource_type, n) not in _SERIES_DONE:
            series_todo[n] = b
    if not base_todo and not series_todo:
        return True
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        try:
            if base_todo:
                _register_base(cur, account_id, resource_type)
            for stored, base in series_todo.items():
                _register_series(cur, account_id, resource_type, stored, base)
            conn.commit()
        finally:
            cur.close()
        if base_todo:
            _DONE.add((account_id, resource_type))
        for n in series_todo:
            _SERIES_DONE.add((account_id, resource_type, n))
        return True
    except Exception as e:
        logger.warning("host agent catalog registration failed [account %s, %s]: %s", account_id, resource_type, e)
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
