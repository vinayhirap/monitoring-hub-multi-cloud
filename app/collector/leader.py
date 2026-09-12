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
a single named lock; whichever one gets it calls `start_fn(leader_event)`
exactly once and then just keeps holding the lock (so nobody else can
also become leader). If the leader process dies or is killed, MySQL
releases the lock the instant that connection closes, and within one
poll interval a surviving worker acquires it and starts its own copy
of the collector threads -- no manual restart, no split-brain window
longer than POLL_INTERVAL_SECONDS.

CONFIRMED INCIDENT (2026-09-12, prod, two live schedulers found running
under different cycle counts -- 486 vs 246 critical-tier cycles at
restart, ~8h12m apart, suspiciously close to MySQL's default 8h
wait_timeout): losing this connection for ANY reason mid-session --
not just process death -- silently releases the lock and lets a
standby take over, while the ORIGINAL leader's already-started
threads kept running indefinitely, since Python threads have no idea
their owning connection died. That produced exactly this app's own
worst-case scenario (N workers = N schedulers) without a single
process ever crashing. Root cause of the connection loss itself
wasn't pinned down further (network blip vs. an idle-timeout hit
despite the 10s poll -- see POLL_INTERVAL_SECONDS), but the fix below
does not depend on finding it: instead of leaving orphaned threads to
run forever once they lose their claim to leadership, `start_fn` now
receives a `threading.Event` that is set for exactly as long as this
process holds the lock. Every long-running collector loop checks it
once per cycle and stops itself the moment it goes false, closing the
gap to about one cycle's worth of overlap instead of "until the next
manual restart."

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
    Starts a background thread that only calls start_fn(leader_event) once,
    the first time THIS process wins the named lock. Safe to call from
    every worker process identically -- exactly one of them will ever
    actually run start_fn(), and leadership migrates automatically if
    that worker later disappears OR loses its lock for any other reason.

    start_fn receives a fresh threading.Event, already set(), that is
    this leadership generation's signal: every long-running loop started
    by start_fn should check `leader_event.is_set()` once per cycle and
    stop itself as soon as it comes back False, rather than assuming
    "I was told to start, so I run forever."
    """

    def _loop():
        conn = None
        started = False
        leader_event = None
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
                        leader_event = threading.Event()
                        leader_event.set()
                        start_fn(leader_event)
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
                if leader_event is not None:
                    logger.warning(
                        f"[leader] lost '{_LOCK_NAME}' after having started collector "
                        f"threads in this worker -- clearing leader_event so they stop "
                        f"themselves (they do NOT self-terminate just because this "
                        f"connection dropped -- see this module's docstring)"
                    )
                    leader_event.clear()
                    leader_event = None
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
                started = False  # this process lost its connection/lock -- another
                                  # worker may now be leader; if THIS process reacquires
                                  # later, start_fn() runs again with a BRAND NEW event,
                                  # so any still-running (but now-signaled-to-stop) old
                                  # threads from the previous generation can never be
                                  # confused with the new ones.
            time.sleep(poll_interval)

    threading.Thread(target=_loop, daemon=True, name="collector-leader").start()
