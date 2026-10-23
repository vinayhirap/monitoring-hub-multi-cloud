# app/hostagents/prefer.py
"""
HOST_AGENT_PREFER_OVER_CWAGENT: skip the PAID CWAgent GetMetricData query for a (resource, metric) a live host agent
already supplies.

How "the agent supplies it" is decided, and why it is NOT read from the `metrics` table
  The CWAgent collector writes the very same metric names (mem_used_percent, disk_used_percent[__slug]) into the same
  `metrics` row, so a fresh `metrics.metric_timestamp` cannot tell "agent fed it" from "CWAgent fed it" -- a heartbeat-only
  agent would then alternate between suppressing and resuming the CWAgent fetch. Instead every accepted NON-heartbeat
  ingest stores a small marker in app/utils/shared_cache (Redis, in-process fallback) keyed by resources.id, listing which
  of those names the payload carried, with a TTL of HOST_AGENT_STALE_SECONDS:

      hostagent:feed:<resources.id>  ->  {"names": ["mem_used_percent", "disk_used_percent", ...]}

  Marker present  => the agent delivered that metric within the stale window => the CWAgent fetch for it is skipped.
  Marker expired  => the agent stopped (or never sent that metric)           => the CWAgent fetch resumes on the next
                     cycle, and its look-back (mem 10 min, disk 20 min, polling_model.py) back-fills the gap.
  Redis down / multi-process in-memory fallback => marker invisible to the collector => nothing is skipped (fails SAFE:
                     costs money, never loses data).

With the flag off every function here returns its input untouched without reading the cache: polling is unchanged.

Estimated savings (see docs/HOST_AGENT_METRICS.md): CloudWatch bills GetMetricData per metric requested ($0.01 / 1000).
mem = 1 metric per instance per standard tick (288/day); disk = 1 metric per mount per low tick (96/day). note_skipped()
logs what each cycle avoided.
"""
import logging

from app.hostagents import settings
from app.utils import shared_cache

logger = logging.getLogger(__name__)

KEY_PREFIX = "hostagent:feed:"
MEM_NAME = "mem_used_percent"
DISK_NAME = "disk_used_percent"          # also matches disk_used_percent__<slug>
USD_PER_METRIC_REQUEST = 0.01 / 1000.0


def feed_key(resource_db_id) -> str:
    return f"{KEY_PREFIX}{int(resource_db_id)}"


def _is_cwagent_name(name: str) -> bool:
    return name == MEM_NAME or name == DISK_NAME or name.startswith(DISK_NAME + "__")


def mark_feed(resource_db_id, stored_names) -> bool:
    """Called after an accepted ingest commits. No-op unless the preference flag is on (flag-off ingest does zero extra
    work) or the payload carried no mem/disk series. True when a marker was stored."""
    if not settings.prefer_over_cwagent():
        return False
    names = sorted({n for n in stored_names if _is_cwagent_name(n)})
    if not names:
        return False
    try:
        return bool(shared_cache.put(feed_key(resource_db_id), {"names": names}, settings.stale_seconds()))
    except Exception as e:                                   # a cache problem must never fail an ingest
        logger.warning("host agent feed marker not stored: %s", e)
        return False


def fed_names(resource_db_id) -> frozenset:
    """Stored metric names a live agent delivered for this resource within the stale window (empty when none/unknown)."""
    try:
        res = shared_cache.peek(feed_key(resource_db_id))
    except Exception:
        return frozenset()
    if res is None or not isinstance(res.value, dict) or res.age_s >= settings.stale_seconds():
        return frozenset()
    return frozenset(n for n in (res.value.get("names") or []) if isinstance(n, str))


def drop_resources_with_fed_metric(resources, metric_name=MEM_NAME):
    """-> (kept, skipped_count). Flag off: (resources, 0) -- the same list object, nothing read."""
    if not settings.prefer_over_cwagent():
        return resources, 0
    kept, skipped = [], 0
    for r in resources:
        if metric_name in fed_names(r["id"]):
            skipped += 1
        else:
            kept.append(r)
    return kept, skipped


def covered_disk_names(resource_db_id) -> frozenset:
    """disk_used_percent[__slug] names the agent feeds for this resource. Flag off: empty, nothing read."""
    if not settings.prefer_over_cwagent():
        return frozenset()
    return frozenset(n for n in fed_names(resource_db_id) if n == DISK_NAME or n.startswith(DISK_NAME + "__"))


def estimated_saving_usd(mem_queries: int, disk_queries: int) -> float:
    return (int(mem_queries) + int(disk_queries)) * USD_PER_METRIC_REQUEST


def note_skipped(kind: str, queries: int, instances: int = 0) -> None:
    """One INFO line per cycle (only when something was skipped) so the saving is visible in the journal."""
    if queries <= 0:
        return
    logger.info("host agent preferred over CWAgent %s: skipped %d GetMetricData queries%s (~$%.6f this cycle)",
                kind, queries, f" for {instances} instances" if instances else "",
                estimated_saving_usd(queries if kind == "mem" else 0, queries if kind == "disk" else 0))
