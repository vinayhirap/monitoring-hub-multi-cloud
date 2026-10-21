# app/utils/cwagent_cache.py
"""
CWAgent discovery cache (Phase B2), shared by the scheduled collector (app/collector/metrics/runner.py) and the instance
detail page (app/aws/collector_direct.py). Kept out of app/collector/disk_mounts.py so this module has no database import
and so tests that stub disk_mounts are unaffected.
"""

# (design notes)
# ListMetrics is a PAID call (per request). The dimension set CWAgent publishes under, and the mount list, only change
# when somebody changes the agent config, but the old cache was per-process memory with a 1 h TTL: every deploy made
# every instance run 1-2 ListMetrics again, and an instance WITHOUT the agent re-ran them every hour. Now:
#   * entries live in Redis via app/utils/shared_cache.py, shared by the scheduled collector (runner.py) and the
#     instance detail page (collector_direct.py), so one discovery serves both and survives a restart;
#   * found  ("agent reports these dimensions")  -> CWAGENT_DIM_TTL_SECONDS, default 4 h (was 1 h);
#   * absent ("no CWAgent for this instance")    -> CWAGENT_NEG_TTL_SECONDS, default 2 h (was 1 h);
#   * a ListMetrics that FAILED is never stored. all_cwagent_disk_dims() and the mem lookup swallow API errors and
#     return "nothing", which the old cache then kept for an hour as "no CWAgent" (a throttle or an expired role could
#     hide memory/disk data for an hour). The calls here go through shared_cache.track() so a failure is seen.
# Why these TTLs: at about 20 running instances the paid calls saved by going from 1 h to 4 h are tens of requests a
# day (cents a month); the value is the restart storm and the cross-process sharing, so the TTLs stay conservative.
# The cost of a long TTL is a STALE answer after a config change (a changed dimension set makes GetMetricData return
# nothing), so the lifetimes are bounded and every entry also carries a fingerprint of the resource row (id, name,
# tags): when discovery sees the instance change, the stored entry is ignored and rediscovered. invalidate_instance()
# is the explicit hook for code that knows the agent was just installed or reconfigured.
from app.utils import shared_cache

_CWAGENT_KINDS = ("mem", "disk", "present")


def cwagent_dim_ttl():
    return _env_seconds("CWAGENT_DIM_TTL_SECONDS", 4 * 3600)


def cwagent_neg_ttl():
    return _env_seconds("CWAGENT_NEG_TTL_SECONDS", 2 * 3600)


def _env_seconds(name, default, lo=60, hi=7 * 24 * 3600):
    import os
    try:
        v = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi)


def cwagent_key(kind, region, instance_id):
    return f"cwagent:{kind}:{region}:{instance_id}"


def resource_fingerprint(resource):
    """Stable digest of what identifies a discovered instance: db id, name and tags. None when there is no row to look at
    (the detail page has only an instance id)."""
    import hashlib
    import json
    if not isinstance(resource, dict):
        return None
    tags = resource.get("tags")
    if isinstance(tags, (bytes, bytearray)):
        tags = tags.decode("utf-8", "replace")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except ValueError:
            pass
    blob = json.dumps([resource.get("id"), resource.get("name"), tags], sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def _cwagent_ttl(envelope):
    return cwagent_dim_ttl() if envelope.get("val") else cwagent_neg_ttl()


def _fp_ok(fp):
    # an entry written without a fingerprint (detail page) or read without one is accepted; two different ones are not
    return lambda env: env.get("fp") is None or fp is None or env.get("fp") == fp


def cwagent_cache_get(kind, region, instance_id, fp=None):
    """-> (found, value). `value` is the stored result, which may be a negative ([] / None)."""
    res = shared_cache.peek(cwagent_key(kind, region, instance_id))
    if res is None or not isinstance(res.value, dict) or not _fp_ok(fp)(res.value):
        return False, None
    return True, res.value.get("val")


def cwagent_cache_put(kind, region, instance_id, value, fp=None):
    """Store a discovery result. Callers must not call this after a failed ListMetrics."""
    shared_cache.put(cwagent_key(kind, region, instance_id), {"fp": fp, "val": value}, _cwagent_ttl({"val": value}))


def cached_discovery(kind, region, instance_id, loader, fp=None):
    """get-or-load with single flight; `loader()` returns the value. Nothing is stored when it raised or any call it made
    recorded an error."""
    res = shared_cache.get_or_load_ex(
        cwagent_key(kind, region, instance_id), lambda: {"fp": fp, "val": loader()},
        _cwagent_ttl, validate=_fp_ok(fp))
    return res.value.get("val")


def cached_disk_dims(cw, resource, **usage):
    """all_cwagent_disk_dims() through the cache. `resource` is a resources row (id, resource_id, region, name, tags).
    `usage` is passed straight through (Phase A metering: usage_source / usage_tier), so a call that really happens is
    attributed exactly as before; a cache hit makes no call and records nothing."""
    from app.collector.disk_mounts import all_cwagent_disk_dims   # lazy: keeps this module free of the DB import chain
    iid = resource["resource_id"]
    return cached_discovery(
        "disk", resource.get("region"), iid,
        lambda: all_cwagent_disk_dims(shared_cache.track(cw, "cwagent.disk"), iid, **usage),
        resource_fingerprint(resource))


def invalidate_instance(region, instance_id):
    """Forget everything cached about CWAgent for one instance (the agent was just installed or reconfigured)."""
    for kind in _CWAGENT_KINDS:
        shared_cache.invalidate(cwagent_key(kind, region, instance_id))
