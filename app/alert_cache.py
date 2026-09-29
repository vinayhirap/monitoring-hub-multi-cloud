# app/alert_cache.py
"""
One cache discipline for every alert-derived number (2026-09-29).

WHY: the Alerts page, its tab badges, the sidebar badge, the Overview banner /
account cards / health ring and the Services tiles are all rollups of the same
`alerts` table, but each had its own in-process cache with its own TTL (Alerts
list: none, /counts: 15 s, rollup: 10 s, Overview: 60 s + a 60 s browser poll).
The service runs `uvicorn --workers 2` and invalidation only clears the cache in
the worker that happened to run the evaluator, so a request served by the OTHER
worker kept an old snapshot for its full TTL: the Alerts page updated first and
the Overview caught up minutes later.

FIX: every cached alert rollup is validated against a cheap fingerprint of the
OPEN alerts read from the database on each request (one indexed query over a
few dozen rows). A change written by any worker/process changes the fingerprint,
so every worker recomputes immediately. A short TTL remains only for the purely
time-based transition active -> stale, which changes no row.
"""
import threading
import time

from app.db import get_connection

# Time-based state changes (active -> stale, mute expiry) alter no row, so the
# fingerprint cannot see them; bound their delay.
TIME_BASED_TTL = 5

_FP_SQL = """
    SELECT COUNT(*)                                        AS n,
           COALESCE(MAX(id), 0)                            AS max_id,
           COALESCE(SUM(status = 'active'), 0)             AS n_active,
           COALESCE(SUM(status = 'acknowledged'), 0)       AS n_ack,
           COALESCE(SUM(UPPER(severity) = 'CRITICAL'), 0)  AS n_crit,
           COALESCE(SUM(silenced), 0)                      AS n_silenced,
           COALESCE(SUM(marked_false_positive), 0)         AS n_fp,
           COALESCE(SUM(muted_until IS NOT NULL), 0)       AS n_muted,
           COALESCE(SUM(UNIX_TIMESTAMP(muted_until)), 0)   AS muted_sum,
           COALESCE(SUM(id * (status = 'acknowledged')), 0) AS ack_ids,
           COALESCE(MAX(aws_account_id), 0)                AS acct_max
    FROM alerts
    WHERE status IN ('active', 'acknowledged')
"""


def alerts_fingerprint():
    """Tuple that changes whenever the set/state of OPEN alerts changes.
    Resolved-alert history is covered too: resolving removes a row from the
    open set, which changes n / max_id / n_active."""
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(_FP_SQL)
            row = cur.fetchone() or {}
        finally:
            cur.close()
    finally:
        conn.close()
    return tuple(sorted((k, str(v)) for k, v in row.items()))


class FingerprintCache:
    """Cache one value; valid while the alert fingerprint is unchanged and it
    is younger than `ttl`. The fingerprint is read BEFORE computing, so a write
    that lands mid-compute makes the next request recompute."""

    def __init__(self, ttl=TIME_BASED_TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._data = None
        self._fp = None
        self._ts = 0.0

    def clear(self):
        with self._lock:
            self._data, self._fp, self._ts = None, None, 0.0

    def get(self, compute):
        try:
            fp = alerts_fingerprint()
        except Exception:
            fp = None          # DB hiccup: fall back to TTL-only behaviour
        now = time.time()
        with self._lock:
            if (self._data is not None and now - self._ts < self.ttl
                    and (fp is None or fp == self._fp)):
                return self._data
        data = compute()
        with self._lock:
            self._data, self._fp, self._ts = data, fp, time.time()
        return data


# ── the one shared rollup every screen reads ─────────────────────────
_rollup = FingerprintCache()


def get_alert_rollup():
    """Unscoped canonical rollup (app/alert_rules.py). Callers filter by the
    requesting user's accessible accounts."""
    from app import alert_rules

    def compute():
        conn = get_connection()
        try:
            cur = conn.cursor(dictionary=True)
            try:
                rows = alert_rules.fetch_open_alert_rows(cur, None)
            finally:
                cur.close()
        finally:
            conn.close()
        return alert_rules.rollup(rows)

    return _rollup.get(compute)


def invalidate_rollup():
    _rollup.clear()
