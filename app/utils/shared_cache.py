# app/utils/shared_cache.py
"""
Small shared cache for PAID cloud reads on UI and discovery paths (Phase B2).

Why it exists
  The detail-page caches in app/aws/collector_direct.py were per-process memory: every restart emptied them and every
  worker kept its own copy, so each deploy re-paid for ListMetrics / GetMetricData the first time anyone looked.
  This module keeps the entries in Redis (already in the stack) so they survive restarts and are shared, with an
  in-process copy as the fallback when Redis is down.

What it guarantees
  * Single flight. N simultaneous viewers of one key cause ONE load: threads in a process share one flight (their result
    is handed to the waiters, even when it was not cacheable), and processes share a short Redis lock.
  * No false data. A load that raised, or during which any call recorded an error (note_error / track), is returned to
    the caller but NEVER stored, so "the call failed" can never be remembered as "there is no data". Loader exceptions
    propagate and are never stored either.
  * No stale surprises. Entries live for exactly the TTL the caller asks for (callers keep today's TTLs); there is no
    stale-while-revalidate. CacheResult.age_s tells a caller how old a hit is.
  * Backends fail soft. Redis errors never reach the caller; after one, Redis is skipped for REDIS_RETRY_SECONDS.
    With Redis down, entries live in the in-process copy (per process, which is the old behaviour).
  * Cross-process invalidation. While Redis is healthy the in-process copy is NOT read, so invalidate() in one process
    is seen by all of them.

Not a cache for: Describe*/List* resource calls (free), anything that must be fresh on every request, or values that are
not JSON-serialisable (those are returned uncached).

Settings (read at call time):  SHARED_CACHE_BACKEND = redis (default) | memory
                               SHARED_CACHE_REDIS_URL = redis://127.0.0.1:6379 (the repo's other Redis users hard-code this)
"""
import contextlib
import copy
import hashlib
import json
import logging
import math
import os
import threading
import time
from collections import namedtuple
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

KEY_PREFIX = "mh:sc:v1:"
LOCK_PREFIX = "mh:sc:lock:v1:"
LOCK_TTL_SECONDS = 30          # a crashed leader releases the cross-process lock after this
LOCK_WAIT_SECONDS = 8.0        # how long a second process waits for the leader before loading itself
LOCK_POLL_SECONDS = 0.05
FLIGHT_WAIT_SECONDS = 60.0     # how long an in-process waiter waits for the leader's flight
REDIS_RETRY_SECONDS = 15.0
L1_MAX_ENTRIES = 5000
MAX_KEY_LEN = 180

CacheResult = namedtuple("CacheResult", "value hit age_s ts shared")
# value   the cached or freshly loaded value
# hit     True when served from the cache without running the loader
# age_s   seconds since the value was loaded (0.0 for a fresh load)
# ts      epoch seconds the value was loaded (use it to anchor a time window)
# shared  True when this caller received the result of another caller's in-flight load

_lock = threading.Lock()
_l1 = {}                       # key -> (expires_epoch, json_envelope)   in-process copy, fallback only
_flights = {}                  # key -> _Flight
_redis_client = None
_redis_down_until = 0.0
_stats = {"hits": 0, "misses": 0, "loads": 0, "uncached_loads": 0, "flight_shared": 0,
          "lock_waits": 0, "redis_errors": 0, "invalidations": 0}

_now = time.time               # patched by tests
_sleep = time.sleep            # patched by tests

_RELEASE_LOCK = ("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
                 "else return 0 end")


# ── error scope: how a loader tells us "something failed even though I returned normally" ──────────────

_tls = threading.local()


class ErrorScope:
    def __init__(self):
        self.errors = []

    @property
    def count(self):
        return len(self.errors)


@contextlib.contextmanager
def error_scope():
    stack = getattr(_tls, "stack", None)
    if stack is None:
        stack = _tls.stack = []
    sc = ErrorScope()
    stack.append(sc)
    try:
        yield sc
    finally:
        stack.pop()


def note_error(where="", exc=None):
    """Record that a call inside the current loader failed although the loader swallowed it (the AWS helpers log and
    return []). Anything loaded while an error is recorded is returned but not cached. No-op outside a scope."""
    for sc in getattr(_tls, "stack", ()) or ():
        sc.errors.append((where, repr(exc) if exc is not None else ""))


class _TrackedClient:
    """Wraps a boto3-style client: a raised exception is recorded with note_error() and re-raised unchanged, so the
    wrapped helper's own try/except still behaves exactly as before."""

    def __init__(self, client, where):
        object.__setattr__(self, "_c", client)
        object.__setattr__(self, "_where", where)

    def __getattr__(self, name):
        attr = getattr(self._c, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            try:
                return attr(*a, **k)
            except Exception as e:
                note_error(f"{self._where}.{name}", e)
                raise
        return call


def track(client, where="client"):
    return _TrackedClient(client, where)


# ── backends ───────────────────────────────────────────────────────────────────────────────────────────

def _backend_is_redis():
    return os.environ.get("SHARED_CACHE_BACKEND", "redis").strip().lower() != "memory"


def _redis():
    """A live Redis client, or None (memory backend, Redis unavailable, or in the retry back-off)."""
    global _redis_client, _redis_down_until
    if not _backend_is_redis():
        return None
    if _now() < _redis_down_until:
        return None
    if _redis_client is None:
        try:
            _redis_client = _connect()
        except Exception as e:
            _redis_failed(e)
            return None
    return _redis_client


def _connect():
    import redis
    c = redis.Redis.from_url(os.environ.get("SHARED_CACHE_REDIS_URL", "redis://127.0.0.1:6379"),
                             decode_responses=True, socket_connect_timeout=1, socket_timeout=2)
    c.ping()
    return c


def _redis_failed(exc):
    global _redis_client, _redis_down_until
    _stats["redis_errors"] += 1
    if _redis_down_until <= _now():
        logger.warning(f"shared_cache: Redis unavailable ({exc}); using the in-process copy for "
                       f"{REDIS_RETRY_SECONDS:.0f}s")
    _redis_client = None
    _redis_down_until = _now() + REDIS_RETRY_SECONDS


def _rk(key):
    if len(key) > MAX_KEY_LEN:
        key = key[:MAX_KEY_LEN - 41] + ":" + hashlib.sha1(key.encode()).hexdigest()
    return KEY_PREFIX + key


def _decode(raw):
    if raw is None:
        return None
    try:
        env = json.loads(raw)
        return env["v"], float(env["ts"])
    except Exception:
        return None


def _read(key):
    """-> (value, ts) or None."""
    r = _redis()
    if r is not None:
        try:
            return _decode(r.get(_rk(key)))
        except Exception as e:
            _redis_failed(e)
    with _lock:
        ent = _l1.get(key)
    if ent and ent[0] > _now():
        return _decode(ent[1])
    return None


def _write(key, value, ttl, ts):
    """-> True when stored. A value that is not JSON-serialisable is simply not cached."""
    try:
        raw = json.dumps({"ts": ts, "v": value})
    except (TypeError, ValueError) as e:
        logger.warning(f"shared_cache: {key} is not cacheable ({e})")
        return False
    ttl = max(1, int(math.ceil(ttl)))
    with _lock:
        _l1[key] = (ts + ttl, raw)
        if len(_l1) > L1_MAX_ENTRIES:
            for k in sorted(_l1, key=lambda k: _l1[k][0])[:len(_l1) - L1_MAX_ENTRIES + 100]:
                _l1.pop(k, None)
    r = _redis()
    if r is not None:
        try:
            r.set(_rk(key), raw, ex=ttl)
        except Exception as e:
            _redis_failed(e)
    return True


def put(key, value, ttl):
    """Store a value directly (for a caller that did its own discovery and knows it succeeded). True when stored."""
    return _write(key, value, ttl, _now())


def invalidate(key):
    """Drop one entry everywhere (Redis and this process's copy)."""
    _stats["invalidations"] += 1
    with _lock:
        _l1.pop(key, None)
    r = _redis()
    if r is not None:
        try:
            r.delete(_rk(key))
        except Exception as e:
            _redis_failed(e)


def invalidate_prefix(prefix):
    """Drop every entry whose key starts with `prefix`. Returns how many Redis keys were removed."""
    _stats["invalidations"] += 1
    with _lock:
        for k in [k for k in _l1 if k.startswith(prefix)]:
            _l1.pop(k, None)
    n = 0
    r = _redis()
    if r is not None:
        try:
            for k in r.scan_iter(match=_rk(prefix) + "*"):
                r.delete(k)
                n += 1
        except Exception as e:
            _redis_failed(e)
    return n


def peek(key):
    """CacheResult for a stored entry without loading anything, else None."""
    got = _read(key)
    if got is None:
        return None
    value, ts = got
    return CacheResult(value, True, max(0.0, _now() - ts), ts, False)


def stats():
    return dict(_stats)


def reset_for_tests():
    global _redis_client, _redis_down_until
    with _lock:
        _l1.clear()
        _flights.clear()
    _redis_client = None
    _redis_down_until = 0.0
    for k in _stats:
        _stats[k] = 0


# ── single-flight get-or-load ────────────────────────────────────────────────────────────────────────

class _Flight:
    __slots__ = ("event", "result", "exc")

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.exc = None


def _ttl_for(ttl, value):
    return ttl(value) if callable(ttl) else ttl


def _usable(got, validate):
    if got is None:
        return False
    if validate is not None:
        try:
            return bool(validate(got[0]))
        except Exception:
            return False
    return True


def get_or_load_ex(key, loader, ttl, *, bypass=False, cacheable=None, validate=None):
    """Return a CacheResult for `key`, running `loader()` at most once across concurrent callers.

    ttl        seconds, or a callable(value) -> seconds (e.g. a shorter TTL for a "nothing found" value)
    bypass     skip the cache read (a manual "refresh"); the fresh value is still stored if it is cacheable
    cacheable  optional callable(value) -> bool; False = return the value but do not store it
    validate   optional callable(cached_value) -> bool; False = treat the stored entry as a miss (e.g. the resource
               it describes has changed since it was stored)
    A loader exception propagates to every waiter and nothing is stored."""
    if not bypass:
        got = _read(key)
        if _usable(got, validate):
            _stats["hits"] += 1
            return CacheResult(got[0], True, max(0.0, _now() - got[1]), got[1], False)

    with _lock:
        flight = _flights.get(key)
        leader = flight is None
        if leader:
            flight = _flights[key] = _Flight()

    if not leader:
        if flight.event.wait(FLIGHT_WAIT_SECONDS):
            _stats["flight_shared"] += 1
            if flight.exc is not None:
                raise flight.exc
            v, ts = flight.result
            # a copy: the leader's caller owns its object, a waiter must not be able to change it underneath them
            return CacheResult(copy.deepcopy(v), False, max(0.0, _now() - ts), ts, True)
        # the leader never finished (stuck loader): load on our own rather than hang forever
        return _load(key, loader, ttl, cacheable, None)

    try:
        res = _lead(key, loader, ttl, bypass, cacheable, validate)
        flight.result = (res.value, res.ts)
        return res
    except BaseException as e:
        flight.exc = e
        raise
    finally:
        flight.event.set()
        with _lock:
            _flights.pop(key, None)


def get_or_load(key, loader, ttl, **kw):
    return get_or_load_ex(key, loader, ttl, **kw).value


def _lead(key, loader, ttl, bypass, cacheable, validate):
    token = None
    r = _redis()
    if r is not None:
        token = "%s:%s" % (os.getpid(), threading.get_ident())
        try:
            got_lock = r.set(LOCK_PREFIX + key[:MAX_KEY_LEN], token, nx=True, ex=LOCK_TTL_SECONDS)
        except Exception as e:
            _redis_failed(e)
            got_lock, token = True, None
        if not got_lock:
            # another process is loading this key: wait for its result instead of paying twice
            _stats["lock_waits"] += 1
            waited = 0.0
            while waited < LOCK_WAIT_SECONDS:
                _sleep(LOCK_POLL_SECONDS)
                waited += LOCK_POLL_SECONDS
                got = _read(key)
                if _usable(got, validate) and not bypass:
                    _stats["hits"] += 1
                    return CacheResult(got[0], True, max(0.0, _now() - got[1]), got[1], True)
            token = None            # leader gave up or failed (errors are never stored): load ourselves, no lock
        else:
            got = _read(key)       # filled between our first read and taking the lock
            if _usable(got, validate) and not bypass:
                _release(r, key, token)
                _stats["hits"] += 1
                return CacheResult(got[0], True, max(0.0, _now() - got[1]), got[1], False)
    try:
        return _load(key, loader, ttl, cacheable, validate)
    finally:
        if token is not None:
            _release(_redis(), key, token)


def _release(r, key, token):
    if r is None:
        return
    try:
        r.eval(_RELEASE_LOCK, 1, LOCK_PREFIX + key[:MAX_KEY_LEN], token)
    except Exception as e:
        _redis_failed(e)


def _load(key, loader, ttl, cacheable, validate):
    _stats["misses"] += 1
    with error_scope() as sc:
        value = loader()
    ts = _now()
    ok = sc.count == 0
    if ok and cacheable is not None:
        try:
            ok = bool(cacheable(value))
        except Exception:
            ok = False
    if ok:
        ok = _write(key, value, _ttl_for(ttl, value), ts)
    if ok:
        _stats["loads"] += 1
    else:
        _stats["uncached_loads"] += 1
        if sc.count:
            logger.info(f"shared_cache: not caching {key} ({sc.count} call error(s): {sc.errors[0][0]})")
    return CacheResult(value, False, 0.0, ts, False)


# ── range handling for detail pages ──────────────────────────────────────────────────────────────────

def canonical_hours(hours, period_fn, ladder):
    """The cache key's range. Snaps `hours` UP to the largest ladder step that CloudWatch would query at an IDENTICAL
    period, so 1H / 3H / 6H / 1D share one entry. A step with a different period is never used (a coarser or finer
    period would change the datapoints, not just their number), and a value above the ladder keeps its own entry."""
    h = int(hours)
    if h <= 0:
        return h
    p = period_fn(h)
    best = h
    for step in sorted(ladder):
        if step >= h and period_fn(step) == p:
            best = step
    return best


def _epoch(t):
    d = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _is_points(v):
    return isinstance(v, list) and bool(v) and isinstance(v[0], dict) and "t" in v[0] and "v" in v[0]


def trim_points(points, hours, anchor_epoch):
    """Only the points inside [anchor - hours, anchor]. A point whose timestamp cannot be parsed is kept, not guessed at."""
    lo = anchor_epoch - hours * 3600
    out = []
    for p in points:
        try:
            e = _epoch(p["t"])
        except Exception:
            out.append(p)
            continue
        if lo <= e <= anchor_epoch + 1:
            out.append(p)
    return out


def trim_series(result, hours, anchor_epoch, keys=None):
    """Copy of a detail-page payload with every {t,v} series (including the per-mount dict) cut to `hours`, and
    period_hours reset to what the caller asked for. `keys` limits which top-level keys are trimmed."""
    if not isinstance(result, dict):
        return result
    out = dict(result)
    for k, v in result.items():
        if keys is not None and k not in keys:
            continue
        if _is_points(v):
            out[k] = trim_points(v, hours, anchor_epoch)
        elif isinstance(v, dict) and v and all(_is_points(x) or x == [] for x in v.values()):
            out[k] = {kk: (trim_points(x, hours, anchor_epoch) if x else x) for kk, x in v.items()}
    if "period_hours" in out:
        out["period_hours"] = int(hours)
    return out


def get_or_load_range(key_prefix, hours, loader, ttl, *, period_fn, ladder, trim_keys=None, bypass=False):
    """get_or_load for a payload that depends on a look-back of `hours`. The entry is stored under the canonical range
    and cut back to exactly the requested window before it is returned, so a caller never receives data for a range it
    did not ask for."""
    hours = int(hours)
    canon = canonical_hours(hours, period_fn, ladder)
    res = get_or_load_ex(f"{key_prefix}:h{canon}", lambda: loader(canon), ttl, bypass=bypass)
    if canon == hours:
        return res.value
    return trim_series(res.value, hours, res.ts, trim_keys)
