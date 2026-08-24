# app/collector/multicloud_scheduler.py
"""
Background loop that periodically calls the Azure and GCP metrics
collectors (app/providers/{azure,gcp}/metrics_collector.py) and pushes
results into VictoriaMetrics.

This is intentionally a single flat interval, not the AWS tiered
critical/standard/low split in app/collector/scheduler.py. That tiering
exists specifically to cut CloudWatch GetMetricData call *volume* because
AWS bills per call. Azure Monitor and GCP Cloud Monitoring platform-metric
reads are free (see the cost-note docstrings in each collector module) --
there's no billing reason to tier them apart, so one interval keeps this
simpler until real usage data says otherwise.

Default interval matches the AWS "standard" tier (5 min) as a reasonable
middle ground: tight enough to be useful, loose enough not to hammer
either cloud's API rate limits across ~20 accounts.
"""
import time
import logging
import threading

logger = logging.getLogger(__name__)

_stop_event = threading.Event()

INTERVAL_SECONDS = 300  # 5 min


def run_once():
    from app.providers.azure.metrics_collector import collect_all_azure_accounts
    from app.providers.gcp.metrics_collector import collect_all_gcp_accounts

    try:
        azure_result = collect_all_azure_accounts()
        logger.info(
            f"[multicloud] Azure: {azure_result['accounts']} account(s), "
            f"{azure_result['pushed']} datapoints pushed"
            + (f", {len(azure_result['errors'])} account(s) had errors" if azure_result["errors"] else "")
        )
    except Exception as e:
        logger.error(f"[multicloud] Azure collection cycle crashed: {e}")

    try:
        gcp_result = collect_all_gcp_accounts()
        logger.info(
            f"[multicloud] GCP: {gcp_result['accounts']} account(s), "
            f"{gcp_result['pushed']} datapoints pushed"
            + (f", {len(gcp_result['errors'])} account(s) had errors" if gcp_result["errors"] else "")
        )
    except Exception as e:
        logger.error(f"[multicloud] GCP collection cycle crashed: {e}")


def run_loop(interval: int = INTERVAL_SECONDS):
    logger.info(f"Multi-cloud (Azure/GCP) metrics scheduler started (interval={interval}s)")
    while not _stop_event.is_set():
        run_once()
        _stop_event.wait(interval)


def stop():
    _stop_event.set()
