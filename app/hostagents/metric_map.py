# app/hostagents/metric_map.py
"""
Ingest payload v1 validation + the CONTRACT-name -> stored-name mapping. Pure: no database, no network, so the whole
validation matrix is unit-testable. Mapping rationale is documented in docs/HOST_AGENT_METRICS.md.

Stored names:
  * mem_used_percent, disk_used_percent (+ __<slug> for non-root mounts) are the names the CWAgent collector already
    writes (app/collector/metrics/runner.py, app/collector/disk_mounts.py), so alerting, thresholds, history charts and
    the evaluator join (metrics.metric_name = metric_catalog.metric_name) treat an agent reading exactly like a CWAgent one.
  * every other signal has NO existing counterpart (or a different meaning: guest CPU is not hypervisor CPUUtilization)
    and gets a `host_` name, so it can never be interleaved with a CloudWatch-sourced series of the same name.
"""
import math
import re
import time

SCHEMA_VERSION = 1

# Same shapes app/collector/disk_mounts.py uses (ROOT_PATHS, slugify_mount_path, IGNORED_*). Duplicated on purpose so this
# module stays import-free; tests/test_host_agent_ingest.py asserts they agree with disk_mounts.
ROOT_PATHS = ("/", "C:")
BASE_DISK_METRIC = "disk_used_percent"
_IGNORED_PATH_PREFIXES = ("/snap/", "/var/snap/", "/run", "/dev", "/proc", "/sys", "/boot/efi")

MAX_SLUG_LEN = 60                  # metrics.metric_name is VARCHAR(100)
MAX_LABEL_VALUE_LEN = 255
MAX_SERIES_PER_METRIC = 32         # distinct mounts / interfaces accepted per metric name per payload
MAX_SERVICES = 200
MAX_SERVICE_NAME_LEN = 100
FUTURE_SKEW_SECONDS = 10 * 60
MAX_AGE_SECONDS = 24 * 3600
SERVICE_STATES = ("running", "stopped", "failed", "unknown")

# contract name -> (stored base name, label key or None, kind)   kind: percent (0-100) | nonneg (>= 0)
METRICS = {
    "cpu_percent":               ("host_cpu_percent",               None,    "percent"),
    "load1":                     ("host_load1",                     None,    "nonneg"),
    "load5":                     ("host_load5",                     None,    "nonneg"),
    "load15":                    ("host_load15",                    None,    "nonneg"),
    "mem_used_percent":          ("mem_used_percent",               None,    "percent"),
    "mem_available_bytes":       ("host_mem_available_bytes",       None,    "nonneg"),
    "swap_used_percent":         ("host_swap_used_percent",         None,    "percent"),
    "disk_used_percent":         (BASE_DISK_METRIC,                 "mount", "percent"),
    "disk_inodes_used_percent":  ("host_disk_inodes_used_percent",  "mount", "percent"),
    "disk_read_bytes_per_s":     ("host_disk_read_bytes_per_s",     None,    "nonneg"),
    "disk_write_bytes_per_s":    ("host_disk_write_bytes_per_s",    None,    "nonneg"),
    "net_rx_bytes_per_s":        ("host_net_rx_bytes_per_s",        "iface", "nonneg"),
    "net_tx_bytes_per_s":        ("host_net_tx_bytes_per_s",        "iface", "nonneg"),
    "proc_count":                ("host_proc_count",                None,    "nonneg"),
    "uptime_seconds":            ("host_uptime_seconds",            None,    "nonneg"),
}

# Stored base names an agent can feed, grouped by the paid CWAgent / CloudWatch fetch they could replace.
CWAGENT_MEM_NAMES = ("mem_used_percent",)


class PayloadError(Exception):
    """The payload as a whole is unusable. `status` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def slugify(value: str) -> str:
    """Same algorithm as disk_mounts.slugify_mount_path, then capped."""
    s = (value or "").strip().strip("/").replace(":", "").replace("\\", "_").replace("/", "_")
    s = re.sub(r"[^a-zA-Z0-9_]", "_", s).strip("_").lower()
    return (s or "root")[:MAX_SLUG_LEN]


def is_real_mount(path: str) -> bool:
    """Root is always real; pseudo/ephemeral paths are not (the agent already drops pseudo fstypes, this is the second
    independent check disk_mounts.py also keeps, since the agent only sends the mount path)."""
    if path in ROOT_PATHS:
        return True
    return not any(path == p or path.startswith(p) for p in _IGNORED_PATH_PREFIXES)


def stored_name(contract_name: str, label_value):
    """-> stored metric_name for a contract metric + its label value (None when the metric has no label)."""
    base, label_key, _kind = METRICS[contract_name]
    if label_key is None:
        return base
    if label_key == "mount":
        if label_value in ROOT_PATHS:
            return base
        return f"{base}__{slugify(label_value)}"
    return f"{base}__{slugify(label_value)}"           # iface


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_payload(body, *, max_batch_metrics: int, now: float = None):
    """Validate a decoded ingest body.

    Returns (clean, rejected):
      clean    {"agent_version", "config_version", "seq", "sent_at", "host": {...},
                "samples": [(stored_name, contract_name, label_value, value, ts_epoch)],  # per-sample valid only
                "services": [(name, state)]}
      rejected number of metric samples / service entries dropped for per-item reasons.
    Raises PayloadError for anything that makes the payload as a whole unusable (400 / 413)."""
    now = time.time() if now is None else now
    if not isinstance(body, dict):
        raise PayloadError(400, "body must be a JSON object")
    schema = body.get("schema")
    if not _is_int(schema) or schema != SCHEMA_VERSION:
        raise PayloadError(400, f"unsupported schema (expected {SCHEMA_VERSION})")

    seq = body.get("seq")
    if not _is_int(seq) or seq < 0:
        raise PayloadError(400, "seq must be a non-negative integer")
    config_version = body.get("config_version")
    if not _is_int(config_version) or config_version < 0:
        raise PayloadError(400, "config_version must be a non-negative integer")
    sent_at = body.get("sent_at")
    if not _is_int(sent_at) or sent_at <= 0:
        raise PayloadError(400, "sent_at must be epoch seconds")
    agent_version = body.get("agent_version")
    if not isinstance(agent_version, str) or not agent_version or len(agent_version) > 40:
        raise PayloadError(400, "agent_version must be a string of 1-40 characters")

    host = body.get("host")
    if not isinstance(host, dict):
        raise PayloadError(400, "host must be an object")
    hostname = host.get("hostname")
    if not isinstance(hostname, str) or not hostname or len(hostname) > 255:
        raise PayloadError(400, "host.hostname must be a string of 1-255 characters")
    instance_id = host.get("instance_id")
    if instance_id is not None and (not isinstance(instance_id, str) or not instance_id or len(instance_id) > 64):
        raise PayloadError(400, "host.instance_id must be null or a string of 1-64 characters")
    for key in ("os", "arch"):
        v = host.get(key)
        if v is not None and (not isinstance(v, str) or len(v) > 40):
            raise PayloadError(400, f"host.{key} must be a string of at most 40 characters")

    metrics = body.get("metrics")
    services = body.get("services")
    if not isinstance(metrics, list) or not isinstance(services, list):
        raise PayloadError(400, "metrics and services must be arrays")
    if len(metrics) > max_batch_metrics:
        raise PayloadError(413, f"too many metrics (max {max_batch_metrics})")
    if len(services) > MAX_SERVICES:
        raise PayloadError(413, f"too many services (max {MAX_SERVICES})")

    rejected = 0
    samples = []
    series_seen = {}                                   # contract name -> set(label values)
    lo, hi = now - MAX_AGE_SECONDS, now + FUTURE_SKEW_SECONDS
    for m in metrics:
        if not isinstance(m, dict):
            rejected += 1
            continue
        name = m.get("name")
        spec = METRICS.get(name) if isinstance(name, str) else None
        if spec is None:
            rejected += 1
            continue
        _base, label_key, kind = spec
        value, ts = m.get("value"), m.get("ts")
        if not _is_number(value) or not math.isfinite(value):
            rejected += 1
            continue
        if kind == "percent" and not (0 <= value <= 100):
            rejected += 1
            continue
        if kind == "nonneg" and value < 0:
            rejected += 1
            continue
        if not _is_number(ts) or not math.isfinite(ts) or not (lo <= ts <= hi):
            rejected += 1
            continue
        label_value = None
        if label_key is not None:
            labels = m.get("labels")
            label_value = labels.get(label_key) if isinstance(labels, dict) else None
            if not isinstance(label_value, str) or not label_value or len(label_value) > MAX_LABEL_VALUE_LEN:
                rejected += 1
                continue
            if label_key == "mount" and not is_real_mount(label_value):
                rejected += 1
                continue
            seen = series_seen.setdefault(name, set())
            if label_value not in seen and len(seen) >= MAX_SERIES_PER_METRIC:
                rejected += 1
                continue
            seen.add(label_value)
        samples.append((stored_name(name, label_value), name, label_value, float(value), int(ts)))

    clean_services = []
    for s in services:
        if (not isinstance(s, dict) or not isinstance(s.get("name"), str) or not s["name"]
                or len(s["name"]) > MAX_SERVICE_NAME_LEN or s.get("state") not in SERVICE_STATES):
            rejected += 1
            continue
        clean_services.append((s["name"], s["state"]))

    return {
        "agent_version": agent_version, "config_version": config_version, "seq": seq, "sent_at": sent_at,
        "host": {"hostname": hostname, "instance_id": instance_id, "os": host.get("os"), "arch": host.get("arch")},
        "samples": samples, "services": clean_services,
    }, rejected


def newest_per_series(samples):
    """{stored_name: (value, ts)} holding the newest sample of every stored series (the last-value cache row)."""
    latest = {}
    for stored, _contract, _label, value, ts in samples:
        cur = latest.get(stored)
        if cur is None or ts >= cur[1]:
            latest[stored] = (value, ts)
    return latest
