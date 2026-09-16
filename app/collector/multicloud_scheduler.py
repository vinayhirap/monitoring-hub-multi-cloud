# app/collector/multicloud_scheduler.py
"""
Background loop that periodically calls the Azure and GCP metrics
collectors (app/providers/{azure,gcp}/metrics_collector.py) and writes
results directly to the local DB (Phase 2/3 -- no longer via VictoriaMetrics,
see each collector's own docstring).

Severity-tiered cadence (2026-09-15 patch -- see monitoring-hub-metric-audit.md
§8 flaw #3, §9 for the original core/extended split this replaces). Kept
DIFFERENT from AWS's critical/standard/low split rather than copied
verbatim, because Azure and GCP are tiered for different reasons here, and
different from EACH OTHER for the same reason:

  - AWS tiers to cut CloudWatch GetMetricData call *volume*, because AWS
    bills every call regardless of the underlying metric's own cost.
  - Azure platform-metric reads are free up to 1,000,000 API calls/month/
    billing account (Microsoft-confirmed) -- there's no bill to protect at
    this app's scale, so Azure's CRITICAL tier is tuned for FRESHNESS:
    it polls at ~1 min, matching Azure Monitor's own real publish rate for
    these metrics (see the workbook's 'Real Publish Rate' column), instead
    of waiting out a flat 5-min cycle for no reason. Slower tiers exist to
    protect headroom under that 1M/month ceiling as more services get
    enabled, and to avoid polling latency-insensitive services as often as
    latency-sensitive ones for no freshness benefit.
  - GCP's read API bills per TIME SERIES RETURNED (not per call), effective
    Oct 2, 2025 pricing change: $0.50/million series above the first
    1,000,000 free/billing-account/month. That meter scales with
    (metrics enabled) x (resources of that type) because list_time_series()
    is fleet-wide per metric type -- this is a real, cost-bearing meter the
    same way AWS's is, so GCP's tiers are tuned for COST like AWS's,
    inheriting AWS's own interval choices (2/5/15/60 min) rather than
    Azure's freshness-driven ones.

Per-cloud tiers (see app/providers/{azure,gcp}/severity_tiers.py for the
exact per-metric allowlists):

  Azure:
    CRITICAL   --  1 min -- alertable core signals (VM CPU/availability,
                             SQL DB CPU/failed-connections, App Service
                             5xx/health) -- matches Azure's real ~1-min
                             publish rate, free to do at this app's scale.
    STANDARD   --  5 min -- remaining core metrics (unchanged from the
                             previous flat core cadence).
    LOW        -- 15 min -- trend-only core metrics (disk bytes, credits,
                             queue depth, ingress/egress, latency detail).
    EXTENDED   -- 15 min -- extended-tier services NOT in
                             SLOW_EXTENDED_SERVICES (unchanged cadence).
    SLOW_EXT.  -- 60 min -- extended-tier services that either publish
                             slower than 1 min anyway (VPN Gateway,
                             Managed Disks: 5 min) or are bursty/
                             config-adjacent rather than steady (Key
                             Vault, CDN, Data Factory) -- 60 min loses no
                             freshness a user would notice.

  GCP:
    CRITICAL   --  2 min -- alertable core signals (Compute Engine CPU,
                             Cloud SQL CPU/up, Cloud Run CPU/latency) --
                             matches AWS's own critical-tier interval.
    STANDARD   --  5 min -- remaining core metrics (unchanged).
    LOW        -- 15 min -- trend-only core metrics (matches AWS's low tier).
    EXTENDED   -- 60 min -- ALL extended-tier services, slowed from 15 min
                             to 60 min (matches AWS's extended tier) --
                             the direct lever on the (metrics enabled) x
                             (resources) series-returned volume that's
                             actually billed.

Backward compatibility: run_loop(leader_event) with no further arguments
(the only call site today, app/main.py's _run_multicloud_collector) keeps
working unchanged -- all new interval/tier knobs have defaults matching
the policy above. run_once() also still exists, unchanged in shape, for
any manual/standalone single-cycle caller; it now runs every tier in one
untiered pass when categories=None, same behavior as before this patch.
"""
import time
import logging
import threading

from app.providers.azure.metric_catalog_data import CURATED as AZURE_CURATED
from app.providers.gcp.metric_catalog_data import CURATED as GCP_CURATED
from app.providers.azure import severity_tiers as azure_tiers
from app.providers.gcp import severity_tiers as gcp_tiers

logger = logging.getLogger(__name__)

_stop_event = threading.Event()

# ── Azure intervals (seconds) -- freshness-driven, free at this app's scale ──
AZURE_CRITICAL_INTERVAL_SECONDS = 60          #  1 min
AZURE_STANDARD_INTERVAL_SECONDS = 300         #  5 min (unchanged core cadence)
AZURE_LOW_INTERVAL_SECONDS = 900              # 15 min
AZURE_EXTENDED_INTERVAL_SECONDS = 900         # 15 min (unchanged for non-slow extended)
AZURE_SLOW_EXTENDED_INTERVAL_SECONDS = 3600   # 60 min

# ── GCP intervals (seconds) -- cost-driven, mirrors AWS's own tiering ──
GCP_CRITICAL_INTERVAL_SECONDS = 120           #  2 min (matches AWS critical)
GCP_STANDARD_INTERVAL_SECONDS = 300           #  5 min (unchanged core cadence)
GCP_LOW_INTERVAL_SECONDS = 900                # 15 min
GCP_EXTENDED_INTERVAL_SECONDS = 3600          # 60 min (matches AWS extended; was 15 min)

# Kept for any external caller still importing the old flat-interval names.
CORE_INTERVAL_SECONDS = AZURE_STANDARD_INTERVAL_SECONDS
EXTENDED_INTERVAL_SECONDS = AZURE_EXTENDED_INTERVAL_SECONDS
INTERVAL_SECONDS = CORE_INTERVAL_SECONDS

# Built once at import time from the curated catalogs -- see each
# severity_tiers.py module for what these actually contain.
_AZURE_CRITICAL = azure_tiers.CRITICAL_METRICS
_AZURE_STANDARD = azure_tiers.build_standard_metrics(AZURE_CURATED)
_AZURE_LOW = azure_tiers.LOW_METRICS
_AZURE_EXTENDED_FAST = azure_tiers.build_extended_fast_metrics(AZURE_CURATED)
_AZURE_EXTENDED_SLOW = azure_tiers.build_extended_slow_metrics(AZURE_CURATED)

_GCP_CRITICAL = gcp_tiers.CRITICAL_METRICS
_GCP_STANDARD = gcp_tiers.build_standard_metrics(GCP_CURATED)
_GCP_LOW = gcp_tiers.LOW_METRICS
_GCP_EXTENDED = gcp_tiers.build_extended_metrics(GCP_CURATED)


def _window_for(interval_seconds, buffer_seconds=60):
    """GetMetricData / Azure query_resources lookback window, sized to a
    tier's own poll interval plus a buffer -- NOT a fixed constant.

    2026-09-16 fix: both Azure's and GCP's collectors previously queried
    a hardcoded ~10-minute window regardless of which tier called them.
    That's fine for the fast core tiers (critical/standard, 1-5 min) but
    silently drops data for anything slower (LOW at 15 min, EXTENDED at
    15-60 min, Azure's SLOW_EXTENDED at 60 min): a window narrower than
    the gap between polls has no guarantee of overlapping whenever the
    cloud actually published a datapoint. Same bug class, same fix
    shape, as AWS's app/collector/metrics/extended.py's _LOOKBACK_MINUTES
    (see that dict's docstring for the fuller math on why this matters
    more the slower a tier polls)."""
    return interval_seconds + buffer_seconds


def _run_azure_pass(tier_label, only_metric_names, categories, window_seconds=600):
    from app.providers.azure.metrics_collector import collect_all_azure_accounts
    try:
        result = collect_all_azure_accounts(categories=categories, only_metric_names=only_metric_names,
                                             window_seconds=window_seconds)
        logger.info(
            f"[multicloud:azure:{tier_label}] {result['accounts']} account(s), "
            f"{result['pushed']} datapoints written directly"
            + (f", {len(result['errors'])} account(s) had errors" if result["errors"] else "")
        )
    except Exception as e:
        logger.error(f"[multicloud:azure:{tier_label}] collection cycle crashed: {e}")


def _run_gcp_pass(tier_label, only_metric_names, categories, window_seconds=600):
    from app.providers.gcp.metrics_collector import collect_all_gcp_accounts
    try:
        result = collect_all_gcp_accounts(categories=categories, only_metric_names=only_metric_names,
                                           window_seconds=window_seconds)
        logger.info(
            f"[multicloud:gcp:{tier_label}] {result['accounts']} account(s), "
            f"{result['pushed']} datapoints written directly"
            + (f", {len(result['errors'])} account(s) had errors" if result["errors"] else "")
        )
    except Exception as e:
        logger.error(f"[multicloud:gcp:{tier_label}] collection cycle crashed: {e}")


def run_once(categories=None):
    """Run one collection cycle for Azure + GCP, restricted to `categories`
    if given (e.g. ("core",) or ("extended","directory")). categories=None
    collects everything enabled, in one untiered pass -- used for a single
    manual/standalone cycle, same behavior as before this patch.

    window_seconds here is deliberately the widest of any configured
    tier (60 min + buffer): a standalone/manual call has no "last polled
    N seconds ago" context to size a tighter window from, and a wider
    window than strictly necessary only means re-reading a few already-
    seen datapoints, never missing new ones -- the safe direction to
    round to when the right answer is unknown."""
    tier_label = "all" if categories is None else "+".join(categories)
    window_seconds = _window_for(max(AZURE_SLOW_EXTENDED_INTERVAL_SECONDS, GCP_EXTENDED_INTERVAL_SECONDS))
    _run_azure_pass(tier_label, None, categories, window_seconds=window_seconds)
    _run_gcp_pass(tier_label, None, categories, window_seconds=window_seconds)


def run_loop(
    leader_event=None,
    azure_critical_interval: int = AZURE_CRITICAL_INTERVAL_SECONDS,
    azure_standard_interval: int = AZURE_STANDARD_INTERVAL_SECONDS,
    azure_low_interval: int = AZURE_LOW_INTERVAL_SECONDS,
    azure_extended_interval: int = AZURE_EXTENDED_INTERVAL_SECONDS,
    azure_slow_extended_interval: int = AZURE_SLOW_EXTENDED_INTERVAL_SECONDS,
    gcp_critical_interval: int = GCP_CRITICAL_INTERVAL_SECONDS,
    gcp_standard_interval: int = GCP_STANDARD_INTERVAL_SECONDS,
    gcp_low_interval: int = GCP_LOW_INTERVAL_SECONDS,
    gcp_extended_interval: int = GCP_EXTENDED_INTERVAL_SECONDS,
):
    """
    leader_event: see app/collector/scheduler.py's run_loop docstring --
    same leadership-loss guard, same reasoning (this loop is started
    under the identical leader-elected code path and was equally
    vulnerable to running forever as an orphaned second scheduler).

    The tick loop runs on Azure's critical interval (the fastest configured
    cadence across both clouds) and fires every other tier only once its
    own interval has elapsed since it last ran -- same "fast tick, slower
    tiers gated by elapsed time" structure app/collector/scheduler.py uses
    for AWS's critical/standard/low/extended/slow-extended split.
    """
    logger.info(
        "Multi-cloud (Azure/GCP) severity-tiered metrics scheduler started "
        f"(azure critical={azure_critical_interval}s standard={azure_standard_interval}s "
        f"low={azure_low_interval}s extended={azure_extended_interval}s "
        f"slow_extended={azure_slow_extended_interval}s; "
        f"gcp critical={gcp_critical_interval}s standard={gcp_standard_interval}s "
        f"low={gcp_low_interval}s extended={gcp_extended_interval}s)"
    )

    last = {
        "gcp_critical": 0.0, "azure_standard": 0.0, "azure_low": 0.0,
        "azure_extended": 0.0, "azure_slow_extended": 0.0,
        "gcp_standard": 0.0, "gcp_low": 0.0, "gcp_extended": 0.0,
    }

    while not _stop_event.is_set():
        if leader_event is not None and not leader_event.is_set():
            logger.warning("[multicloud-scheduler] leadership lost -- stopping this loop "
                            "(another worker is now the leader)")
            return

        now = time.time()

        # Azure critical runs every tick (Azure's tick rate = the fastest
        # configured interval across both clouds). GCP critical is gated
        # like every other tier below, since its own interval (2 min) is
        # slower than the tick rate (1 min).
        _run_azure_pass("critical", _AZURE_CRITICAL, ("core",), window_seconds=_window_for(azure_critical_interval))

        if now - last["gcp_critical"] >= gcp_critical_interval:
            _run_gcp_pass("critical", _GCP_CRITICAL, ("core",), window_seconds=_window_for(gcp_critical_interval))
            last["gcp_critical"] = now

        if now - last["azure_standard"] >= azure_standard_interval:
            _run_azure_pass("standard", _AZURE_STANDARD, ("core",), window_seconds=_window_for(azure_standard_interval))
            last["azure_standard"] = now

        if now - last["azure_low"] >= azure_low_interval:
            _run_azure_pass("low", _AZURE_LOW, ("core",), window_seconds=_window_for(azure_low_interval))
            last["azure_low"] = now

        if now - last["azure_extended"] >= azure_extended_interval:
            _run_azure_pass("extended", _AZURE_EXTENDED_FAST, ("extended", "directory"),
                             window_seconds=_window_for(azure_extended_interval))
            last["azure_extended"] = now

        if now - last["azure_slow_extended"] >= azure_slow_extended_interval:
            _run_azure_pass("slow_extended", _AZURE_EXTENDED_SLOW, ("extended",),
                             window_seconds=_window_for(azure_slow_extended_interval))
            last["azure_slow_extended"] = now

        if now - last["gcp_standard"] >= gcp_standard_interval:
            _run_gcp_pass("standard", _GCP_STANDARD, ("core",), window_seconds=_window_for(gcp_standard_interval))
            last["gcp_standard"] = now

        if now - last["gcp_low"] >= gcp_low_interval:
            _run_gcp_pass("low", _GCP_LOW, ("core",), window_seconds=_window_for(gcp_low_interval))
            last["gcp_low"] = now

        if now - last["gcp_extended"] >= gcp_extended_interval:
            _run_gcp_pass("extended", _GCP_EXTENDED, ("extended", "directory"),
                           window_seconds=_window_for(gcp_extended_interval))
            last["gcp_extended"] = now

        elapsed = time.time() - now
        sleep = max(0, azure_critical_interval - elapsed)
        _stop_event.wait(timeout=sleep)


def stop():
    _stop_event.set()
