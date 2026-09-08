# app/collector/metrics_writer.py
"""
Maintains the `metrics` table as a single-row-per-(resource, metric)
LAST-VALUE CACHE for alert_evaluator.py's threshold joins.

All historical/time-series data lives in VictoriaMetrics — that's the
system of record and the only place range queries or graphs should read
from (see app/clients/vm_client.py). This table never stores history;
every write is an upsert that overwrites the previous value in place, so
its row count stays equal to the number of distinct (resource, metric)
pairs being alerted on, not the number of datapoints collected over time.

Requires a UNIQUE KEY on (resource_id, metric_name) — see
db/migrations for the migration that adds it and collapses any old
history rows down to one per pair.
"""
import logging
from datetime import datetime
from app.db import get_connection

logger = logging.getLogger(__name__)


def write_metric(resource_db_id: int, metric_name: str, metric_value: float):
    """
    Upsert a single metric's latest value.
    resource_db_id: resources.id (integer PK, not AWS resource string)
    metric_name:    lowercase metric name e.g. 'cpuutilization'
    metric_value:   float value
    """
    if resource_db_id is None or metric_value is None:
        return

    conn   = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO metrics
                (resource_id, metric_name, metric_value, metric_timestamp)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                metric_value     = VALUES(metric_value),
                metric_timestamp = VALUES(metric_timestamp)
        """, (
            resource_db_id,
            metric_name,
            round(float(metric_value), 6),
            datetime.utcnow(),
        ))
        conn.commit()

    except Exception as e:
        logger.error(f"metrics_writer error [{resource_db_id}/{metric_name}]: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()


def write_metrics_batch(datapoints: list):
    """
    Upsert multiple metrics' latest values in a single transaction.
    datapoints: list of (resource_db_id, metric_name, metric_value) tuples
    More efficient than calling write_metric() in a loop.
    """
    if not datapoints:
        return

    conn   = get_connection()
    cursor = conn.cursor()

    try:
        now = datetime.utcnow()
        cursor.executemany("""
            INSERT INTO metrics
                (resource_id, metric_name, metric_value, metric_timestamp)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                metric_value     = VALUES(metric_value),
                metric_timestamp = VALUES(metric_timestamp)
        """, [
            (r_id, name, round(float(val), 6), now)
            for r_id, name, val in datapoints
            if r_id is not None and val is not None
        ])
        conn.commit()
        logger.debug(f"Batch upserted {cursor.rowcount} metrics")

    except Exception as e:
        logger.error(f"metrics_writer batch error: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()


def write_metric_history_batch(datapoints: list):
    """
    Inserts raw time-series datapoints into metric_history -- the local
    replacement for VictoriaMetrics' range-query/graphing role, now that
    AWS metrics are fetched via direct GetMetricData calls instead of
    VM/YACE (see apply_direct_gmd_metrics_revival.py). Every call ADDS
    rows -- this is genuine history, unlike write_metrics_batch() above
    which upserts a single latest value.

    datapoints: list of (resource_db_id, metric_name, value, timestamp) tuples.
    """
    if not datapoints:
        return

    conn   = get_connection()
    cursor = conn.cursor()

    try:
        cursor.executemany("""
            INSERT INTO metric_history
                (resource_id, metric_name, metric_value, metric_timestamp)
            VALUES (%s, %s, %s, %s)
        """, [
            (r_id, name, round(float(val), 6), ts)
            for r_id, name, val, ts in datapoints
            if r_id is not None and val is not None
        ])
        conn.commit()
        logger.debug(f"Wrote {cursor.rowcount} history datapoints")

    except Exception as e:
        logger.error(f"metric_history batch write error: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()


def prune_metric_history(retain_days: int = 7) -> int:
    """
    Deletes metric_history rows older than retain_days. Called
    periodically (see scheduler.py's low tier) to keep this table
    bounded -- unlike the `metrics` last-value cache (which never grows
    past one row per resource/metric pair), this table accumulates a new
    row every collection cycle and needs active pruning.
    """
    conn   = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM metric_history WHERE metric_timestamp < DATE_SUB(NOW(), INTERVAL %s DAY)",
            (retain_days,)
        )
        deleted = cursor.rowcount
        conn.commit()
        if deleted:
            logger.info(f"metric_history: pruned {deleted} row(s) older than {retain_days} days")
        return deleted
    except Exception as e:
        logger.error(f"metric_history prune error: {e}")
        conn.rollback()
        return 0
    finally:
        cursor.close()
        conn.close()
