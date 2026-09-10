# app/collector/scheduler.py
"""
Tiered scheduler — Phase 2 implementation.

  critical  — every 2 min  : EC2 CPU/Network, RDS, ELB
  standard  — every 5 min  : RDS (still, every cycle) + EBS + Lambda Errors
  low       — every 15 min : EC2 Disk, EC2 CWAgent mem/disk, Lambda
                              Invocations, extended-tier services

Note (post metric_audit.md §8/§10 fix): EC2 CPU/Network and ELB were
previously ALSO re-triggered on the "standard" tier (dispatch condition
was `tier in ("critical","standard")`), which meant that on every cycle
where "standard" happened to fire (every 5 min), those metrics were
requested via GetMetricData TWICE in immediate succession -- once by
"critical"'s own always-running 2-min loop, once again by "standard".
Both AWS's actual publication cadence for these metrics (EC2 basic
monitoring: 5 min; EC2 detailed monitoring, if enabled: 1 min; ALB: 1
min) and the fact that critical tier's independent 2-min loop already
provides continuous coverage make the second call pure duplicate spend.
Fixed in app/collector/metrics/runner.py -- ec2_critical and elb tasks
now dispatch on tier == "critical" only. EBS had the same issue between
"standard" and "low" (EBS publishes at 5-min resolution; the 15-min "low"
re-poll could only ever re-return data "standard" had already fetched);
EBS is now dispatched on tier == "standard" only.

Alerts evaluated after every standard cycle.
Discovery runs every 15 min (aligned with low tier).
Partition management runs daily.

Cost impact (3 accounts):
  Before Phase 2:        510 metrics x 288 cycles/day = $44/mo
  After Phase 2:          ~220 avg x 240 cycles/day    = ~$15/mo  (66% reduction)
  After dedup fix (this change): removes the EC2-critical/ELB "standard"-
  tier duplicate call (~1 in 3 cycles) and the EBS "low"-tier duplicate
  call (~2 in 3 fifteen-minute cycles) on top of that -- exact %
  depends on real fleet size; see monitoring-hub-metric-audit.md §3 for
  the formula and REMAINING in the deployment report for what still
  needs live account/resource counts to quantify precisely.
"""
import time
import logging
import threading
from datetime import datetime
from app.db import get_connection

_stop_event = threading.Event()
logger      = logging.getLogger(__name__)

# ── Intervals (seconds) ───────────────────────────────────────
CRITICAL_INTERVAL  = 120    #  2 min — EC2 CPU, RDS, ELB
STANDARD_INTERVAL  = 300    #  5 min — + EBS, Lambda Errors
LOW_INTERVAL       = 900    # 15 min — EC2 Disk, Lambda Invocations
DISCOVERY_INTERVAL = 900    # 15 min — aligned with low tier


def _get_active_accounts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, account_name, account_id, role_arn, auth_mode,
                   external_id, default_region
            FROM aws_accounts
            WHERE status = 'active'
        """)
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()



# VictoriaMetrics is intentionally stopped (see monitoring-hub-metric-audit.md
# §8 flaw #4). sync_metrics_from_vm() is now REDUNDANT, not just idle: Phase 1's
# GMD revival (apply_direct_gmd_metrics_revival.py, see the run_metrics_collection
# call below) already writes every AWS metric this app tracks straight into the
# same `metrics` table sync_metrics_from_vm() exists to populate FROM VM --
# evaluate_alerts() already has fresh data by the time this function would run.
# Disabled here at the call site only -- app/collector/metrics_vm_sync.py itself
# is untouched, per instruction, pending a later full VM-code cleanup pass once
# the direct-cloud path has been running in production long enough to trust
# fully. Toggle this back to True only if VM is intentionally restarted AND a
# real (not-yet-identified) reason to prefer VM-sourced data over the direct
# GMD data already in `metrics` is found.
_VM_SYNC_ENABLED = False


def run_once(tier="standard"):
    """Single collection + alert cycle for given tier."""
    from app.collector.metrics.runner  import run_metrics_collection
    from app.collector.alert_evaluator import evaluate_alerts
    from app.collector.metrics_writer  import prune_metric_history

    accounts = _get_active_accounts()
    if not accounts:
        logger.warning("No active accounts")
        return

    # Re-enabled (see apply_direct_gmd_metrics_revival.py) -- this was
    # disabled, not deleted, during the VM/YACE cost-avoidance migration.
    # AWS billing for GetMetricData applies again; accepted deliberately.
    run_metrics_collection(accounts, tier=tier)

    if tier == "low":
        prune_metric_history()

    # Evaluate alerts after every standard cycle
    if tier == "standard":
        if _VM_SYNC_ENABLED:
            from app.collector.metrics_vm_sync import sync_metrics_from_vm
            sync_metrics_from_vm()
        evaluate_alerts()
        

def run_discovery_once():
    from app.collector.discovery.runner import run_discovery
    run_discovery()


def run_loop():
    """
    Tiered loop:
      Every 2 min  → critical tier
      Every 5 min  → standard tier (+ alerts)
      Every 15 min → low tier + discovery + partition check
    """
    last_standard   = 0
    last_low        = 0
    last_discovery  = 0
    cycle           = 0

    logger.info("Tiered scheduler started "
                "(critical=2min, standard=5min, low=15min)")

    while not _stop_event.is_set():
        now    = time.time()
        cycle += 1

        # ── Critical tier (2 min) ─────────────────────────────
        logger.info(f"[Cycle {cycle}] critical tier")
        try:
            run_once("critical")
        except Exception as e:
            logger.error(f"Critical tier error: {e}")

        # ── Standard tier (5 min) ─────────────────────────────
        if now - last_standard >= STANDARD_INTERVAL:
            logger.info(f"[Cycle {cycle}] standard tier")
            try:
                run_once("standard")
                last_standard = now
            except Exception as e:
                logger.error(f"Standard tier error: {e}")

        # ── Low tier + discovery (15 min) ─────────────────────
        if now - last_low >= LOW_INTERVAL:
            logger.info(f"[Cycle {cycle}] low tier")
            try:
                run_once("low")
                last_low = now
            except Exception as e:
                logger.error(f"Low tier error: {e}")

        if now - last_discovery >= DISCOVERY_INTERVAL:
            logger.info(f"[Cycle {cycle}] discovery")
            try:
                run_discovery_once()
                last_discovery = now
            except Exception as e:
                logger.error(f"Discovery error: {e}")

        # Sleep until next critical cycle
        elapsed = time.time() - now
        sleep   = max(0, CRITICAL_INTERVAL - elapsed)
        logger.info(f"[Cycle {cycle}] done in {elapsed:.1f}s — next in {sleep:.0f}s")
        _stop_event.wait(timeout=sleep)


# ── Standalone entry ──────────────────────────────────────────

def run():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger.info("Single collection cycle (standard)...")
    run_discovery_once()
    run_once("standard")
    logger.info("Done.")


if __name__ == "__main__":
    run()