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
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from app.db import get_connection

logger = logging.getLogger(__name__)

# Audit B14: collector writes are fanned out from up to 10 account threads x
# 6 task threads (runner.py) plus the extended/multi-cloud collectors, while
# mysql-connector's pool RAISES "pool exhausted" instead of waiting. Every
# writer below therefore takes a slot first, so a large collection cycle
# queues its writes instead of exhausting the shared pool (which is what
# turns into 500s on login/API requests). Tunable; keep well under
# DB_POOL_SIZE (default 20).
_WRITE_SLOTS = threading.BoundedSemaphore(
    max(1, int(os.getenv("METRICS_WRITE_CONCURRENCY", "4")))
)

# prune_metric_history deletes in chunks so one 30-day sweep never holds a
# multi-million-row DELETE (long locks, huge undo log) against a table the
# collectors are inserting into at the same time.
_PRUNE_BATCH_ROWS = 10000
_PRUNE_MAX_BATCHES = 1000


# MySQL errors worth retrying: 1213 = deadlock (InnoDB already rolled the
# transaction back), 1205 = lock wait timeout. Seen at service startup when
# two uvicorn workers wrote overlapping rows at the same moment. The whole
# batch transaction is re-run on a fresh connection; any other error is
# logged and the batch dropped, as before.
_RETRYABLE_ERRNOS = {1213, 1205}
_RETRY_DELAYS = (0.2, 0.4)  # sleeps between attempts -> max 3 attempts


def _is_retryable(exc) -> bool:
    return getattr(exc, "errno", None) in _RETRYABLE_ERRNOS


def _sorted_for_locking(rows, key):
    """Sort a batch by the table's unique key so concurrent writers take row
    locks in the same order (reduces deadlocks). Falls back to the original
    order if the rows can't be compared."""
    try:
        return sorted(rows, key=key)
    except TypeError:
        return list(rows)


def _run_batch_with_retry(execute, error_label, success_label):
    """Run execute(cursor) + commit, retrying the whole transaction on
    deadlock / lock-wait-timeout. Logs WARNING per retry, ERROR only if the
    final attempt fails (or on any non-retryable error)."""
    attempts = len(_RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        conn   = get_connection()
        cursor = conn.cursor()
        retry  = False
        try:
            execute(cursor)
            conn.commit()
            logger.debug(success_label.format(cursor.rowcount))
            return
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            if _is_retryable(e) and attempt < attempts:
                logger.warning(
                    f"{error_label} (attempt {attempt}/{attempts}, retrying): {e}"
                )
                retry = True
            else:
                logger.error(f"{error_label}: {e}")
                return
        finally:
            cursor.close()
            conn.close()
        if retry:
            time.sleep(_RETRY_DELAYS[attempt - 1])


@contextmanager
def _write_slot():
    _WRITE_SLOTS.acquire()
    try:
        yield
    finally:
        _WRITE_SLOTS.release()


def write_metric(resource_db_id: int, metric_name: str, metric_value: float):
    """
    Upsert a single metric's latest value.
    resource_db_id: resources.id (integer PK, not AWS resource string)
    metric_name:    lowercase metric name e.g. 'cpuutilization'
    metric_value:   float value
    """
    if resource_db_id is None or metric_value is None:
        return

    with _write_slot():
        _write_metric_locked(resource_db_id, metric_name, metric_value)


def _write_metric_locked(resource_db_id, metric_name, metric_value):
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

    with _write_slot():
        _write_metrics_batch_locked(datapoints)


def _write_metrics_batch_locked(datapoints):
    now  = datetime.utcnow()
    rows = _sorted_for_locking(
        [
            (r_id, name, round(float(val), 6), now)
            for r_id, name, val in datapoints
            if r_id is not None and val is not None
        ],
        key=lambda r: (r[0], r[1]),  # UNIQUE (resource_id, metric_name)
    )

    def _execute(cursor):
        cursor.executemany("""
            INSERT INTO metrics
                (resource_id, metric_name, metric_value, metric_timestamp)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                metric_value     = VALUES(metric_value),
                metric_timestamp = VALUES(metric_timestamp)
        """, rows)

    _run_batch_with_retry(
        _execute,
        error_label="metrics_writer batch error",
        success_label="Batch upserted {} metrics",
    )


def write_metric_history_batch(datapoints: list):
    """
    Inserts raw time-series datapoints into metric_history -- the local
    replacement for VictoriaMetrics' range-query/graphing role, now that
    AWS metrics are fetched via direct GetMetricData calls instead of
    VM/YACE (see apply_direct_gmd_metrics_revival.py). Every call ADDS
    rows -- this is genuine history, unlike write_metrics_batch() above
    which upserts a single latest value.

    INSERT IGNORE, not plain INSERT (2026-09-16, see
    db/migrations/043_metric_history_dedup_key.sql): once that
    migration's UNIQUE KEY (resource_id, metric_name, metric_timestamp)
    is in place, re-writing an already-recorded datapoint is expected,
    routine behavior for the slow_extended tier's now much-wider
    GetMetricData lookback windows (extended.py's _LOOKBACK_MINUTES) --
    a window wider than the poll interval necessarily re-fetches
    already-seen data on every subsequent cycle. Plain INSERT would
    turn that expected overlap into a hard duplicate-key error on
    EVERY slow_extended cycle after the first, aborting the whole
    batch's write (see the try/except below -- previously exists only
    for genuinely unexpected DB errors, not a routine, expected
    condition). IGNORE makes a repeat of the exact same
    (resource_id, metric_name, metric_timestamp, value) a silent no-op,
    while a real DB error (connection loss, etc.) still raises via
    non-duplicate-key error codes and is still caught below.

    datapoints: list of (resource_db_id, metric_name, value, timestamp) tuples.
    """
    if not datapoints:
        return

    with _write_slot():
        _write_metric_history_batch_locked(datapoints)


def _write_metric_history_batch_locked(datapoints):
    rows = _sorted_for_locking(
        [
            (r_id, name, round(float(val), 6), ts)
            for r_id, name, val, ts in datapoints
            if r_id is not None and val is not None
        ],
        # UNIQUE (resource_id, metric_name, metric_timestamp)
        key=lambda r: (r[0], r[1], r[3]),
    )

    def _execute(cursor):
        cursor.executemany("""
            INSERT IGNORE INTO metric_history
                (resource_id, metric_name, metric_value, metric_timestamp)
            VALUES (%s, %s, %s, %s)
        """, rows)

    _run_batch_with_retry(
        _execute,
        error_label="metric_history batch write error",
        success_label="Wrote {} history datapoints",
    )


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
        deleted = 0
        for _ in range(_PRUNE_MAX_BATCHES):
            cursor.execute(
                "DELETE FROM metric_history "
                "WHERE metric_timestamp < DATE_SUB(NOW(), INTERVAL %s DAY) "
                "LIMIT %s",
                (retain_days, _PRUNE_BATCH_ROWS)
            )
            batch = cursor.rowcount or 0
            conn.commit()
            deleted += batch
            if batch < _PRUNE_BATCH_ROWS:
                break
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
