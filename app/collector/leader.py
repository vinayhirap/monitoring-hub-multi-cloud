# app/collector/leader.py
"""
MySQL named-lock leader election for the background collector threads.

WHY THIS EXISTS: with `uvicorn ... --workers N` (N > 1), each worker is
a fully separate OS process, and app/main.py's lifespan() hook runs
once per process. Before this file existed, that meant every worker
started its own independent copy of the scheduler / describe-poll /
multi-cloud loops -- N workers = N schedulers, all hitting the same
DB rows and the same AWS APIs on the same cadence. This is what
produced the Sep 5 2026 incident: two workers' schedulers UPDATEing
alerts/alert_pending at the same instant, causing an InnoDB deadlock
whose exception leaked a DB connection (see app/db.py's docstring and
apply_db_pool_leak_and_leader_election_fix.py for the full story).

HOW IT WORKS: one dedicated MySQL connection is held for the lifetime
of the worker process (MySQL's GET_LOCK() is session-scoped -- the
lock is tied to that exact connection). Every worker polls to acquire
a single named lock; whichever one gets it calls `start_fn()` exactly
once and then just keeps holding the lock (so nobody else can also
become leader). If the leader process dies or is killed, MySQL
releases the lock the instant that connection closes, and within one
poll interval a surviving worker acquires it and starts its own copy
of the collector threads -- no manual restart, no split-brain window
longer than POLL_INTERVAL_SECONDS.

This intentionally checks out one connection from the pool for the
full process lifetime. app/db.py's pool_size was raised specifically
to account for this (one per worker, permanently) on top of normal
request/collector traffic.
"""
import logging
import threading
import time

from app.db import get_connection

logger = logging.getLogger(__name__)

_LOCK_NAME = "monitoring_hub_collector_leader"
POLL_INTERVAL_SECONDS = 10


def _try_acquire(conn) -> bool:
    """GET_LOCK with a 0s timeout: non-blocking, just checks/claims right now."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT GET_LOCK(%s, 0)", (_LOCK_NAME,))
        row = cur.fetchone()
        return bool(row and row[0] == 1)
    finally:
        cur.close()


def run_when_leader(start_fn, poll_interval: int = POLL_INTERVAL_SECONDS) -> None:
    """
    Starts a background thread that only calls start_fn() once, the
    first time THIS process wins the named lock. Safe to call from
    every worker process identically -- exactly one of them will ever
    actually run start_fn(), and leadership migrates automatically if
    that worker later disappears.
    """

    def _loop():
        conn = None
        started = False
        while True:
            try:
                if conn is None:
                    conn = get_connection()
                if not started:
                    if _try_acquire(conn):
                        logger.info(
                            f"[leader] acquired '{_LOCK_NAME}' in this worker "
                            f"-- starting collector background loops here"
                        )
                        started = True
                        start_fn()
                    else:
                        logger.debug(
                            f"[leader] '{_LOCK_NAME}' held by another worker "
                            f"-- standing by"
                        )
                # Once started, this thread's only remaining job is to keep
                # the connection (and therefore the lock) alive -- MySQL
                # releases GET_LOCK() the moment the session/connection
                # closes, which is exactly the signal a standby worker's
                # own loop needs to take over.
            except Exception as e:
                logger.warning(f"[leader] lock check failed, will retry: {e}")
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
                started = False  # this process lost its connection/lock -- another
                                  # worker may now be leader; if THIS process's own
                                  # threads were already started, they keep running
                                  # (harmless/rare edge case: brief DB blip, not a
                                  # real failover), but this process must stop
                                  # claiming leadership until it re-acquires cleanly.
            time.sleep(poll_interval)

    threading.Thread(target=_loop, daemon=True, name="collector-leader").start()
