# tests/test_shared_cache.py
"""
Phase B2: shared (Redis) cache for paid cloud reads on UI and discovery paths.

Part 1  app/utils/shared_cache.py on its own, against a FakeRedis (two module instances sharing one FakeRedis stand in for
        two processes / a restart).
Part 2  the savings, proved with a COUNTING fake CloudWatch client:
          N simultaneous viewers -> 1 paid call, equivalent ranges -> 1 call, a restart -> 0 new calls,
          a failed call is never remembered as "no data", CWAgent discovery (detail page and collector), metric discovery.
No test touches a real Redis or a real cloud: tests/conftest.py forces the in-process backend, and Redis is faked here.
"""
import json
import math
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, install_real_utils_package  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────────────────────────────

class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t
        self.on_sleep = None

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s
        if self.on_sleep:
            self.on_sleep()


class FakeRedis:
    """The few commands shared_cache uses, with TTLs driven by the test clock."""

    def __init__(self, clock):
        self.d = {}
        self.clock = clock
        self.fail = False

    def _chk(self):
        if self.fail:
            raise ConnectionError("redis down")

    def _alive(self, k):
        v = self.d.get(k)
        if v is not None and v[1] is not None and v[1] <= self.clock.t:
            del self.d[k]
            return None
        return v

    def ping(self):
        self._chk()
        return True

    def get(self, k):
        self._chk()
        v = self._alive(k)
        return v[0] if v else None

    def set(self, k, val, ex=None, nx=False):
        self._chk()
        if nx and self._alive(k):
            return None
        self.d[k] = (val, self.clock.t + ex if ex else None)
        return True

    def delete(self, *ks):
        self._chk()
        return sum(1 for k in ks if self.d.pop(k, None) is not None)

    def scan_iter(self, match=None):
        self._chk()
        prefix = (match or "").rstrip("*")
        for k in list(self.d):
            if self._alive(k) and k.startswith(prefix):
                yield k

    def eval(self, script, n, key, token):
        self._chk()
        v = self._alive(key)
        if v and v[0] == token:
            del self.d[key]
            return 1
        return 0


def make_sc(monkeypatch, fake=None, clock=None):
    """A fresh, isolated shared_cache module (one 'process') wired to `fake` Redis and `clock`."""
    clock = clock or Clock()
    sc = load_module("app/utils/shared_cache.py")
    sc._now, sc._sleep = clock.now, clock.sleep
    if fake is not None:
        monkeypatch.setenv("SHARED_CACHE_BACKEND", "redis")
        sc._redis_client = fake
        sc._connect = lambda: fake
    else:
        monkeypatch.setenv("SHARED_CACHE_BACKEND", "memory")
    return sc, clock


def run_threads(n, fn):
    out, errs = [None] * n, [None] * n

    def work(i):
        try:
            out[i] = fn(i)
        except BaseException as e:      # noqa: BLE001 - the tests assert on it
            errs[i] = e
    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    return ts, out, errs


# ══ Part 1: the cache itself ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("use_redis", [True, False])
def test_hit_then_expiry_at_exactly_the_ttl(monkeypatch, use_redis):
    clock = Clock()
    sc, _ = make_sc(monkeypatch, FakeRedis(clock) if use_redis else None, clock)
    calls = []
    load = lambda: calls.append(1) or {"a": [1, 2]}
    r1 = sc.get_or_load_ex("k", load, 60)
    r2 = sc.get_or_load_ex("k", load, 60)
    assert (r1.value, r1.hit) == ({"a": [1, 2]}, False) and (r2.value, r2.hit) == ({"a": [1, 2]}, True)
    assert len(calls) == 1
    clock.t += 30
    assert sc.get_or_load_ex("k", load, 60).age_s == pytest.approx(30)
    clock.t += 31                                    # past the TTL: reload, never serve older than asked for
    assert sc.get_or_load("k", load, 60) == {"a": [1, 2]}
    assert len(calls) == 2


@pytest.mark.parametrize("use_redis", [True, False])
def test_n_simultaneous_viewers_cause_one_load(monkeypatch, use_redis):
    clock = Clock()
    sc, _ = make_sc(monkeypatch, FakeRedis(clock) if use_redis else None, clock)
    gate, calls = threading.Event(), []

    def slow():
        calls.append(1)
        gate.wait(5)
        return {"v": 42}
    ts, out, errs = run_threads(20, lambda i: sc.get_or_load("shared", slow, 300))
    time.sleep(0.3)                                  # let every viewer arrive while the one load is in flight
    gate.set()
    for t in ts:
        t.join(5)
    assert errs == [None] * 20
    assert len(calls) == 1, "20 viewers must cost ONE paid call"
    assert all(o == {"v": 42} for o in out)
    assert sc.stats()["flight_shared"] >= 1


def test_failed_load_is_shared_with_waiters_and_never_stored(monkeypatch):
    sc, _ = make_sc(monkeypatch, FakeRedis(Clock()))
    gate, calls = threading.Event(), []

    def boom():
        calls.append(1)
        gate.wait(5)
        raise RuntimeError("Throttling")
    ts, out, errs = run_threads(10, lambda i: sc.get_or_load("k", boom, 300))
    time.sleep(0.3)
    gate.set()
    for t in ts:
        t.join(5)
    assert len(calls) == 1, "a failing API must not be hit once per viewer"
    assert all(isinstance(e, RuntimeError) for e in errs)
    assert sc.peek("k") is None
    assert sc.get_or_load("k", lambda: calls.append(2) or "ok", 300) == "ok" and len(calls) == 2


def test_result_built_around_a_swallowed_error_is_returned_but_not_cached(monkeypatch):
    """The AWS helpers log an exception and return [] / {}; that must never be remembered as 'no data'."""
    sc, _ = make_sc(monkeypatch, FakeRedis(Clock()))
    calls = []

    def partial():
        calls.append(1)
        sc.note_error("gmd_series", RuntimeError("boom"))
        return {"mem": []}
    gate = threading.Event()
    slow = lambda: (gate.wait(5), partial())[1]
    ts, out, errs = run_threads(6, lambda i: sc.get_or_load("k", slow, 300))
    time.sleep(0.3)
    gate.set()
    for t in ts:
        t.join(5)
    assert len(calls) == 1 and all(o == {"mem": []} for o in out), "viewers during the failure share it"
    assert sc.peek("k") is None
    sc.get_or_load("k", partial, 300)
    assert len(calls) == 2, "the next viewer retries instead of reading a remembered failure"
    assert sc.stats()["uncached_loads"] == 2


def test_tracked_client_records_a_failure_and_reraises(monkeypatch):
    sc, _ = make_sc(monkeypatch, FakeRedis(Clock()))

    class Cw:
        def list_metrics(self, **k):
            raise RuntimeError("AccessDenied")

        region = "ap-south-1"

    def swallow_like_the_real_helpers():
        try:
            sc.track(Cw(), "cw").list_metrics(Namespace="CWAgent")
        except Exception:
            return []                                 # exactly what all_cwagent_disk_dims() does
    assert sc.get_or_load("k", swallow_like_the_real_helpers, 3600) == []
    assert sc.peek("k") is None, "an API failure must not be cached as an empty result"
    assert sc.track(Cw(), "cw").region == "ap-south-1", "non-callable attributes pass through"


def test_cacheable_predicate_ttl_callable_validate_and_bypass(monkeypatch):
    clock = Clock()
    sc, _ = make_sc(monkeypatch, FakeRedis(clock), clock)
    n = []
    # empty is not stored
    assert sc.get_or_load("e", lambda: n.append(1) or [], 60, cacheable=lambda v: len(v) > 0) == []
    sc.get_or_load("e", lambda: n.append(1) or [], 60, cacheable=lambda v: len(v) > 0)
    assert len(n) == 2
    # a different lifetime for a negative answer
    ttl = lambda v: 3600 if v["found"] else 120
    sc.get_or_load("neg", lambda: {"found": False}, ttl)
    clock.t += 121
    assert sc.peek("neg") is None
    sc.get_or_load("pos", lambda: {"found": True}, ttl)
    clock.t += 3000
    assert sc.peek("pos") is not None
    # validate: a stored entry the caller no longer trusts is a miss
    sc.get_or_load("fp", lambda: {"fp": "a"}, 600)
    assert sc.get_or_load("fp", lambda: {"fp": "b"}, 600, validate=lambda v: v["fp"] == "b") == {"fp": "b"}
    assert sc.get_or_load("fp", lambda: {"fp": "c"}, 600, validate=lambda v: v["fp"] == "b") == {"fp": "b"}
    # bypass = a manual refresh: skips the read, stores the fresh value
    sc.get_or_load("r", lambda: 1, 600)
    assert sc.get_or_load("r", lambda: 2, 600, bypass=True) == 2
    assert sc.get_or_load("r", lambda: 3, 600) == 2


def test_non_json_value_is_returned_uncached(monkeypatch):
    sc, _ = make_sc(monkeypatch, FakeRedis(Clock()))
    v = sc.get_or_load("k", lambda: {"s": {1, 2}}, 60)
    assert v == {"s": {1, 2}} and sc.peek("k") is None


def test_waiters_get_their_own_copy(monkeypatch):
    sc, _ = make_sc(monkeypatch, FakeRedis(Clock()))
    gate = threading.Event()
    ts, out, errs = run_threads(2, lambda i: sc.get_or_load("k", lambda: gate.wait(5) and {"l": [1]}, 60))
    time.sleep(0.2)
    gate.set()
    for t in ts:
        t.join(5)
    assert out[0] == out[1] == {"l": [1]} and out[0] is not out[1] and out[0]["l"] is not out[1]["l"]
    out[0]["l"].append("mutated")                    # one viewer changing its response ...
    assert out[1] == {"l": [1]}                      # ... cannot change another's
    assert sc.get_or_load("k", lambda: None, 60) == {"l": [1]}      # ... or the cached entry


def test_restart_costs_nothing_when_redis_holds_the_entry(monkeypatch):
    clock, fake = Clock(), None
    fake = FakeRedis(clock)
    sc1, _ = make_sc(monkeypatch, fake, clock)
    calls = []
    sc1.get_or_load("k", lambda: calls.append(1) or {"x": 1}, 300)
    sc2, _ = make_sc(monkeypatch, fake, clock)       # the process restarted: fresh module state, same Redis
    r = sc2.get_or_load_ex("k", lambda: calls.append(2) or {"x": 2}, 300)
    assert r.hit and r.value == {"x": 1} and calls == [1]


def test_second_process_waits_for_the_leader_instead_of_paying_twice(monkeypatch):
    clock = Clock()
    fake = FakeRedis(clock)
    sc, _ = make_sc(monkeypatch, fake, clock)
    fake.set(sc.LOCK_PREFIX + "k", "other-process:1", ex=30, nx=True)       # another process is mid-load
    clock.on_sleep = lambda: fake.set(sc.KEY_PREFIX + "k", json.dumps({"ts": clock.t, "v": 7}), ex=300)
    calls = []
    r = sc.get_or_load_ex("k", lambda: calls.append(1) or 99, 300)
    assert r.value == 7 and calls == [] and sc.stats()["lock_waits"] == 1


def test_second_process_loads_itself_when_the_leader_never_delivers(monkeypatch):
    clock = Clock()
    fake = FakeRedis(clock)
    sc, _ = make_sc(monkeypatch, fake, clock)
    fake.set(sc.LOCK_PREFIX + "k", "dead-process:1", ex=30, nx=True)        # leader crashed or failed (errors are not stored)
    calls = []
    assert sc.get_or_load("k", lambda: calls.append(1) or 5, 300) == 5 and calls == [1]


def test_lock_is_released_after_the_load_even_when_it_fails(monkeypatch):
    clock = Clock()
    fake = FakeRedis(clock)
    sc, _ = make_sc(monkeypatch, fake, clock)
    sc.get_or_load("ok", lambda: 1, 60)
    with pytest.raises(ValueError):
        sc.get_or_load("bad", lambda: (_ for _ in ()).throw(ValueError("x")), 60)
    assert not [k for k in fake.d if k.startswith(sc.LOCK_PREFIX)]


def test_invalidate_is_seen_by_every_process(monkeypatch):
    clock = Clock()
    fake = FakeRedis(clock)
    a, _ = make_sc(monkeypatch, fake, clock)
    b, _ = make_sc(monkeypatch, fake, clock)
    a.get_or_load("cwagent:mem:r:i-1", lambda: 1, 600)
    a.get_or_load("cwagent:disk:r:i-1", lambda: 1, 600)
    a.get_or_load("other", lambda: 1, 600)
    assert b.peek("cwagent:mem:r:i-1") is not None
    b.invalidate("cwagent:mem:r:i-1")
    assert a.peek("cwagent:mem:r:i-1") is None
    assert b.invalidate_prefix("cwagent:") == 1            # the disk one; the mem one is already gone
    assert a.peek("cwagent:disk:r:i-1") is None and a.peek("other") is not None


def test_redis_outage_falls_back_to_the_process_copy_and_recovers(monkeypatch):
    clock = Clock()
    fake = FakeRedis(clock)
    sc, _ = make_sc(monkeypatch, fake, clock)
    fake.fail = True
    calls = []
    load = lambda: calls.append(1) or {"v": 1}
    assert sc.get_or_load("k", load, 300) == {"v": 1}
    assert sc.get_or_load("k", load, 300) == {"v": 1} and len(calls) == 1, "memory fallback still dedupes"
    errs = sc.stats()["redis_errors"]
    for _ in range(20):
        sc.get_or_load("k", load, 300)
    assert sc.stats()["redis_errors"] == errs, "Redis is not hammered while it is down"
    fake.fail = False
    clock.t += sc.REDIS_RETRY_SECONDS + 1
    sc.get_or_load("k2", load, 300)
    assert fake.get(sc.KEY_PREFIX + "k2") is not None, "back on Redis after the retry window"


def test_memory_backend_never_touches_redis(monkeypatch):
    sc, _ = make_sc(monkeypatch, None)
    sc._connect = lambda: (_ for _ in ()).throw(AssertionError("must not connect"))
    assert sc.get_or_load("k", lambda: 1, 60) == 1 and sc.get_or_load("k", lambda: 2, 60) == 1


# ── ranges ──────────────────────────────────────────────────────────────────────────────────────────────

def _smart(h):                       # copy of collector_direct._smart_period (checked against the real one in Part 2)
    period = max(60, math.ceil(h * 3600 / 1440))
    step = 3600 if h > 63 * 24 else 300 if h > 15 * 24 else 60
    return math.ceil(period / step) * step


LADDER = (1, 3, 6, 24, 168, 720)


def test_canonical_hours_only_snaps_to_an_identical_period(monkeypatch):
    sc, _ = make_sc(monkeypatch)
    canon = {h: sc.canonical_hours(h, _smart, LADDER) for h in (1, 3, 6, 12, 24, 48, 168, 720, 4000)}
    assert canon == {1: 24, 3: 24, 6: 24, 12: 24, 24: 24,       # all 60 s
                     48: 48,                                    # 120 s: no ladder step has it -> own entry
                     168: 168, 720: 720,                        # 420 s / 1800 s: not shared with anything else
                     4000: 4000}


def _pts(anchor, hours_back, step_min=30):
    base = datetime.fromtimestamp(anchor, tz=timezone.utc)
    return [{"t": (base - timedelta(minutes=m)).isoformat(), "v": float(m)} for m in range(0, hours_back * 60 + 1, step_min)][::-1]


def test_trim_series_returns_exactly_the_requested_window(monkeypatch):
    sc, clock = make_sc(monkeypatch)
    payload = {"cpu": _pts(clock.t, 24), "disk_by_mount": {"/": _pts(clock.t, 24), "/d": []},
               "label": "x", "period_hours": 24, "bad": [{"t": "not-a-time", "v": 1}]}
    out = sc.trim_series(payload, 3, clock.t)
    ages_min = [float(p["v"]) for p in out["cpu"]]
    assert max(ages_min) <= 180 and min(ages_min) == 0 and len(out["cpu"]) == 7      # 0..180 min every 30
    assert [float(p["v"]) for p in out["disk_by_mount"]["/"]] == ages_min, "nested per-mount series are cut the same way"
    assert out["disk_by_mount"]["/d"] == [] and out["label"] == "x" and out["period_hours"] == 3
    assert out["bad"] == payload["bad"], "a point whose time cannot be read is kept, not guessed at"
    assert len(payload["cpu"]) == 49, "the cached payload itself is not mutated"
    only = sc.trim_series(payload, 3, clock.t, keys={"cpu"})
    assert len(only["cpu"]) == 7 and len(only["disk_by_mount"]["/"]) == 49


def test_get_or_load_range_one_entry_for_equivalent_ranges_and_never_a_wider_window(monkeypatch):
    sc, clock = make_sc(monkeypatch, FakeRedis(Clock()))
    clock = sc._now.__self__
    fetched = []

    def loader(h):
        fetched.append(h)
        return {"cpu": _pts(clock.t, h), "period_hours": h}
    got = {h: sc.get_or_load_range("p", h, loader, 300, period_fn=_smart, ladder=LADDER) for h in (1, 3, 6, 24)}
    assert fetched == [24], "1H, 3H, 6H and 1D are ONE fetch"
    for h, r in got.items():
        assert r["period_hours"] == h
        assert max(float(p["v"]) for p in r["cpu"]) <= h * 60, f"{h}H response contains older data than asked for"
        assert len(r["cpu"]) == h * 2 + 1
    sc.get_or_load_range("p", 168, loader, 300, period_fn=_smart, ladder=LADDER)
    assert fetched == [24, 168], "a different CloudWatch period gets its own entry"


# ══ Part 2: counting fake CloudWatch ═════════════════════════════════════════════════════════════════

class FakeAws:
    """Counts every paid-call shape: ListMetrics requests and GetMetricData (calls and metrics requested)."""

    def __init__(self, agent=True):
        self.agent = agent
        self.list_metrics_calls = 0
        self.gmd_calls = 0
        self.gmd_metrics = 0
        self.fail_list = False
        self.fail_gmd = False
        self.gate = None
        self.now = datetime.now(timezone.utc)

    def get_session(self, region=None, role_arn=None, external_id=None, account=None):
        outer = self

        class Sess:
            def client(self, name, config=None):
                return outer.cw()
        return Sess()

    def cw(self):
        outer = self

        class Cw:
            def list_metrics(self, Namespace=None, MetricName=None, Dimensions=None):
                outer.list_metrics_calls += 1
                if outer.fail_list:
                    raise RuntimeError("Throttling: rate exceeded")
                if not outer.agent:
                    return {"Metrics": []}
                iid = Dimensions[0]["Value"]
                name = MetricName or "mem_used_percent"
                if name == "Memory % Committed Bytes In Use":
                    return {"Metrics": []}
                dims = [{"Name": "InstanceId", "Value": iid}]
                if name == "disk_used_percent":          # the real per-mount shape CWAgent publishes
                    dims += [{"Name": "path", "Value": "/"}, {"Name": "device", "Value": "nvme0n1p1"},
                             {"Name": "fstype", "Value": "ext4"}]
                return {"Metrics": [{"MetricName": name, "Dimensions": dims}]}

            def get_metric_data(self, MetricDataQueries, StartTime, EndTime, ScanBy=None, NextToken=None):
                outer.gmd_calls += 1
                outer.gmd_metrics += len(MetricDataQueries)
                if outer.gate is not None:
                    outer.gate.wait(5)
                if outer.fail_gmd:
                    raise RuntimeError("Throttling")
                span = (EndTime - StartTime).total_seconds() / 60
                mins = [m for m in (5, 90, 300, 1000) if m <= span]
                return {"MetricDataResults": [
                    {"Id": q["Id"], "Timestamps": [EndTime - timedelta(minutes=m) for m in mins], "Values": [1.0] * len(mins)}
                    for q in MetricDataQueries]}
        return Cw()


def _history(resource_type, identifier, metric, start, end, **k):
    """DB-backed series: one point every 30 min inside [start, end] (like _metric_history_query_range)."""
    out, t = [], end
    while t >= start:
        out.append({"t": t.isoformat(), "v": 1.0})
        t -= timedelta(minutes=30)
    return out[::-1]


def _fake_disk_dims(cw, iid, **usage):
    """Behaves like app/collector/disk_mounts.all_cwagent_disk_dims: logs and swallows API errors, returning []."""
    try:
        r = cw.list_metrics(Namespace="CWAgent", MetricName="disk_used_percent",
                            Dimensions=[{"Name": "InstanceId", "Value": iid}])
    except Exception:
        return []
    return [(m["Dimensions"], "/", "disk_used_percent", "disk_used_percent", False) for m in r.get("Metrics", [])]


@pytest.fixture
def cd(monkeypatch):
    """collector_direct loaded the way the other isolated tests load it, wired to a FakeAws and a fake Redis."""
    from tests.test_collector_direct import _stub_and_load, _RoutingConn
    aws, clock = FakeAws(), Clock(time.time())
    fake = FakeRedis(clock)

    def load():
        mod = _stub_and_load(_RoutingConn())
        mod.get_session = aws.get_session
        mod._metric_history_query_range = _history
        mod.all_cwagent_disk_dims = _fake_disk_dims
        return mod
    mod = load()
    sc = mod.shared_cache
    monkeypatch.setenv("SHARED_CACHE_BACKEND", "redis")
    sc._now, sc._sleep = clock.now, clock.sleep
    sc._redis_client = fake
    sc._connect = lambda: fake
    monkeypatch.setattr(mod.cwagent_cache.shared_cache, "_now", clock.now, raising=False)

    class Env:
        pass
    e = Env()
    e.mod, e.aws, e.clock, e.fake, e.sc, e.load = mod, aws, clock, fake, sc, load
    e.view = lambda hours=6, iid="i-0abc": mod.get_ec2_metric_series(iid, "ap-south-1", hours, account={"id": 7})
    yield e
    sc._connect = lambda: (_ for _ in ()).throw(RuntimeError("no redis in tests"))


def test_canonical_hours_agrees_with_the_real_smart_period(cd):
    """Part 1 used a copy of _smart_period; the snapping must follow the real one."""
    real = cd.mod._smart_period
    assert [real(h) for h in (1, 3, 6, 24, 168, 720)] == [60, 60, 60, 60, 420, 1800]
    assert all(real(h) == _smart(h) for h in range(1, 800))
    assert cd.mod._ladder() == LADDER


def test_n_simultaneous_viewers_of_an_ec2_detail_page_cost_one_paid_call_set(cd):
    cd.aws.gate = threading.Event()
    ts, out, errs = run_threads(12, lambda i: cd.view(6))
    time.sleep(0.4)
    cd.aws.gate.set()
    for t in ts:
        t.join(8)
    assert errs == [None] * 12
    assert cd.aws.gmd_calls == 1, "12 viewers, one GetMetricData"
    assert cd.aws.list_metrics_calls == 3, "one presence + one memory + one disk discovery, not 12x"
    assert all(o == out[0] for o in out)
    assert out[0]["cwagent_installed"] is True and out[0]["mem_utilization"] and out[0]["disk_used_percent"]


def test_range_variants_share_one_fetch_and_never_return_more_than_asked(cd):
    results = {h: cd.view(h) for h in (1, 3, 6, 24)}
    assert cd.aws.gmd_calls == 1 and cd.aws.list_metrics_calls == 3
    now = cd.clock.now()
    for h, r in results.items():
        assert r["period_hours"] == h and r["period_secs"] == 60
        for key in ("cpu", "mem_utilization", "disk_used_percent"):
            for p in r[key]:
                age_h = (datetime.fromtimestamp(now, tz=timezone.utc) - datetime.fromisoformat(p["t"])).total_seconds() / 3600
                assert age_h <= h + 0.05, f"{key} in the {h}H response has a point {age_h:.2f} h old"
        assert set(r["disk_used_percent_by_mount"]) == {"/"}
    assert len(results[1]["cpu"]) < len(results[6]["cpu"]) < len(results[24]["cpu"])
    cd.view(168)
    assert cd.aws.gmd_calls == 2, "1W has a different CloudWatch period (420 s) so it is its own entry"
    assert cd.view(168)["period_secs"] == 420 and cd.aws.gmd_calls == 2


def test_restart_does_not_pay_again(cd):
    cd.view(6)
    paid = (cd.aws.gmd_calls, cd.aws.list_metrics_calls)
    cd.sc._l1.clear()                       # process memory is gone ...
    restarted = cd.load()                   # ... and the module is loaded afresh
    out = restarted.get_ec2_metric_series("i-0abc", "ap-south-1", 3, account={"id": 7})
    assert (cd.aws.gmd_calls, cd.aws.list_metrics_calls) == paid, "Redis served the entry across the restart"
    assert out["cwagent_installed"] is True


def test_entry_expires_at_the_old_ttl(cd):
    cd.view(6)
    cd.clock.t += 301
    cd.view(6)
    assert cd.aws.gmd_calls == 2, "detail data is never older than the 300 s it always was"


def test_failed_getmetricdata_is_not_remembered_as_no_data(cd):
    cd.aws.fail_gmd = True
    first = cd.view(6)
    assert first["mem_utilization"] == [], "the failed call yields an empty series for this view only"
    cd.aws.fail_gmd = False
    second = cd.view(6)
    assert second["mem_utilization"], "the very next view retries and gets real data"
    assert cd.aws.gmd_calls == 2
    cd.view(6)
    assert cd.aws.gmd_calls == 2, "...and the good result is cached"


def test_failed_listmetrics_is_not_remembered_as_no_cwagent(cd):
    cd.aws.fail_list = True
    r1 = cd.view(6)
    assert r1["cwagent_installed"] is False
    cd.aws.fail_list = False
    r2 = cd.view(6)
    assert r2["cwagent_installed"] is True and r2["mem_utilization"], "no 5-minute or 1-hour wait to see the agent"
    stored = [json.loads(v[0])["v"] for k, v in cd.fake.d.items() if "cwagent:present" in k]
    assert stored == [True], "only the successful answer is stored"


def test_agent_absent_is_remembered_for_the_old_five_minutes_only_on_the_detail_page(cd):
    cd.aws.agent = False
    cd.view(6)
    cd.view(6)
    assert cd.aws.list_metrics_calls == 1
    cd.clock.t += 301                        # series entry and the 5-minute negative both expire
    cd.aws.agent = True
    assert cd.view(6)["cwagent_installed"] is True, "installing the agent shows up within 5 minutes, as before"


# ── CWAgent discovery for the collector ─────────────────────────────────────────────────────────────────

RES = {"id": 5, "resource_id": "i-0abc", "resource_type": "ec2", "name": "web-1", "region": "ap-south-1",
       "tags": json.dumps({"Name": "web-1", "_instance_type": "t3.medium"})}


def _load_runner():
    """app/collector/metrics/runner.py in isolation. Self-contained (does not rely on another test module's loader, which
    only works when an earlier test has already imported the real `app` package)."""
    install_real_utils_package()
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.aws.sts", get_boto3_session=lambda *a, **k: None)
    install_stub("app.aws.boto_config", STANDARD_RETRY={})
    install_stub("app.collector.metrics_writer", write_metrics_batch=lambda rows: None,
                 write_metric_history_batch=lambda rows, **kw: None)
    install_stub("app.collector.disk_mounts", all_cwagent_disk_dims=_fake_disk_dims,
                 ensure_disk_mount_metric_registered=lambda *a, **k: None)
    install_stub("app.collector.api_usage", record=lambda *a, **k: None)
    for name in ("polling_model", "env_polling"):          # the real, dependency-free modules
        real = load_module(f"app/collector/{name}.py")
        sys.modules[f"app.collector.{name}"] = real
        setattr(sys.modules["app.collector"], name, real)
    return load_module("app/collector/metrics/runner.py")


@pytest.fixture
def rn(monkeypatch):
    runner = _load_runner()
    cwc = sys.modules["app.utils.cwagent_cache"] if "app.utils.cwagent_cache" in sys.modules else runner.cwagent_cache
    sc = runner.shared_cache
    aws, clock = FakeAws(), Clock(time.time())
    fake = FakeRedis(clock)
    monkeypatch.setenv("SHARED_CACHE_BACKEND", "redis")
    sc._now, sc._sleep, sc._redis_client = clock.now, clock.sleep, fake
    sc._connect = lambda: fake
    sys.modules["app.collector.disk_mounts"].all_cwagent_disk_dims = _fake_disk_dims

    class Env:
        pass
    e = Env()
    e.runner, e.aws, e.clock, e.fake, e.sc, e.cwc = runner, aws, clock, fake, sc, cwc
    e.cw = aws.cw()
    yield e
    sc._connect = lambda: (_ for _ in ()).throw(RuntimeError("no redis in tests"))


def test_collector_disk_discovery_is_cached_shared_and_survives_a_restart(rn):
    first = rn.runner._cached_disk_dims(rn.cw, RES)
    assert first and rn.aws.list_metrics_calls == 1
    rn.runner._cached_disk_dims(rn.cw, RES)
    rn.sc._l1.clear()                                       # restart
    again = rn.runner._cached_disk_dims(rn.cw, RES)
    assert rn.aws.list_metrics_calls == 1, "one ListMetrics, however many polls and restarts"
    assert [m[1] for m in again] == ["/"], "a cached entry unpacks exactly like a fresh one"
    for dims, path, metric_name, cw_name, invert in again:
        assert (path, invert) == ("/", False)


def test_collector_ttls_found_four_hours_absent_two_hours(rn, monkeypatch):
    monkeypatch.delenv("CWAGENT_DIM_TTL_SECONDS", raising=False)
    monkeypatch.delenv("CWAGENT_NEG_TTL_SECONDS", raising=False)
    assert (rn.cwc.cwagent_dim_ttl(), rn.cwc.cwagent_neg_ttl()) == (4 * 3600, 2 * 3600)
    rn.runner._cached_disk_dims(rn.cw, RES)
    rn.clock.t += 4 * 3600 - 5
    rn.runner._cached_disk_dims(rn.cw, RES)
    assert rn.aws.list_metrics_calls == 1
    rn.clock.t += 10
    rn.runner._cached_disk_dims(rn.cw, RES)
    assert rn.aws.list_metrics_calls == 2, "found: rediscovered after 4 h"
    # absent
    other = dict(RES, resource_id="i-0none", id=6)
    rn.aws.agent = False
    rn.runner._cached_disk_dims(rn.cw, other)
    n = rn.aws.list_metrics_calls
    rn.clock.t += 2 * 3600 - 5
    assert rn.runner._cached_disk_dims(rn.cw, other) == [] and rn.aws.list_metrics_calls == n
    rn.clock.t += 10
    rn.runner._cached_disk_dims(rn.cw, other)
    assert rn.aws.list_metrics_calls == n + 1, "absent: rechecked after 2 h"


def test_ttl_overrides_are_clamped(rn, monkeypatch):
    monkeypatch.setenv("CWAGENT_DIM_TTL_SECONDS", "5")
    monkeypatch.setenv("CWAGENT_NEG_TTL_SECONDS", "banana")
    assert rn.cwc.cwagent_dim_ttl() == 60 and rn.cwc.cwagent_neg_ttl() == 2 * 3600


def test_collector_failed_listmetrics_is_not_cached_as_no_cwagent(rn):
    rn.aws.fail_list = True
    assert rn.runner._cached_disk_dims(rn.cw, RES) == []            # all_cwagent_disk_dims swallowed the error
    rn.aws.fail_list = False
    assert rn.runner._cached_disk_dims(rn.cw, RES), "the old cache hid this instance's disk data for an hour"
    assert rn.aws.list_metrics_calls == 2


def test_real_all_cwagent_disk_dims_failure_is_seen_through_the_tracked_client(rn):
    from tests.test_cwagent_windows_disk import _load_disk_mounts
    real = _load_disk_mounts()
    sys.modules["app.collector.disk_mounts"].all_cwagent_disk_dims = real.all_cwagent_disk_dims
    rn.aws.fail_list = True
    assert rn.runner._cached_disk_dims(rn.cw, RES) == []
    assert rn.sc.peek(rn.cwc.cwagent_key("disk", "ap-south-1", "i-0abc")) is None
    rn.aws.fail_list = False
    assert rn.runner._cached_disk_dims(rn.cw, RES)


def test_changed_instance_is_rediscovered_and_unchanged_is_not(rn):
    rn.runner._cached_disk_dims(rn.cw, RES)
    rn.runner._cached_disk_dims(rn.cw, dict(RES, resource_type="ec2"))
    assert rn.aws.list_metrics_calls == 1
    changed = dict(RES, tags=json.dumps({"Name": "web-1", "_instance_type": "t3.large", "cwagent": "yes"}))
    rn.runner._cached_disk_dims(rn.cw, changed)
    assert rn.aws.list_metrics_calls == 2, "discovery saw the instance change -> the cached answer is ignored"
    assert rn.cwc.resource_fingerprint(RES) == rn.cwc.resource_fingerprint(dict(RES, tags=dict(json.loads(RES["tags"]))))


def test_invalidate_instance_forces_rediscovery(rn):
    rn.runner._cached_disk_dims(rn.cw, RES)
    rn.cwc.invalidate_instance("ap-south-1", "i-0abc")
    rn.runner._cached_disk_dims(rn.cw, RES)
    assert rn.aws.list_metrics_calls == 2


def test_collector_memory_batch_remembers_found_and_remembers_absent_only_when_nothing_failed(rn):
    a, b = dict(RES, resource_id="i-a", id=1), dict(RES, resource_id="i-b", id=2)

    class Cw:
        def __init__(self, outer):
            self.o = outer

        def list_metrics(self, Namespace=None, MetricName=None, Dimensions=None):
            self.o.aws.list_metrics_calls += 1
            iid = Dimensions[0]["Value"]
            if iid == "i-bad":
                raise RuntimeError("Throttling")
            if iid == "i-a" and MetricName == "mem_used_percent":
                return {"Metrics": [{"Dimensions": [{"Name": "InstanceId", "Value": iid}]}]}
            return {"Metrics": []}
    cw = Cw(rn)
    bad = dict(RES, resource_id="i-bad", id=3)

    # batch with a failure: a is remembered, b (absent) and bad are NOT
    res = rn.runner._ec2_instances_with_cwagent_mem_dims(cw, [a, b, bad])
    assert set(res) == {"i-a"}
    calls1 = rn.aws.list_metrics_calls
    rn.runner._ec2_instances_with_cwagent_mem_dims(cw, [a, b, bad])
    assert rn.aws.list_metrics_calls - calls1 == (2 + 1), "only b (Linux+Windows names) and bad (1, it throws) are asked again"

    # clean batch: absent is remembered
    rn.runner._ec2_instances_with_cwagent_mem_dims(cw, [b])
    calls2 = rn.aws.list_metrics_calls
    out = rn.runner._ec2_instances_with_cwagent_mem_dims(cw, [a, b])
    assert rn.aws.list_metrics_calls == calls2 and set(out) == {"i-a"}
    assert out["i-a"][1] and out["i-a"][2] == "mem_used_percent", "(resource, dims, metric name) shape is unchanged"
    rn.sc._l1.clear()
    rn.runner._ec2_instances_with_cwagent_mem_dims(cw, [a, b])
    assert rn.aws.list_metrics_calls == calls2, "restart: still no ListMetrics"


# ── metric-catalog discovery ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mc(monkeypatch):
    install_real_utils_package()
    install_stub("app.account_names", account_label=lambda i: f"account {i}", plural=lambda n, w: w)
    from tests.test_metric_catalog import _load
    from unittest.mock import MagicMock
    mod = _load(MagicMock())
    clock = Clock(time.time())
    fake = FakeRedis(clock)
    sc = mod.shared_cache
    monkeypatch.setenv("SHARED_CACHE_BACKEND", "redis")
    sc._now, sc._sleep, sc._redis_client = clock.now, clock.sleep, fake
    sc._connect = lambda: fake

    class Env:
        pass
    e = Env()
    e.mod, e.clock, e.sc, e.calls, e.names, e.fail = mod, clock, sc, [], {"CPUUtilization", "NetworkIn"}, False

    def fake_discover(acc, namespace, region):
        e.calls.append((namespace, region))
        if e.fail:
            raise RuntimeError("AccessDenied")
        return set(e.names)
    mod._discover_aws_metrics = fake_discover
    yield e
    sc._connect = lambda: (_ for _ in ()).throw(RuntimeError("no redis in tests"))


ACC = {"id": 7, "provider": "aws", "default_region": "ap-south-1"}


def test_discovery_simultaneous_expands_cost_one_listmetrics(mc):
    gate = threading.Event()
    orig = mc.mod._discover_aws_metrics
    mc.mod._discover_aws_metrics = lambda a, n, r: gate.wait(5) and orig(a, n, r)
    ts, out, errs = run_threads(10, lambda i: mc.mod._discover_cached("aws", ACC, 7, "AWS/EC2", None, False))
    time.sleep(0.3)
    gate.set()
    for t in ts:
        t.join(5)
    assert errs == [None] * 10 and len(mc.calls) == 1
    assert all(o[0] == {"CPUUtilization", "NetworkIn"} for o in out)


def test_discovery_cached_across_restart_with_age_and_refresh_bypass(mc):
    names, r1 = mc.mod._discover_cached("aws", ACC, 7, "AWS/EC2", None, False)
    assert names == {"CPUUtilization", "NetworkIn"} and not r1.hit
    mc.clock.t += 120
    mc.sc._l1.clear()                                        # restart
    names2, r2 = mc.mod._discover_cached("aws", ACC, 7, "AWS/EC2", None, False)
    assert len(mc.calls) == 1 and r2.hit and int(r2.age_s) == 120, "age is exposed"
    mc.names = {"CPUUtilization", "NetworkIn", "DiskReadOps"}
    names3, r3 = mc.mod._discover_cached("aws", ACC, 7, "AWS/EC2", None, True)       # manual refresh
    assert "DiskReadOps" in names3 and len(mc.calls) == 2 and not r3.hit
    assert "DiskReadOps" in mc.mod._discover_cached("aws", ACC, 7, "AWS/EC2", None, False)[0]


def test_discovery_cache_is_per_account_namespace_and_region(mc):
    mc.mod._discover_cached("aws", ACC, 7, "AWS/EC2", None, False)
    mc.mod._discover_cached("aws", ACC, 8, "AWS/EC2", None, False)                    # other account
    mc.mod._discover_cached("aws", ACC, 7, "AWS/RDS", None, False)                    # other namespace
    mc.mod._discover_cached("aws", ACC, 7, "AWS/EC2", "us-east-1", False)             # other region
    mc.mod._discover_cached("aws", ACC, 7, "AWS/EC2", "ap-south-1", False)            # == the account default: shared
    assert len(mc.calls) == 4


def test_discovery_failure_and_empty_result_are_never_cached(mc):
    mc.fail = True
    with pytest.raises(RuntimeError):
        mc.mod._discover_cached("aws", ACC, 7, "AWS/EC2", None, False)
    mc.fail = False
    mc.names = set()
    assert mc.mod._discover_cached("aws", ACC, 7, "AWS/EC2", None, False)[0] == set()
    mc.names = {"NewMetric"}                                  # the namespace has just started publishing
    assert mc.mod._discover_cached("aws", ACC, 7, "AWS/EC2", None, False)[0] == {"NewMetric"}
    assert len(mc.calls) == 3


def test_discover_endpoint_takes_a_refresh_flag_and_reports_cache_state(mc):
    import inspect
    assert "refresh" in inspect.signature(mc.mod.discover_namespace_metrics).parameters
    src = inspect.getsource(mc.mod.discover_namespace_metrics)
    assert '"cached"' in src and '"cache_age_s"' in src and src.count("**cache_info") == 2


# ── with Phase A's real metering: the signal the PROD comparison SQL reads ────────────────────────────

def test_real_metering_sees_one_paid_call_set_for_n_viewers_and_nothing_for_cache_hits(cd):
    """api_usage.record() sits at the cloud-call sites (Phase A), so only calls that really happen are counted: 12
    simultaneous viewers record ONE GetMetricData and one presence + one memory ListMetrics, and every later view
    (a cache hit) records nothing."""
    au = load_module("app/collector/api_usage.py")
    cd.mod.api_usage = au
    cd.aws.gate = threading.Event()
    ts, out, errs = run_threads(12, lambda i: cd.view(6))
    time.sleep(0.4)
    cd.aws.gate.set()
    for t in ts:
        t.join(8)
    assert errs == [None] * 12
    snap = au.snapshot()
    assert snap[("aws", "ui", "ui", "GetMetricData")] == (1, 2), "1 request, 2 billed metrics (memory + the root disk)"
    assert snap[("aws", "ui", "ui", "ListMetrics")] == (2, 2), "presence + memory discovery (the disk helper is faked here)"
    before = dict(snap)
    for h in (1, 3, 6, 24):
        cd.view(h)
    cd.view(6)
    assert au.snapshot() == before, "cache hits, including other ranges, record no paid call"
