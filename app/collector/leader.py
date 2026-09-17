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

CONFIRMED INCIDENT (2026-09-17, prod, two workers both logged "[leader]
acquired" with no "[leader] lost" in between, both ran a full collector
cycle -- including the hourly CSPM security checks, app/collector/
cspm.py -- concurrently): the 2026-09-12 fix above correctly assumed a
lost connection would eventually surface as an exception on that
connection and trigger leader_event.clear() -- but the leader thread's
poll loop, once started=True, issued no further queries on that
connection AT ALL. A connection that died loudly (something else using
it threw immediately) was still handled correctly; a connection that
died SILENTLY -- the server reaping an idle session, a network blip
with no immediate socket error -- was not, because nothing was left to
throw. MySQL still released the GET_LOCK the instant it reaped that
connection, so a standby correctly took over, but the original leader
never noticed its own loss and kept its already-started threads
running forever alongside the new leader's -- the exact "N workers = N
schedulers" scenario this file exists to prevent, reached by a
different silent path than 2026-09-12's. Fixed by having an
already-started leader actively re-confirm ownership (IS_USED_LOCK vs.
its own connection id) every poll_interval instead of only reacting to
an exception that, in this failure mode, never came -- see
run_when_leader()'s docstring below for the exact mechanism.
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


def _lock_holder(conn):
    """Returns the connection id currently holding _LOCK_NAME, or None
    if nobody holds it right now. Unlike _try_acquire, this never tries
    to take the lock -- it's a read-only ownership check, used once per
    poll interval by an already-started leader to prove it still
    actually holds what it thinks it holds (see run_when_leader's
    2026-09-17 fix note for why this exists)."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT IS_USED_LOCK(%s)", (_LOCK_NAME,))
        row = cur.fetchone()
        return row[0] if row else None
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

    2026-09-17 FIX -- confirmed split-leadership incident: two worker
    processes both logged "[leader] acquired" with no "[leader] lost" in
    between, and both ran a full collector cycle (including CSPM,
    app/collector/cspm.py) concurrently. Root cause: once started=True
    below, the old code's loop body did *nothing* each poll interval --
    no query, no health check -- so a connection that died SILENTLY
    (server-side idle-timeout kill, a network blip that doesn't surface
    as an immediate socket error) was undetectable to the leader that
    lost it. MySQL correctly released the GET_LOCK the instant it
    reaped that dead connection, so a standby worker correctly took
    over -- but the original leader's thread never issued another query
    to surface that as an exception, so it never called
    leader_event.clear() and its already-started collector threads kept
    running indefinitely alongside the new leader's. The fix: an
    already-started leader now actively re-confirms ownership every
    poll_interval via _lock_holder() instead of assuming silence means
    "still fine" -- any mismatch (lock released, or, more unusually,
    held by a different connection id) raises and is handled by the
    exact same cleanup path a hard connection error always used.
    """

    def _loop():
        conn = None
        started = False
        leader_event = None
        my_connection_id = None
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
                        my_connection_id = conn.connection_id
                        leader_event = threading.Event()
                        leader_event.set()
                        start_fn(leader_event)
                    else:
                        logger.debug(
                            f"[leader] '{_LOCK_NAME}' held by another worker "
                            f"-- standing by"
                        )
                else:
                    # Already leader: prove it, every poll_interval,
                    # instead of assuming a quiet connection is a
                    # healthy one -- see this function's 2026-09-17 fix
                    # note above for the incident that made this
                    # necessary. Any mismatch (None = released, or some
                    # other connection id = genuinely surprising) is
                    # raised so the except block below runs its
                    # existing, already-correct leader_event.clear()
                    # cleanup -- this check adds detection, it doesn't
                    # change what happens once loss is detected.
                    holder = _lock_holder(conn)
                    if holder != my_connection_id:
                        raise RuntimeError(
                            f"'{_LOCK_NAME}' is no longer held by this connection "
                            f"(connection id {my_connection_id!r}) -- currently held by "
                            f"{holder!r}"
                        )
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
                my_connection_id = None
            time.sleep(poll_interval)

    threading.Thread(target=_loop, daemon=True, name="collector-leader").start()
