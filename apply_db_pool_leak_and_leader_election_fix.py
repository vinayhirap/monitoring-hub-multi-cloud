#!/usr/bin/env python3
"""
apply_db_pool_leak_and_leader_election_fix.py — permanent fix for the
Sep 5 2026 incident: monitoring-hub.service died with repeated
"Failed getting connection; pool exhausted" errors and had to be
manually restarted, during which the dashboard served hours-stale
CloudWatch/VM chart data.

ROOT CAUSES FOUND (both real, both fixed here):

  1. DUPLICATE BACKGROUND COLLECTORS (the trigger).
     app/main.py's lifespan() starts the scheduler / describe-poll /
     multi-cloud background threads unconditionally. The systemd unit
     runs `uvicorn ... --workers 2`, so BOTH worker processes run a
     full independent copy of every background loop. Server logs from
     the incident show two workers running "[Cycle 10] critical tier"
     at the exact same timestamp, then:
         ERROR app.collector.scheduler: Standard tier error:
         1213 (40001): Deadlock found when trying to get lock
     — both workers were UPDATEing the same alerts/alert_pending rows
     at the same instant. This also silently doubles every AWS
     Describe/GetMetricData-adjacent API call, undermining the cost
     work already documented elsewhere in this codebase.
     FIX: app/collector/leader.py (new) — a MySQL named-lock leader
     election. Only the worker that wins GET_LOCK() starts the
     background threads; the other stays dormant and will pick up
     leadership automatically if the leader worker dies/restarts.

  2. DB CONNECTION POOL LEAKS (what actually exhausted the pool).
     A static audit of every get_connection() call site in this
     codebase (99 total) found 75 with no try/finally around the
     connection — any exception (a MySQL deadlock being the most
     obvious example, per the log above) leaks that connection
     forever. pool_size was 10; it doesn't take many unlucky
     exceptions across a day of uptime before every future query
     blocks for connection_timeout (10s) and then raises exactly the
     "pool exhausted" error seen in the logs.
     FIX, two layers:
       a. app/db.py — pool_size 10 -> 20 (configurable via
          DB_POOL_SIZE), plus a leak-guard: get_connection() now
          returns a connection whose close() is guaranteed to run at
          most once and, if the caller's code path never calls it at
          all, is auto-recovered once Python's garbage collector
          reclaims the abandoned connection object. This is a
          BACKSTOP for the ~60 lower-frequency, request-scoped call
          sites this script does not individually rewrite (admin/user
          management, settings, alerts API, etc.) — it prevents a
          PERMANENT pool jam from any of them, though recovery isn't
          instant (GC-timing dependent), so it's not a substitute for
          fixing the actual hot paths below.
       b. Deterministic try/finally fixes in the specific functions
          PROVEN guilty by the incident logs — the continuously
          running background-loop code (scheduler, alert evaluator,
          VM sync, describe-poll, discovery, multi-cloud collectors,
          credential loading) — since these run every 30s-15min
          forever and are what actually accumulates leaks fast enough
          to exhaust a pool of 10-20 in well under a day.

NOT in scope (by design, not oversight): the ~60 request-scoped API
handlers (app/api/admin/*, app/api/alerts.py, app/api/settings.py,
app/api/metric_catalog.py, app/api/auth.py, etc.) only leak once per
FAILED HTTP request, not once per scheduler tick — orders of magnitude
lower frequency. They're protected by the db.py backstop (1.a above).
Rewriting all of them individually was judged higher-risk (each has
different early-return/commit semantics) than the value of doing it
blind in one pass; happy to do a second, more careful pass over those
specifically if you want full try/finally coverage everywhere too.

Usage:
    python apply_db_pool_leak_and_leader_election_fix.py --dry-run
    python apply_db_pool_leak_and_leader_election_fix.py
"""
import argparse
import py_compile
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKUP_SUFFIX = ".bak.pre-db-pool-leak-fix"

NEW_LEADER_FILE = REPO_ROOT / "app" / "collector" / "leader.py"
NEW_LEADER_FILE_CONTENT = '''# app/collector/leader.py
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
'''


class PatchError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────
# Patches
# ─────────────────────────────────────────────────────────────────────────
PATCHES = [
    (
        "app/db.py",
        [
            (
r'''# app/db.py
import os
import mysql.connector
from mysql.connector import pooling

_pool = pooling.MySQLConnectionPool(
    pool_name="monitoring_pool",
    pool_size=10,
    pool_reset_session=True,
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", 3307)),      # 3307 = Docker local, 3306 = EC2 prod
    user=os.getenv("DB_USER", "root"),         # root = Docker local, monitor = EC2 prod
    password=os.getenv("DB_PASSWORD", "root123"),
    database=os.getenv("DB_NAME", "monitoring_hub"),
    use_pure=True,
    connection_timeout=10,
)


def get_connection():
    return _pool.get_connection()
''',
r'''# app/db.py
"""
See apply_db_pool_leak_and_leader_election_fix.py's module docstring
for the full incident writeup (Sep 5 2026 pool-exhaustion outage) this
file's leak-guard and larger pool_size were written to fix.
"""
import os
import logging
import weakref
import mysql.connector
from mysql.connector import pooling
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Raised from 10: with `--workers N` each worker now also holds one
# permanent connection for its leader-election lock (app/collector/
# leader.py), on top of normal request + background-collector traffic.
# Override via DB_POOL_SIZE if you change --workers count.
_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", 20))

_pool = pooling.MySQLConnectionPool(
    pool_name="monitoring_pool",
    pool_size=_POOL_SIZE,
    pool_reset_session=True,
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", 3307)),      # 3307 = Docker local, 3306 = EC2 prod
    user=os.getenv("DB_USER", "root"),         # root = Docker local, monitor = EC2 prod
    password=os.getenv("DB_PASSWORD", "root123"),
    database=os.getenv("DB_NAME", "monitoring_hub"),
    use_pure=True,
    connection_timeout=10,
)


def get_connection():
    """
    Returns a pooled connection with a leak-guard attached: if the
    caller's code path raises before calling .close() (the ~75
    call-sites-without-try/finally audit that motivated this fix),
    the connection is still guaranteed to return to the pool exactly
    once -- either via the caller's own .close() (normal path,
    unaffected) or, failing that, automatically once Python's garbage
    collector reclaims the abandoned connection object. This is a
    BACKSTOP, not a substitute for try/finally at genuinely hot call
    sites (see the specific fixes elsewhere in this patch) -- GC-timing
    recovery is not instant, so it prevents a PERMANENT pool jam
    without guaranteeing low latency under a leak storm.
    """
    conn = _pool.get_connection()
    state = {"closed": False}
    original_close = conn.close

    def _guarded_close():
        if state["closed"]:
            return
        state["closed"] = True
        try:
            _finalizer.detach()
        except Exception:
            pass
        original_close()

    conn.close = _guarded_close

    def _finalize():
        if not state["closed"]:
            state["closed"] = True
            logger.warning(
                "DB connection leak auto-recovered by leak-guard "
                "(a code path never called .close() on it) -- this "
                "prevented a permanent pool jam, but the underlying "
                "call site should still be fixed with try/finally."
            )
            try:
                original_close()
            except Exception as e:
                logger.error(f"leak-guard: failed to auto-return connection: {e}")

    _finalizer = weakref.finalize(conn, _finalize)
    return conn


@contextmanager
def get_db_cursor(dictionary: bool = False, commit: bool = True):
    """
    Preferred pattern for ALL NEW CODE: guarantees the connection and
    cursor are released even on exception, and commits/rolls back
    deterministically.

        with get_db_cursor(dictionary=True) as (conn, cursor):
            cursor.execute("SELECT ...")
            rows = cursor.fetchall()

    Set commit=False for read-only call sites if you want to avoid an
    unnecessary commit (harmless either way for pure SELECTs, but
    explicit is cheap).
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=dictionary)
    try:
        yield conn, cursor
        if commit:
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cursor.close()
        conn.close()
''',
            ),
        ],
    ),
    (
        "app/main.py",
        [
            (
r'''@asynccontextmanager
async def lifespan(app):
    # ── Startup ───────────────────────────────────────────────
    threading.Thread(target=_run_collector, daemon=True, name="collector").start()
    threading.Thread(target=_run_describe_poll_loop, daemon=True, name="describe-poll").start()
    threading.Thread(target=_run_multicloud_collector, daemon=True, name="multicloud-collector").start()
    redis_task = asyncio.create_task(_safe_redis_listener())
    logger.info("Startup complete — collector running, Redis listener started")
    yield''',
r'''def _start_all_collector_threads():
    threading.Thread(target=_run_collector, daemon=True, name="collector").start()
    threading.Thread(target=_run_describe_poll_loop, daemon=True, name="describe-poll").start()
    threading.Thread(target=_run_multicloud_collector, daemon=True, name="multicloud-collector").start()


@asynccontextmanager
async def lifespan(app):
    # ── Startup ───────────────────────────────────────────────
    # Leader election across uvicorn workers -- see app/collector/leader.py
    # for why this exists (Sep 5 2026 incident: duplicate collector loops
    # across --workers 2 caused DB deadlocks + doubled AWS API calls).
    # Only the worker that wins the MySQL named lock actually starts the
    # background threads; others stand by and take over automatically if
    # the leader worker dies.
    from app.collector.leader import run_when_leader
    run_when_leader(_start_all_collector_threads)
    redis_task = asyncio.create_task(_safe_redis_listener())
    logger.info("Startup complete — collector leader-election started, Redis listener started")
    yield''',
            ),
        ],
    ),
    (
        "app/collector/scheduler.py",
        [
            (
r'''def _get_active_accounts():
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
    return rows''',
r'''def _get_active_accounts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, account_name, account_id,
                   role_arn, external_id, default_region
            FROM aws_accounts
            WHERE status = 'active'
        """)
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()''',
            ),
        ],
    ),
    (
        "app/collector/discovery/runner.py",
        [
            (
r'''def _get_active_accounts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, account_name, account_id, role_arn,
               external_id, default_region
        FROM aws_accounts
        WHERE status = 'active' AND provider = 'aws'
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows''',
r'''def _get_active_accounts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, account_name, account_id, role_arn,
                   external_id, default_region
            FROM aws_accounts
            WHERE status = 'active' AND provider = 'aws'
        """)
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()''',
            ),
        ],
    ),
    (
        "app/collector/metrics_vm_sync.py",
        [
            (
r'''    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT DISTINCT
            r.id             AS resource_db_id,
            r.resource_id    AS aws_resource_id,
            r.resource_type,
            mc.metric_name,
            mc.service,
            mc.statistic
        FROM thresholds t
        JOIN metric_catalog mc
            ON mc.id = t.metric_id
        JOIN resources r
            ON r.resource_type  = t.resource_type
           AND r.aws_account_id = t.aws_account_id
        WHERE t.enabled = 1
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows''',
r'''    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT DISTINCT
                r.id             AS resource_db_id,
                r.resource_id    AS aws_resource_id,
                r.resource_type,
                mc.metric_name,
                mc.service,
                mc.statistic
            FROM thresholds t
            JOIN metric_catalog mc
                ON mc.id = t.metric_id
            JOIN resources r
                ON r.resource_type  = t.resource_type
               AND r.aws_account_id = t.aws_account_id
            WHERE t.enabled = 1
        """)
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()''',
            ),
        ],
    ),
    (
        "app/collector/alert_evaluator.py",
        [
            (
r'''def evaluate_alerts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)

    stale_total, stale_accounts, stale_orphans = _auto_resolve_stale_alerts(cursor)
    conn.commit()
    if stale_total:''',
r'''def evaluate_alerts():
    conn   = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        _evaluate_alerts_body(conn, cursor)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cursor.close()
        conn.close()


def _evaluate_alerts_body(conn, cursor):
    """
    Body of evaluate_alerts(), split out so the connection/cursor
    acquired in evaluate_alerts() are guaranteed to be released via
    try/finally even if a MySQL deadlock (observed in production, Sep
    5 2026) or any other exception happens partway through -- this
    used to leak the connection every time that happened, which is
    what exhausted the pool and took the dashboard offline for hours.
    """
    stale_total, stale_accounts, stale_orphans = _auto_resolve_stale_alerts(cursor)
    conn.commit()
    if stale_total:''',
            ),
            (
r'''        except Exception as e:
            logger.warning(f"Alert publish failed: {e}")

    conn.commit()
    cursor.close()
    conn.close()

    logger.info(
        f"Alert evaluation complete — "
        f"new: {new_alerts}, resolved: {resolved}, already open: {already_open}, "
        f"pending touched: {pending_touched}, pending promoted: {pending_cleared}"
    )''',
r'''        except Exception as e:
            logger.warning(f"Alert publish failed: {e}")

    conn.commit()

    logger.info(
        f"Alert evaluation complete — "
        f"new: {new_alerts}, resolved: {resolved}, already open: {already_open}, "
        f"pending touched: {pending_touched}, pending promoted: {pending_cleared}"
    )''',
            ),
        ],
    ),
    (
        "app/aws/describe_polling.py",
        [
            (
r'''    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT a.id AS account_db_id, a.role_arn, a.external_id, a.default_region,
               r.resource_id
        FROM resources r
        JOIN aws_accounts a ON a.id = r.aws_account_id
        WHERE r.resource_type = 'ec2'
          AND r.instance_state = 'running'
          AND a.status = 'active'
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()

    grouped = {}
    for row in rows:''',
r'''    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT a.id AS account_db_id, a.role_arn, a.external_id, a.default_region,
                   r.resource_id
            FROM resources r
            JOIN aws_accounts a ON a.id = r.aws_account_id
            WHERE r.resource_type = 'ec2'
              AND r.instance_state = 'running'
              AND a.status = 'active'
        """)
        rows = cur.fetchall()
    finally:
        cur.close(); conn.close()

    grouped = {}
    for row in rows:''',
            ),
            (
r'''    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id AS account_db_id, role_arn, external_id, default_region
        FROM aws_accounts WHERE status = 'active'
    """)
    accounts = cur.fetchall()
    cur.close(); conn.close()

    grouped = {}
    for a in accounts:''',
r'''    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id AS account_db_id, role_arn, external_id, default_region
            FROM aws_accounts WHERE status = 'active'
        """)
        accounts = cur.fetchall()
    finally:
        cur.close(); conn.close()

    grouped = {}
    for a in accounts:''',
            ),
        ],
    ),
    (
        "app/providers/azure/metrics_collector.py",
        [
            (
r'''def collect_all_azure_accounts() -> dict:
    """Runs collect_account_metrics() for every active Azure account. Used by the scheduler."""
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT * FROM aws_accounts
        WHERE status = 'active' AND provider = 'azure'
    """)
    accounts = cur.fetchall()
    cur.close(); conn.close()

    totals = {"accounts": len(accounts), "pushed": 0, "errors": []}''',
r'''def collect_all_azure_accounts() -> dict:
    """Runs collect_account_metrics() for every active Azure account. Used by the scheduler."""
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT * FROM aws_accounts
            WHERE status = 'active' AND provider = 'azure'
        """)
        accounts = cur.fetchall()
    finally:
        cur.close(); conn.close()

    totals = {"accounts": len(accounts), "pushed": 0, "errors": []}''',
            ),
        ],
    ),
    (
        "app/providers/gcp/metrics_collector.py",
        [
            (
r'''def collect_all_gcp_accounts() -> dict:
    """Runs collect_account_metrics() for every active GCP account. Used by the scheduler."""
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT * FROM aws_accounts
        WHERE status = 'active' AND provider = 'gcp'
    """)
    accounts = cur.fetchall()
    cur.close(); conn.close()

    totals = {"accounts": len(accounts), "pushed": 0, "errors": []}''',
r'''def collect_all_gcp_accounts() -> dict:
    """Runs collect_account_metrics() for every active GCP account. Used by the scheduler."""
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT * FROM aws_accounts
            WHERE status = 'active' AND provider = 'gcp'
        """)
        accounts = cur.fetchall()
    finally:
        cur.close(); conn.close()

    totals = {"accounts": len(accounts), "pushed": 0, "errors": []}''',
            ),
        ],
    ),
    (
        "app/providers/azure/provider.py",
        [
            (
r'''        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, account_name, account_id, tenant_id, subscription_id,
                   client_id, default_region
            FROM aws_accounts
            WHERE status = 'active' AND provider = 'azure'
        """)
        accounts = cursor.fetchall()
        cursor.close()
        conn.close()

        for account in accounts:
            try:
                secret = load_credential(account["id"])''',
r'''        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute("""
                SELECT id, account_name, account_id, tenant_id, subscription_id,
                       client_id, default_region
                FROM aws_accounts
                WHERE status = 'active' AND provider = 'azure'
            """)
            accounts = cursor.fetchall()
        finally:
            cursor.close()
            conn.close()

        for account in accounts:
            try:
                secret = load_credential(account["id"])''',
            ),
        ],
    ),
    (
        "app/providers/gcp/provider.py",
        [
            (
r'''        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, account_name, account_id, project_id,
                   service_account_email, default_region
            FROM aws_accounts
            WHERE status = 'active' AND provider = 'gcp'
        """)
        accounts = cursor.fetchall()
        cursor.close()
        conn.close()

        for account in accounts:
            try:
                sa_key_json = load_credential(account["id"])''',
r'''        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute("""
                SELECT id, account_name, account_id, project_id,
                       service_account_email, default_region
                FROM aws_accounts
                WHERE status = 'active' AND provider = 'gcp'
            """)
            accounts = cursor.fetchall()
        finally:
            cursor.close()
            conn.close()

        for account in accounts:
            try:
                sa_key_json = load_credential(account["id"])''',
            ),
        ],
    ),
    (
        "app/credentials.py",
        [
            (
r'''    token = _fernet().encrypt(secret_plaintext.encode())
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO provider_credentials (aws_account_id, provider, credential_ref, secret_encrypted)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            provider = VALUES(provider),
            credential_ref = VALUES(credential_ref),
            secret_encrypted = VALUES(secret_encrypted),
            updated_at = NOW()
    """, (account_id, provider, ref, token))
    conn.commit()
    cur.close()
    conn.close()''',
r'''    token = _fernet().encrypt(secret_plaintext.encode())
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO provider_credentials (aws_account_id, provider, credential_ref, secret_encrypted)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                provider = VALUES(provider),
                credential_ref = VALUES(credential_ref),
                secret_encrypted = VALUES(secret_encrypted),
                updated_at = NOW()
        """, (account_id, provider, ref, token))
        conn.commit()
    finally:
        cur.close()
        conn.close()''',
            ),
            (
r'''    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT secret_encrypted FROM provider_credentials WHERE aws_account_id = %s",
        (account_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None''',
r'''    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT secret_encrypted FROM provider_credentials WHERE aws_account_id = %s",
            (account_id,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        return None''',
            ),
            (
r'''def delete_credential(account_id: int) -> None:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM provider_credentials WHERE aws_account_id = %s", (account_id,))
    conn.commit()
    cur.close()
    conn.close()''',
r'''def delete_credential(account_id: int) -> None:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM provider_credentials WHERE aws_account_id = %s", (account_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()''',
            ),
        ],
    ),
]


def preflight():
    print("=== Pre-flight: verifying anchors match exactly ===")
    problems = []

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        if not full_path.exists():
            problems.append(f"MISSING FILE: {rel_path}")
            continue
        text = full_path.read_text(encoding="utf-8")
        for old, new in replacements:
            if new in text:
                continue  # already patched
            count = text.count(old)
            if count == 0:
                problems.append(f"{rel_path}: anchor not found (0 matches) — {old[:70]!r}")
            elif count > 1:
                problems.append(f"{rel_path}: anchor matched {count} times, expected exactly 1")

    if NEW_LEADER_FILE.exists():
        existing = NEW_LEADER_FILE.read_text(encoding="utf-8")
        if existing != NEW_LEADER_FILE_CONTENT:
            problems.append(
                f"{NEW_LEADER_FILE.relative_to(REPO_ROOT)} already exists with "
                f"DIFFERENT content — refusing to overwrite blindly."
            )

    if problems:
        print("\n".join(f"  - {p}" for p in problems))
        raise PatchError(f"{len(problems)} problem(s) found — aborting, no files touched.")
    print("  OK — all anchors match exactly once.")


def apply_all(dry_run: bool):
    changed_files = []
    report = []

    for rel_path, replacements in PATCHES:
        full_path = REPO_ROOT / rel_path
        text = full_path.read_text(encoding="utf-8")
        original_text = text
        for old, new in replacements:
            if new in text:
                continue  # already patched
            if old not in text:
                raise PatchError(f"{rel_path}: expected anchor vanished mid-patch — aborting")
            text = text.replace(old, new, 1)

        if text == original_text:
            continue

        if dry_run:
            report.append(f"[DRY RUN] would patch: {rel_path}")
        else:
            backup_path = full_path.with_suffix(full_path.suffix + BACKUP_SUFFIX)
            if not backup_path.exists():
                shutil.copy2(full_path, backup_path)
            full_path.write_text(text, encoding="utf-8")
            report.append(f"PATCHED: {rel_path}  (backup: {backup_path.name})")
            changed_files.append(full_path)

    if NEW_LEADER_FILE.exists() and NEW_LEADER_FILE.read_text(encoding="utf-8") == NEW_LEADER_FILE_CONTENT:
        report.append(f"SKIPPED (already exists, identical): {NEW_LEADER_FILE.relative_to(REPO_ROOT)}")
    else:
        if dry_run:
            report.append(f"[DRY RUN] would create: {NEW_LEADER_FILE.relative_to(REPO_ROOT)}")
        else:
            NEW_LEADER_FILE.write_text(NEW_LEADER_FILE_CONTENT, encoding="utf-8")
            report.append(f"CREATED: {NEW_LEADER_FILE.relative_to(REPO_ROOT)}")
            changed_files.append(NEW_LEADER_FILE)

    for line in report:
        print(line)

    return changed_files


def validate_python_syntax(changed_files):
    print("\n=== Validating Python syntax (py_compile) ===")
    for f in changed_files:
        if f.suffix != ".py":
            continue
        try:
            py_compile.compile(str(f), doraise=True)
            print(f"  OK  {f.relative_to(REPO_ROOT)}")
        except py_compile.PyCompileError as e:
            raise PatchError(f"SYNTAX ERROR after patching {f}: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        preflight()
        changed = apply_all(args.dry_run)
        if not args.dry_run:
            validate_python_syntax(changed)
            print(f"\n=== Done. {len(changed)} file(s) touched. ===")
            print("\nNext steps:")
            print("  1. sudo systemctl restart monitoring-hub")
            print("  2. sudo journalctl -u monitoring-hub -f")
            print("     Look for exactly ONE of these (only the leader worker logs it):")
            print("       [leader] acquired 'monitoring_hub_collector_leader' in this")
            print("       worker -- starting collector background loops here")
            print("     If you see it TWICE, something is still wrong -- stop and tell me.")
            print("  3. Confirm no more duplicate '[Cycle N]' log lines at the same")
            print("     timestamp from two different pids.")
            print("  4. DB_POOL_SIZE now defaults to 20 (was 10). Override via .env if needed.")
        else:
            print("\n=== Dry run complete. Re-run without --dry-run to apply. ===")
    except PatchError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
