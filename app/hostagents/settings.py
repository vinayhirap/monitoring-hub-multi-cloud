# app/hostagents/settings.py
"""
Environment-driven settings for the host agent feature. Read at call time (never cached at import) so a test or an
operator restart picks up the current environment. Dependency-free on purpose: other phases import from here.
"""
import os

_TRUE = ("true", "1", "yes", "on")

DEFAULT_CONFIG = {
    "interval_seconds": 60,
    "watch_services": [],
    "disk_exclude_fstypes": ["tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs"],
    "max_batch_metrics": 500,
}
INTERVAL_MIN_SECONDS = 15
INTERVAL_MAX_SECONDS = 300


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def enabled() -> bool:
    """HOST_AGENT_ENABLED, default false."""
    return os.getenv("HOST_AGENT_ENABLED", "false").strip().lower() in _TRUE


def stale_seconds() -> int:
    return _int_env("HOST_AGENT_STALE_SECONDS", 180)


def max_body_bytes() -> int:
    return _int_env("HOST_AGENT_MAX_BODY_BYTES", 262144)


def ingest_rate_per_min() -> int:
    return _int_env("HOST_AGENT_INGEST_RATE_PER_MIN", 30)


def enroll_rate_per_hour() -> int:
    return _int_env("HOST_AGENT_ENROLL_RATE_PER_HOUR", 20)
