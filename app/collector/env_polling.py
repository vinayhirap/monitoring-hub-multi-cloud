# app/collector/env_polling.py
"""
Phase 4c -- environment-based polling (CloudWatch cost optimisation).

PURE decision logic: no database, no boto3, no scheduler imports. Everything the
collectors, the alert evaluator, the metric-meta API and the report script need to
agree on lives here, so none of them can drift.

DESIGN (confirmed with Vinay)
-----------------------------
  prod / unknown / ignore   polled exactly as before (prod-like, never slowed)
  uat    critical-class     unchanged (a P1-set metric on UAT is still P2 in the 4b matrix,
                            so it keeps prod cadence -- detection latency does not move)
  uat    everything else    base interval x POLL_UAT_MULTIPLIER (default 3)
  dev    critical-class     max(base, POLL_DEV_CRITICAL_INTERVAL_SECONDS)  (default 600 s)
  dev    everything else    max(base, POLL_DEV_INTERVAL_SECONDS)           (default 3600 s)
                            or not collected at all when POLL_DEV_NONCRITICAL_MODE=off

"critical-class" = a key in polling_model.p1_metric_keys() (the set evaluated on every
2-minute tick). A slowed interval is NEVER shorter than the base interval, and a base
interval of 24 h or more is never slowed (a daily metric costs one call a day).

WHY SLOWING DOES NOT LOSE HISTORY
---------------------------------
CloudWatch bills GetMetricData per metric REQUESTED, not per datapoint returned, and the
query Period is unchanged. A slowed resource is therefore asked for a longer WINDOW on the
poll that does run (`lookback_minutes`): same bucket size, same number of stored points,
only later. Charts stay continuous and dynamic baselines (metric_baseline / STL) see the
same density. What changes is freshness: the newest point is up to one slowed interval
older, and the alert engine is told so (extra_window_minutes) so it never calls that
"no data".

FLAG
----
ENV_POLLING_ENABLED (default off) AND ENV_AWARE_CLASSIFICATION on. Every function here that
changes a decision returns the unchanged prod answer when `enabled()` is false.
"""
import math
import os
import threading

FLAG_ENV = "ENV_POLLING_ENABLED"

PROD, UAT, DEV, UNKNOWN, IGNORE = "prod", "uat", "dev", "unknown", "ignore"

# Defaults (each overridable by an environment variable, read at call time).
DEFAULT_UAT_MULTIPLIER = 3
DEFAULT_DEV_INTERVAL_SECONDS = 3600
DEFAULT_DEV_CRITICAL_INTERVAL_SECONDS = 600
DEFAULT_DEV_NONCRITICAL_MODE = "slow"          # slow | off
MAX_MULTIPLIER = 12
MIN_INTERVAL_SECONDS = 120
MAX_INTERVAL_SECONDS = 86400

# Never slowed whatever their environment (see docs/POLLING_COST_ESTIMATE.md):
#   wafv2   dedicated 1-minute collector with its own window logic (waf.py)
EXEMPT_RESOURCE_TYPES = frozenset({"wafv2"})

# A base interval at or above this is left alone (daily publishers: one call a day).
NEVER_SLOW_AT_OR_ABOVE_SECONDS = 86400

# The alert evaluator counts one "cycle" per CYCLE_SECONDS (alert_evaluator.CYCLE_MINUTES).
CYCLE_SECONDS = 300

_TRUE = ("1", "true", "yes", "on")


# ── flags / settings ────────────────────────────────────────────────────
def requested():
    """ENV_POLLING_ENABLED set to a true value (does not look at the classification flag)."""
    return os.getenv(FLAG_ENV, "false").strip().lower() in _TRUE


def classification_enabled():
    return os.getenv("ENV_AWARE_CLASSIFICATION", "false").strip().lower() in _TRUE


def enabled():
    """Both flags on. Without ENV_AWARE_CLASSIFICATION the env_resolved column may not be
    migrated/maintained, and every label would be stale or absent -- the safe answer is
    'off' (prod behaviour), never 'slow everything'."""
    return requested() and classification_enabled()


def _int_env(name, default, lo, hi):
    try:
        v = int(float(os.getenv(name, "").strip()))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def uat_multiplier():
    return _int_env("POLL_UAT_MULTIPLIER", DEFAULT_UAT_MULTIPLIER, 1, MAX_MULTIPLIER)


def dev_interval_seconds():
    return _int_env("POLL_DEV_INTERVAL_SECONDS", DEFAULT_DEV_INTERVAL_SECONDS,
                    MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)


def dev_critical_interval_seconds():
    return _int_env("POLL_DEV_CRITICAL_INTERVAL_SECONDS", DEFAULT_DEV_CRITICAL_INTERVAL_SECONDS,
                    MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)


def dev_noncritical_mode():
    v = os.getenv("POLL_DEV_NONCRITICAL_MODE", DEFAULT_DEV_NONCRITICAL_MODE).strip().lower()
    return v if v in ("slow", "off") else DEFAULT_DEV_NONCRITICAL_MODE


def dev_eval_critical_only():
    """DEV: only critical-class metrics are evaluated for alerts (Vinay's design)."""
    return os.getenv("POLL_DEV_EVAL_CRITICAL_ONLY", "true").strip().lower() in _TRUE


# ── classification helpers ──────────────────────────────────────────────
def normalize_env(env):
    """'uat' | 'dev' | 'prod'. Anything else (None, unknown, ignore, junk) is prod-like."""
    e = (env or "").strip().lower()
    return e if e in (UAT, DEV) else PROD


def is_critical_metric(resource_type, db_metric_name, p1_keys=None):
    """True for a key in polling_model.p1_metric_keys(). `p1_keys` is injectable for tests."""
    if p1_keys is None:
        from app.collector import polling_model
        p1_keys = polling_model.p1_metric_keys()
    return (resource_type, (db_metric_name or "").lower()) in p1_keys


# ── the decision ────────────────────────────────────────────────────────
def effective_interval(env, base_interval, critical, resource_type=None, force_enabled=None):
    """Seconds between collections for this (environment, metric), or None when the metric
    is not collected at all (DEV non-critical with POLL_DEV_NONCRITICAL_MODE=off).

    Never below base_interval; prod-like environments and exempt types always get
    base_interval. `force_enabled` lets tests/report script evaluate the flag-ON answer
    without touching os.environ (None -> read the real flag)."""
    base = int(base_interval)
    on = enabled() if force_enabled is None else force_enabled
    if not on or base >= NEVER_SLOW_AT_OR_ABOVE_SECONDS or resource_type in EXEMPT_RESOURCE_TYPES:
        return base
    e = normalize_env(env)
    if e == PROD:
        return base
    if e == UAT:
        return base if critical else base * uat_multiplier()
    # dev
    if critical:
        return max(base, dev_critical_interval_seconds())
    if dev_noncritical_mode() == "off":
        return None
    return max(base, dev_interval_seconds())


def multiplier(env, base_interval, critical, resource_type=None, force_enabled=None):
    """effective/base as a float (1.0 = unchanged); None when not collected."""
    eff = effective_interval(env, base_interval, critical, resource_type, force_enabled)
    return None if eff is None else eff / float(base_interval)


def is_slowed(env, base_interval, critical, resource_type=None, force_enabled=None):
    eff = effective_interval(env, base_interval, critical, resource_type, force_enabled)
    return eff is None or eff > int(base_interval)


def extra_window_minutes(env, base_interval, critical, resource_type=None, force_enabled=None):
    """How many MORE minutes of freshness slack a slowed metric needs: (effective - base)/60.
    0 for anything unchanged. Added to the alert evaluation window and staleness limit."""
    eff = effective_interval(env, base_interval, critical, resource_type, force_enabled)
    if eff is None:
        return 0
    return max(0, int(math.ceil((eff - int(base_interval)) / 60.0)))


def alert_for_scale(env, base_interval, critical, resource_type=None, force_enabled=None):
    """Factor applied to the evaluator's required breach / healthy cycles.

    The evaluator counts one cycle per ~5 minutes and re-reads the cached last value on every
    pass, so one slowed reading would be counted several times and an alert would fire (or
    resolve) on a single datapoint. Scaling the cycle requirement by the number of cycles a
    reading stays current keeps 'N independent readings', which is also exactly the longer
    UAT alert-for duration. 1 for anything unchanged."""
    eff = effective_interval(env, base_interval, critical, resource_type, force_enabled)
    if eff is None:
        return 1
    base = int(base_interval)
    if eff <= base:
        return 1
    return max(1, int(math.ceil(eff / float(max(base, CYCLE_SECONDS)))))


def evaluate_for_alerts(env, critical, resource_type=None, force_enabled=None, base_interval=None):
    """False only for DEV non-critical metrics when POLL_DEV_EVAL_CRITICAL_ONLY is on.

    A metric whose base interval is a day or longer (certificate expiry, S3 storage) is never slowed by
    this feature, so it is always evaluated: skipping it would silence a rare daily signal (a DEV
    certificate nearing expiry) for no saving. `base_interval=None` (callers that do not know it) keeps
    the old answer."""
    on = enabled() if force_enabled is None else force_enabled
    if not on or resource_type in EXEMPT_RESOURCE_TYPES:
        return True
    if base_interval is not None and base_interval >= NEVER_SLOW_AT_OR_ABOVE_SECONDS:
        return True
    if normalize_env(env) != DEV:
        return True
    return bool(critical) or not dev_eval_critical_only()


# ── per-resource scheduling (stateful, in-memory) ───────────────────────
class PollState:
    """Last-collected time per key. Lives in the collector process (one leader per
    deployment); lost on restart, which simply makes every slowed resource due once on
    the first tick -- the same 'run now' the tier loop already does after a restart."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last = {}

    def get(self, key):
        with self._lock:
            return self._last.get(key)

    def set(self, key, when):
        with self._lock:
            self._last[key] = when

    def clear(self):
        with self._lock:
            self._last.clear()

    def snapshot(self):
        with self._lock:
            return dict(self._last)


_STATE = PollState()


def state():
    return _STATE


def is_due(effective, base, last, now):
    """True when a resource whose cadence is `effective` seconds should be collected on a
    tick at `now`. `last` is the previous collection (None -> due). A tolerance of half the
    base interval (at most 150 s) absorbs tick jitter: the 2-min loop really ticks at
    ~2-4 min and the 5-min tier on a 150 s-gapped grid."""
    if effective is None:
        return False
    if last is None or effective <= base:
        return True
    tol = min(base / 2.0, 150.0)
    return (now - last) >= (effective - tol)


def stagger_seconds(key_number, effective, base):
    """Deterministic first-collection offset in [0, effective - base] in whole base steps, so
    slowed resources do not all fall due on the same tick (3 busy ticks, then 6 idle ones).
    Cost-neutral; it only smooths load. `key_number` is the resource's primary key."""
    slots = max(1, int(round(effective / float(base))))
    return (int(key_number) % slots) * int(base)


def select_due(resources, tier, base_interval, now, *, critical=False, state_obj=None,
               force_enabled=None, record=True):
    """Filter `resources` (dicts with id, resource_type, env_resolved) down to the ones due
    on this tick and annotate each with `_poll_extra_min` (extra look-back minutes needed to
    cover the skipped ticks; 0 for anything on its normal cadence).

    Returns (due_resources, skipped_count). With the flag off it returns the input list
    UNCHANGED (same objects, same order, no annotation) -- the zero-behaviour-change path.

    `record=True` marks the due resources as collected at `now` (call it once per tick, right
    before the collection that uses the result). The report script passes record=False."""
    on = enabled() if force_enabled is None else force_enabled
    if not on:
        return list(resources), 0
    st = state_obj if state_obj is not None else _STATE
    due, skipped = [], 0
    for r in resources:
        rtype = r.get("resource_type")
        eff = effective_interval(r.get("env_resolved"), base_interval, critical, rtype, True)
        if eff is None:
            skipped += 1
            continue
        if eff <= base_interval:
            due.append(r)                       # prod-like / unchanged: untouched, not even annotated
            continue
        key = (r.get("id"), tier, bool(critical))
        last = st.get(key)
        if last is None:
            # first sight after a restart or reclassification: collect now, but pretend the
            # last collection was `stagger` ago so the fleet spreads over the slowed window
            collect = True
            extra = int(math.ceil((eff - base_interval) / 60.0))
            new_last = now - stagger_seconds(r.get("id") or 0, eff, base_interval)
        elif is_due(eff, base_interval, last, now):
            collect = True
            gap = min(now - last, eff * 2)       # a missed run widens the window, bounded
            extra = max(0, int(math.ceil((gap - base_interval) / 60.0)))
            new_last = now
        else:
            collect = False
        if not collect:
            skipped += 1
            continue
        r2 = dict(r)
        r2["_poll_extra_min"] = extra
        due.append(r2)
        if record:
            st.set(key, new_last)
    return due, skipped


def round_extra(extra_min, step=5):
    """Group look-back extensions into 5-minute steps so a fleet of slowed resources makes a
    handful of GetMetricData groups, not one per resource (more groups = more requests of
    fewer queries; the billed unit is per metric, but the call count is not free)."""
    extra = int(extra_min or 0)
    if extra <= 0:
        return 0
    return int(math.ceil(extra / float(step))) * step
