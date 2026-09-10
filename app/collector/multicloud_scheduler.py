# app/collector/multicloud_scheduler.py
"""
Background loop that periodically calls the Azure and GCP metrics
collectors (app/providers/{azure,gcp}/metrics_collector.py) and writes
results directly to the local DB (Phase 2/3 -- no longer via VictoriaMetrics,
see each collector's own docstring).

Two-tier priority-based cadence (see monitoring-hub-metric-audit.md §8 flaw
#3, §9 -- added after the original flat-5-min design; kept deliberately
DIFFERENT from AWS's critical/standard/low split rather than copying it,
because the two clouds are tiered for different reasons here:

  - AWS tiers to cut CloudWatch GetMetricData call *volume*, because AWS
    bills every call regardless of the underlying metric's own cost.
  - Azure platform-metric reads are free up to 1,000,000 API calls/month/
    billing account (Microsoft-confirmed) -- tiering here isn't chasing a
    current bill, it's protecting headroom under that ceiling as more
    extended services get enabled.
  - GCP's read API bills per TIME SERIES RETURNED (not per call), effective
    Oct 2, 2025 pricing change: $0.50/million series above the first
    1,000,000 free/billing-account/month. That meter scales with
    (metrics enabled) x (resources of that type) because list_time_series()
    is fleet-wide per metric type -- tiering here directly slows growth
    toward a real, if currently small, cost line.

CORE tier   -- 5 min  -- the services this app's own resource collectors
                          already treat as primary (Azure: VM, Storage
                          Account, SQL Database, App Service; GCP: Compute
                          Engine, Cloud Storage, Cloud SQL, Cloud Run).
                          category='core' in metric_catalog.
EXTENDED tier -- 15 min -- everything else the user has enabled (VMSS, AKS,
                          Cosmos DB, Redis, GKE, Pub/Sub, etc. -- and any
                          DIRECTORY-tier metric a user has manually enabled
                          via "Discover", which behaves like 'extended' for
                          cadence purposes once selected).
                          category IN ('extended','directory').

This mirrors the PRIORITY of AWS's split (fast for what's latency-
sensitive, slower for trend/low-priority) without assuming Azure/GCP's
publication cadence or billing shape matches AWS's -- both clouds' core
platform metrics publish at 1-min resolution (Microsoft/Google-confirmed),
well within a 5-min poll's ability to always see fresh data, and none of
this tiering trades away freshness on any metric this app treats as
alertable/critical.
"""
import time
import logging
import threading

logger = logging.getLogger(__name__)

_stop_event = threading.Event()

CORE_INTERVAL_SECONDS     = 300   # 5 min  -- VM/Compute/SQL/Storage/Run/App Service
EXTENDED_INTERVAL_SECONDS = 900   # 15 min -- everything else enabled

# Kept for any external caller still importing the old flat-interval name.
INTERVAL_SECONDS = CORE_INTERVAL_SECONDS


def run_once(categories=None):
    """Run one collection cycle for Azure + GCP, restricted to `categories`
    if given (e.g. ("core",) or ("extended","directory")). categories=None
    collects everything enabled, in one pass -- used by run() below for a
    single manual/standalone cycle."""
    from app.providers.azure.metrics_collector import collect_all_azure_accounts
    from app.providers.gcp.metrics_collector import collect_all_gcp_accounts

    tier_label = "+".join(categories) if categories else "all"

    try:
        azure_result = collect_all_azure_accounts(categories=categories)
        logger.info(
            f"[multicloud:{tier_label}] Azure: {azure_result['accounts']} account(s), "
            f"{azure_result['pushed']} datapoints written directly (Phase 2 -- no longer via VM)"
            + (f", {len(azure_result['errors'])} account(s) had errors" if azure_result["errors"] else "")
        )
    except Exception as e:
        logger.error(f"[multicloud:{tier_label}] Azure collection cycle crashed: {e}")

    try:
        gcp_result = collect_all_gcp_accounts(categories=categories)
        logger.info(
            f"[multicloud:{tier_label}] GCP: {gcp_result['accounts']} account(s), "
            f"{gcp_result['pushed']} datapoints written directly (Phase 3 -- no longer via VM)"
            + (f", {len(gcp_result['errors'])} account(s) had errors" if gcp_result["errors"] else "")
        )
    except Exception as e:
        logger.error(f"[multicloud:{tier_label}] GCP collection cycle crashed: {e}")


def run_loop(core_interval: int = CORE_INTERVAL_SECONDS, extended_interval: int = EXTENDED_INTERVAL_SECONDS):
    logger.info(
        f"Multi-cloud (Azure/GCP) metrics scheduler started "
        f"(core={core_interval}s, extended={extended_interval}s)"
    )
    last_extended = 0.0
    while not _stop_event.is_set():
        now = time.time()

        run_once(categories=("core",))

        if now - last_extended >= extended_interval:
            run_once(categories=("extended", "directory"))
            last_extended = now

        elapsed = time.time() - now
        sleep = max(0, core_interval - elapsed)
        _stop_event.wait(timeout=sleep)


def stop():
    _stop_event.set()
