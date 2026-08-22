# app/collector/scheduler.py
"""
Tiered scheduler — Phase 2 implementation.

  critical  — every 2 min  : EC2 CPU/Network, RDS, ELB
  standard  — every 5 min  : above + EBS + Lambda Errors
  low       — every 15 min : EC2 Disk, Lambda Invocations

Alerts evaluated after every standard cycle.
Discovery runs every 15 min (aligned with low tier).
Partition management runs daily.

Cost impact (3 accounts):
  Before:  510 metrics x 288 cycles/day = $44/mo
  After:   ~220 avg x 240 cycles/day    = ~$15/mo  (66% reduction)
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
PARTITION_INTERVAL = 86400  # daily


def _get_active_accounts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, account_name, account_id,
               role_arn, external_id, default_region
        FROM aws_accounts
        WHERE status = 'active'
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def _ensure_next_partition():
    try:
        from datetime import date

        conn   = get_connection()
        cursor = conn.cursor()

        today = date.today()
        if today.month == 12:
            next_year, next_month = today.year + 1, 1
        else:
            next_year, next_month = today.year, today.month + 1

        if next_month == 12:
            bound_year, bound_month = next_year + 1, 1
        else:
            bound_year, bound_month = next_year, next_month + 1

        partition_name = f"p{next_year}_{next_month:02d}"
        bound_date     = f"{bound_year}-{bound_month:02d}-01"

        cursor.execute("""
            SELECT PARTITION_NAME FROM information_schema.PARTITIONS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME   = 'metrics'
              AND PARTITION_NAME = %s
        """, (partition_name,))

        if not cursor.fetchone():
            cursor.execute(f"""
                ALTER TABLE metrics REORGANIZE PARTITION p_future INTO (
                    PARTITION {partition_name} VALUES LESS THAN (TO_DAYS('{bound_date}')),
                    PARTITION p_future VALUES LESS THAN MAXVALUE
                )
            """)
            conn.commit()
            logger.info(f"Added partition: {partition_name}")

        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Partition error: {e}")


def run_once(tier="standard"):
    """Single collection + alert cycle for given tier."""
    from app.collector.metrics.runner  import run_metrics_collection
    from app.collector.alert_evaluator import evaluate_alerts
    from app.collector.metrics_vm_sync   import sync_metrics_from_vm

    accounts = _get_active_accounts()
    if not accounts:
        logger.warning("No active accounts")
        return

    # TEMPORARILY DISABLED — migrating to VM/YACE, no boto3 GMD calls.
    # Re-enable only if you decide to roll back the VM migration.
    # run_metrics_collection(accounts, tier=tier)
    logger.info(f"[{tier}] GMD collection skipped — VM/YACE migration in progress")

    # Evaluate alerts after every standard cycle
    if tier == "standard":
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
    last_partition  = 0
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

        if now - last_partition >= PARTITION_INTERVAL:
            try:
                _ensure_next_partition()
                last_partition = now
            except Exception as e:
                logger.error(f"Partition error: {e}")

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