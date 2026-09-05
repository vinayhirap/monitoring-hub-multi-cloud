# app/db.py
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
