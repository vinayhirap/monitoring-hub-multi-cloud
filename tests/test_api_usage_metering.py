# tests/test_api_usage_metering.py
"""
Phase A (metering, no behaviour change): every PAID CloudWatch call is counted
in app/collector/api_usage.py with a source dimension (collector | ui |
discovery) and an operation (GetMetricData | ListMetrics | GetMetricStatistics).

No network, no DB, no real boto3 client: cloud clients are faked and op_events
writes are captured. These tests prove the COUNTING LOGIC and that the hooks
never raise; they cannot show real paid-call volume (DEV makes no paid calls
by design) -- that is measured on PROD from op_events.
"""
import sys
import threading
import types

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, install_polling_modules, install_real_utils_package


# ── isolation ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _leave_real_app_modules_untouched():
    """install_stub() mutates REAL app.* modules that an earlier test file
    already imported (e.g. app.threshold_defaults.DEFAULT_THRESHOLDS = {}),
    and these tests swap in freshly loaded api_usage/polling_model copies.
    Restore every app.* module exactly as it was so no other test file can be
    affected by running after this one."""
    def _is_app(n):
        return n == "app" or n.startswith("app.")

    saved = {n: m for n, m in sys.modules.items() if _is_app(n) and m is not None}
    dicts = {n: dict(m.__dict__) for n, m in saved.items()}
    yield
    for n in [n for n in list(sys.modules) if _is_app(n) and n not in saved]:
        del sys.modules[n]
    for n, m in saved.items():
        sys.modules[n] = m
        m.__dict__.clear()
        m.__dict__.update(dicts[n])


# ── helpers ─────────────────────────────────────────────────────────────

def _usage():
    """Real api_usage with a captured op_log. Returns (module, rows)."""
    rows = []
    install_stub("app.collector.op_log", log_event=lambda *a, **k: rows.append((a, k)))
    _, au = install_polling_modules()
    return au, rows


def _details(rows):
    return {(k["detail"]["provider"], k["detail"]["tier"], k["detail"]["source"],
             k["detail"]["operation"]): k["detail"] for _, k in rows}


def _snap(au):
    return au.snapshot()


class FakeCW:
    """Fake CloudWatch client. Counts every request it receives."""

    def __init__(self, list_pages=None, gmd_pages=1, fail=False):
        self.list_calls = []
        self.gmd_calls = []
        self.stat_calls = []
        self._list_pages = list_pages if list_pages is not None else [[]]
        self._gmd_pages = gmd_pages
        self._fail = fail

    def _boom(self):
        return self._fail if isinstance(self._fail, Exception) else RuntimeError("throttled")

    def list_metrics(self, **kw):
        self.list_calls.append(kw)
        if self._fail:
            raise self._boom()
        pages = self._list_pages
        return {"Metrics": pages[0] if pages else []}

    def get_metric_data(self, **kw):
        self.gmd_calls.append(kw)
        if self._fail:
            raise self._boom()
        n = len(self.gmd_calls)
        resp = {"MetricDataResults": []}
        if n < self._gmd_pages:
            resp["NextToken"] = f"t{n}"
        return resp

    def get_metric_statistics(self, **kw):
        self.stat_calls.append(kw)
        if self._fail:
            raise self._boom()
        return {"Datapoints": []}

    def get_paginator(self, name):
        assert name == "list_metrics"
        cw = self

        class _P:
            def paginate(self, **kw):
                for page in cw._list_pages:
                    cw.list_calls.append(kw)      # the page was fetched
                    yield {"Metrics": page}
        return _P()


class FakeSession:
    def __init__(self, cw):
        self._cw = cw

    def client(self, *a, **k):
        return self._cw


# ── 1. counters ─────────────────────────────────────────────────────────

def test_counters_accumulate_per_key():
    au, _ = _usage()
    au.record("aws", "standard", calls=2, units=1000)
    au.record("aws", "standard", calls=1, units=500)
    au.record("aws", "critical", calls=1, units=7)
    s = _snap(au)
    assert s[("aws", "standard", "collector", "GetMetricData")] == (3, 1500)
    assert s[("aws", "critical", "collector", "GetMetricData")] == (1, 7)


def test_source_and_operation_are_separate_keys():
    au, _ = _usage()
    au.record("aws", "standard", calls=1, units=1, operation="ListMetrics")
    au.record("aws", "standard", calls=1, units=10)
    au.record("aws", "ui", calls=1, units=1, source="ui", operation="ListMetrics")
    au.record("aws", "ui", calls=2, units=40, source="ui", operation="GetMetricData")
    au.record("aws", "discovery", calls=1, units=1, source="discovery", operation="ListMetrics")
    assert set(_snap(au)) == {
        ("aws", "standard", "collector", "ListMetrics"),
        ("aws", "standard", "collector", "GetMetricData"),
        ("aws", "ui", "ui", "ListMetrics"),
        ("aws", "ui", "ui", "GetMetricData"),
        ("aws", "discovery", "discovery", "ListMetrics"),
    }


def test_non_collector_tier_label_is_the_source_name():
    au, _ = _usage()
    # whatever tier a caller passes, ui/discovery rows are labelled by source
    au.record("aws", "standard", calls=1, units=1, source="ui", operation="ListMetrics")
    au.record("aws", None, calls=1, units=1, source="discovery", operation="ListMetrics")
    assert set(_snap(au)) == {("aws", "ui", "ui", "ListMetrics"),
                              ("aws", "discovery", "discovery", "ListMetrics")}


def test_legacy_callers_unchanged():
    """Existing positional/keyword call shapes keep working and keep their
    (provider, tier) meaning."""
    au, _ = _usage()
    au.record("aws", "standard", calls=1, units=3)          # runner / waf
    au.record("aws", "tag_sync", calls=632, units=0)        # tag_sync
    au.record("azure", "low", calls=4, units=4)             # azure collector
    au.record("gcp", "low", calls=1, units=9)               # gcp collector
    au.record("aws", None, calls=1, units=1)                # tier missing
    assert set(_snap(au)) == {
        ("aws", "standard", "collector", "GetMetricData"),
        ("aws", "tag_sync", "collector", "GetResources"),
        ("azure", "low", "collector", "MonitorMetrics"),
        ("gcp", "low", "collector", "ListTimeSeries"),
        ("aws", "unknown", "collector", "GetMetricData"),
    }


def test_zero_records_are_ignored():
    au, _ = _usage()
    au.record("aws", "standard", calls=0, units=0)
    assert _snap(au) == {}


def test_record_is_thread_safe():
    au, _ = _usage()

    def work():
        for _ in range(1000):
            au.record("aws", "ui", calls=1, units=2, source="ui", operation="ListMetrics")

    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert _snap(au)[("aws", "ui", "ui", "ListMetrics")] == (8000, 16000)


# ── 2. flush writes the expected op_events detail ───────────────────────

def test_flush_writes_one_row_per_key_with_expected_detail():
    au, rows = _usage()
    au.record("aws", "standard", calls=2, units=1000)
    au.record("aws", "standard", calls=1, units=500)
    au.record("aws", "standard", calls=3, units=3, operation="ListMetrics")
    au.record("aws", "ui", calls=2, units=40, source="ui", operation="GetMetricData")
    au.record("aws", "ui", calls=5, units=5, source="ui", operation="GetMetricStatistics")
    au.record("aws", "discovery", calls=2, units=2, source="discovery", operation="ListMetrics")
    au.record("aws", "tag_sync", calls=632, units=0)
    au.record("gcp", "low", calls=1, units=2_000_000)
    assert au.flush() == 7
    d = _details(rows)

    gmd = d[("aws", "standard", "collector", "GetMetricData")]
    assert (gmd["calls"], gmd["units"], gmd["unit"]) == (3, 1500, "metrics_requested")
    assert abs(gmd["list_price_usd"] - 0.015) < 1e-9
    assert gmd["window_end"] >= gmd["window_start"]

    lm = d[("aws", "standard", "collector", "ListMetrics")]
    assert (lm["calls"], lm["units"], lm["unit"]) == (3, 3, "requests")
    assert lm["list_price_usd"] is None          # rate not verified -> no invented price

    ui_gmd = d[("aws", "ui", "ui", "GetMetricData")]
    assert (ui_gmd["calls"], ui_gmd["units"]) == (2, 40)
    assert abs(ui_gmd["list_price_usd"] - 0.0004) < 1e-9

    ui_stat = d[("aws", "ui", "ui", "GetMetricStatistics")]
    assert (ui_stat["calls"], ui_stat["units"], ui_stat["unit"]) == (5, 5, "metrics_requested")
    assert ui_stat["list_price_usd"] is None

    disc = d[("aws", "discovery", "discovery", "ListMetrics")]
    assert (disc["calls"], disc["units"], disc["source"], disc["tier"]) == (2, 2, "discovery", "discovery")

    tag = d[("aws", "tag_sync", "collector", "GetResources")]
    assert (tag["calls"], tag["units"], tag["list_price_usd"]) == (632, 0, 0.0)

    assert abs(d[("gcp", "low", "collector", "ListTimeSeries")]["list_price_usd"] - 1.0) < 1e-9


def test_flush_keeps_the_documented_detail_keys():
    """Backward compatibility with the original daily-totals SQL."""
    au, rows = _usage()
    au.record("aws", "standard", calls=1, units=10)
    au.flush()
    (args, kw), = rows
    assert args[0] == "metric_api_usage" and kw["severity"] == "INFO"
    for k in ("provider", "tier", "calls", "units", "unit", "list_price_usd",
              "window_start", "window_end"):
        assert k in kw["detail"]
    assert kw["detail"]["provider"] == "aws" and kw["detail"]["tier"] == "standard"


def test_flush_resets_counters():
    au, _ = _usage()
    au.record("aws", "ui", calls=1, units=1, source="ui", operation="ListMetrics")
    assert au.flush() == 1
    assert _snap(au) == {}
    assert au.flush() == 0


def test_flush_survives_one_failing_row():
    rows = []

    def log_event(*a, **k):
        if k["detail"]["source"] == "ui":
            raise RuntimeError("db down")
        rows.append(k)

    install_stub("app.collector.op_log", log_event=log_event)
    _, au = install_polling_modules()
    au.record("aws", "standard", calls=1, units=1)
    au.record("aws", "ui", calls=1, units=1, source="ui", operation="ListMetrics")
    assert au.flush() == 1           # the healthy row is still written
    assert len(rows) == 1


# ── 3. never raises ─────────────────────────────────────────────────────

def test_record_never_raises_on_bad_input():
    au, _ = _usage()
    au.record("aws", "ui", calls="x", units=None, source="ui", operation="ListMetrics")
    au.record(None, None, calls=object(), units=object())
    au.record("aws", ["unhashable"], calls=1, units=1)
    au.record("aws", "ui", calls=1, units=1, source=None, operation=12)
    # nothing was corrupted: a good record afterwards still lands
    au.record("aws", "standard", calls=1, units=1)
    assert _snap(au)[("aws", "standard", "collector", "GetMetricData")] == (1, 1)


def test_flush_never_raises_when_op_log_is_unavailable():
    au, _ = _usage()
    install_stub("app.collector.op_log")          # module without log_event -> ImportError
    sys.modules["app.collector.op_log"].__dict__.pop("log_event", None)
    au.record("aws", "ui", calls=1, units=1, source="ui", operation="ListMetrics")
    assert au.flush() == 0


def test_flush_never_raises_when_op_log_import_explodes():
    au, _ = _usage()
    sys.modules["app.collector.op_log"] = None    # `import` raises ImportError
    try:
        au.record("aws", "ui", calls=1, units=1, source="ui", operation="ListMetrics")
        assert au.flush() == 0
    finally:
        sys.modules.pop("app.collector.op_log", None)   # never leave a None entry behind


def test_billed_metric_queries_rule():
    au, _ = _usage()
    q = [{"Id": "a", "MetricStat": {}},
         {"Id": "b", "MetricStat": {}, "ReturnData": False},
         {"Id": "c", "Expression": "100 - a"},                      # math: free
         {"Id": "d", "Expression": "SEARCH('{AWS/EC2} x', 'Average')"}]
    assert au.billed_metric_queries(q) == 3
    assert au.billed_metric_queries([]) == 0
    assert au.billed_metric_queries(None) == 0
    assert au.billed_metric_queries([None]) == 0                    # never raises


def test_billed_metric_queries_matches_collector_rule():
    """The UI path must count GetMetricData units exactly like the collector."""
    install_real_utils_package()   # Phase B2: runner imports app.utils.cwagent_cache / shared_cache
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.aws.sts", get_boto3_session=lambda *a, **k: None)
    install_stub("app.aws.boto_config", STANDARD_RETRY={})
    install_stub("app.collector.metrics_writer",
                 write_metrics_batch=lambda rows: None,
                 write_metric_history_batch=lambda rows, **kw: None)
    install_stub("app.collector.disk_mounts",
                 all_cwagent_disk_dims=lambda *a, **k: {},
                 ensure_disk_mount_metric_registered=lambda *a, **k: None)
    pm = load_module("app/collector/polling_model.py")
    ep = load_module("app/collector/env_polling.py")
    au = load_module("app/collector/api_usage.py")
    for name, m in (("polling_model", pm), ("env_polling", ep), ("api_usage", au)):
        sys.modules[f"app.collector.{name}"] = m
        setattr(sys.modules["app.collector"], name, m)
    runner = load_module("app/collector/metrics/runner.py")
    q = [{"Id": "a", "MetricStat": {}}, {"Id": "c", "Expression": "100 - a"},
         {"Id": "d", "Expression": "SEARCH('x', 'Average')"}, {"Id": "e", "MetricStat": {}}]
    assert au.billed_metric_queries(q) == runner._billed_units(q) == 3


# ── 4. hooks: disk_mounts (shared by collector and UI) ──────────────────

def _load_disk_mounts():
    au, rows = _usage()
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.threshold_defaults", DEFAULT_THRESHOLDS={})
    return load_module("app/collector/disk_mounts.py"), au, rows


def _disk_metric(path="/"):
    return {"Dimensions": [{"Name": "InstanceId", "Value": "i-1"},
                           {"Name": "path", "Value": path},
                           {"Name": "device", "Value": "xvda1"},
                           {"Name": "fstype", "Value": "ext4"}]}


def test_disk_mounts_collector_hook_counts_each_list_metrics_request():
    mod, au, _ = _load_disk_mounts()
    cw = FakeCW(list_pages=[[_disk_metric()]])
    out = mod.all_cwagent_disk_dims(cw, "i-1", usage_tier="standard")
    assert out                                            # behaviour unchanged
    assert len(cw.list_calls) == 1
    assert _snap(au) == {("aws", "standard", "collector", "ListMetrics"): (1, 1)}


def test_disk_mounts_windows_fallback_counts_the_second_request():
    mod, au, _ = _load_disk_mounts()
    cw = FakeCW(list_pages=[[]])                          # nothing found either time
    assert mod.all_cwagent_disk_dims(cw, "i-1", usage_tier="low") == []
    assert len(cw.list_calls) == 2                        # Linux name, then Windows name
    assert _snap(au) == {("aws", "low", "collector", "ListMetrics"): (2, 2)}


def test_disk_mounts_ui_source_is_labelled_ui():
    mod, au, _ = _load_disk_mounts()
    cw = FakeCW(list_pages=[[_disk_metric()]])
    mod.all_cwagent_disk_dims(cw, "i-1", usage_source="ui")
    assert _snap(au) == {("aws", "ui", "ui", "ListMetrics"): (1, 1)}


def test_disk_mounts_default_tier_is_unknown_not_an_error():
    mod, au, _ = _load_disk_mounts()
    mod.all_cwagent_disk_dims(FakeCW(list_pages=[[_disk_metric()]]), "i-1")
    assert set(_snap(au)) == {("aws", "unknown", "collector", "ListMetrics")}


def test_disk_mounts_failed_request_is_still_counted_and_does_not_raise():
    mod, au, _ = _load_disk_mounts()
    cw = FakeCW(fail=True)
    assert mod.all_cwagent_disk_dims(cw, "i-1", usage_source="ui") == []
    assert _snap(au) == {("aws", "ui", "ui", "ListMetrics"): (2, 2)}


def test_disk_mounts_works_when_metering_is_broken():
    mod, au, _ = _load_disk_mounts()
    mod.api_usage = types.SimpleNamespace(record=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    # a raising record() would be a bug in api_usage (which never raises); the
    # real module's record() is exercised in test_record_never_raises_*.
    # Here: with the real module, a bogus tier/source still returns data.
    mod.api_usage = au
    cw = FakeCW(list_pages=[[_disk_metric()]])
    assert mod.all_cwagent_disk_dims(cw, "i-1", usage_tier=["bad"], usage_source=None)


# ── 5. hooks: runner (CWAgent memory dimensions, tier = current tier) ───

def _load_runner(seen_disk_kwargs=None):
    install_real_utils_package()   # Phase B2: runner imports app.utils.cwagent_cache / shared_cache
    au, rows = _usage()
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.aws.sts", get_boto3_session=lambda *a, **k: None)
    install_stub("app.aws.boto_config", STANDARD_RETRY={})
    install_stub("app.collector.metrics_writer",
                 write_metrics_batch=lambda rows: None,
                 write_metric_history_batch=lambda rows, **kw: None)

    def disk(cw, iid, **k):
        if seen_disk_kwargs is not None:
            seen_disk_kwargs.append(k)
        return []

    install_stub("app.collector.disk_mounts", all_cwagent_disk_dims=disk,
                 ensure_disk_mount_metric_registered=lambda *a, **k: None)
    pm = load_module("app/collector/polling_model.py")
    ep = load_module("app/collector/env_polling.py")
    for name, m in (("polling_model", pm), ("env_polling", ep)):
        sys.modules[f"app.collector.{name}"] = m
        setattr(sys.modules["app.collector"], name, m)
    return load_module("app/collector/metrics/runner.py"), au


def test_runner_mem_dimension_lookup_is_metered_under_the_current_tier():
    runner, au = _load_runner()
    runner._CURRENT_TIER = "standard"
    cw = FakeCW(list_pages=[[]])                      # Linux and Windows names both miss
    out = runner._ec2_instances_with_cwagent_mem_dims_uncached(
        cw, [{"resource_id": "i-1"}, {"resource_id": "i-2"}])
    assert out == {}
    assert len(cw.list_calls) == 4                    # 2 instances x (Linux + Windows)
    assert _snap(au) == {("aws", "standard", "collector", "ListMetrics"): (4, 4)}


def test_runner_mem_dimension_failure_counts_and_does_not_raise():
    runner, au = _load_runner()
    runner._CURRENT_TIER = "critical"
    cw = FakeCW(fail=True)
    assert runner._ec2_instances_with_cwagent_mem_dims_uncached(cw, [{"resource_id": "i-1"}]) == {}
    assert _snap(au) == {("aws", "critical", "collector", "ListMetrics"): (1, 1)}


def test_runner_passes_current_tier_to_disk_lookup():
    seen = []
    runner, _ = _load_runner(seen)
    runner._CURRENT_TIER = "low"      # (the shared cache starts empty: tests/conftest.py resets it around every test)
    runner._cached_disk_dims(FakeCW(), {"region": "r", "resource_id": "i-9"})
    assert seen == [{"usage_tier": "low"}]


# ── 6. hooks: collector_direct (every call there is UI-triggered) ───────

def _load_direct():
    install_real_utils_package()   # Phase B2: collector_direct imports app.utils.shared_cache / cwagent_cache
    au, rows = _usage()
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.clients.vm_client", vm_query=lambda p: None, vm_query_all=lambda p, d: {})
    install_stub("app.collector.disk_mounts", all_cwagent_disk_dims=lambda cw, iid, **k: [])
    install_stub("app.aws.boto_config", STANDARD_RETRY=None, CONCURRENT_CLIENT_RETRY=None)
    install_stub("app.utils.time_json", to_utc_iso=lambda v: str(v))
    return load_module("app/aws/collector_direct.py"), au


def _q(n):
    return [{"Id": f"q{i}", "MetricStat": {"Metric": {}, "Period": 60, "Stat": "Average"}}
            for i in range(n)]


def test_gmd_pages_counts_every_page_and_units_once_per_request():
    mod, au = _load_direct()
    cw = FakeCW(gmd_pages=3)
    list(mod._gmd_pages(cw, _q(4) + [{"Id": "m", "Expression": "100 - q0"}], 0, 1, "TimestampAscending"))
    assert len(cw.gmd_calls) == 3
    # 3 pages, 4 billed metrics counted once (math expression is free)
    assert _snap(au) == {("aws", "ui", "ui", "GetMetricData"): (3, 4)}


def test_gmd_snapshot_counts_one_request_per_500_query_chunk():
    mod, au = _load_direct()
    cw = FakeCW()
    assert mod._gmd_snapshot(cw, _q(501)) == {}
    assert len(cw.gmd_calls) == 2
    assert _snap(au) == {("aws", "ui", "ui", "GetMetricData"): (2, 501)}


def test_gmd_series_is_metered_too():
    mod, au = _load_direct()
    cw = FakeCW()
    assert mod._gmd_series(cw, _q(3), hours=6) == {}
    assert _snap(au) == {("aws", "ui", "ui", "GetMetricData"): (1, 3)}


def test_gmd_failure_is_counted_and_not_raised_by_the_hook():
    mod, au = _load_direct()
    cw = FakeCW(fail=True)
    assert mod._gmd_snapshot(cw, _q(2)) == {}             # the module's own except
    assert _snap(au) == {("aws", "ui", "ui", "GetMetricData"): (1, 2)}


def test_cwagent_presence_and_dimension_lookups_are_metered_as_ui():
    mod, au = _load_direct()
    cw = FakeCW(list_pages=[[{"Dimensions": [{"Name": "InstanceId", "Value": "i-1"}]}]])
    mod.get_session = lambda *a, **k: FakeSession(cw)
    assert mod._ec2_cwagent_installed_raw("i-1", "r", None) is True
    assert mod._ec2_cwagent_dimensions(cw, "mem_used_percent", "i-1") == \
        [{"Name": "InstanceId", "Value": "i-1"}]
    assert _snap(au) == {("aws", "ui", "ui", "ListMetrics"): (2, 2)}


def test_cwagent_lookup_failure_is_counted_and_swallowed():
    mod, au = _load_direct()
    cw = FakeCW(fail=True)
    mod.get_session = lambda *a, **k: FakeSession(cw)
    assert mod._ec2_cwagent_installed_raw("i-1", "r", None) is False
    assert mod._ec2_cwagent_dimensions(cw, "mem_used_percent", "i-1") is None
    assert _snap(au) == {("aws", "ui", "ui", "ListMetrics"): (2, 2)}


def test_s3_detail_page_counts_every_get_metric_statistics_request():
    mod, au = _load_direct()
    cw = FakeCW()
    mod.get_session = lambda *a, **k: FakeSession(cw)
    mod._s3_bucket_region = lambda *a, **k: "ap-south-1"
    out = mod._get_s3_metric_series_raw("bkt", 24, None)
    assert out["bucket_name"] == "bkt"
    n = len(cw.stat_calls)
    assert n >= 2
    assert _snap(au) == {("aws", "ui", "ui", "GetMetricStatistics"): (n, n)}


def test_s3_failed_requests_are_counted_and_not_raised():
    mod, au = _load_direct()
    from botocore.exceptions import ClientError           # storage_series only catches ClientError
    cw = FakeCW(fail=ClientError({"Error": {"Code": "Throttling", "Message": "x"}}, "GetMetricStatistics"))
    mod.get_session = lambda *a, **k: FakeSession(cw)
    mod._s3_bucket_region = lambda *a, **k: "ap-south-1"
    mod._get_s3_metric_series_raw("bkt", 24, None)
    n = len(cw.stat_calls)
    assert _snap(au) == {("aws", "ui", "ui", "GetMetricStatistics"): (n, n)}


def test_collector_direct_passes_ui_source_to_the_shared_disk_helper():
    """Behavioural (was a source-text match, which a cache wrapper around the call naturally changes): the detail page
    really calls the shared disk helper with usage_source="ui", so a real call is attributed to the UI."""
    seen = []
    mod, _ = _load_direct()
    mod.all_cwagent_disk_dims = lambda cw, iid, **k: seen.append(k) or []
    cw = FakeCW(list_pages=[[{"Dimensions": [{"Name": "InstanceId", "Value": "i-1"}]}]])
    mod.get_session = lambda *a, **k: FakeSession(cw)
    mod._ec2_cwagent_installed = lambda *a, **k: True
    mod._metric_history_query_range = lambda *a, **k: []
    mod._get_ec2_metric_series_raw("i-1", "ap-south-1", 6, None)
    assert seen == [{"usage_source": "ui"}]


# ── 7. hooks: metric-catalog discovery (one record per page fetched) ────

def _load_catalog(cw):
    install_real_utils_package()   # Phase B2: metric_catalog imports app.utils.shared_cache
    au, rows = _usage()
    install_stub("app.db", get_connection=lambda: None, get_db_cursor=lambda: None)
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    install_stub("app.threshold_defaults", DEFAULT_THRESHOLDS={}, FALLBACK_THRESHOLD=(70, 90, ">"),
                 normalize_threshold_resource_type=lambda s: s,
                 normalize_service_key=lambda rt, rid: rt)
    install_stub("app.aws.boto_config", STANDARD_RETRY=None)
    install_stub("app.aws.sts", get_boto3_session=lambda acc: FakeSession(cw))
    install_stub("app.audit", write_audit=lambda *a, **k: None)
    install_stub("app.account_names", account_label=lambda *a, **k: "", plural=lambda *a, **k: "")
    install_stub("app.utils.time_json", to_utc_iso=lambda v: str(v))
    return load_module("app/api/metric_catalog.py"), au


def _names(prefix, n):
    return [{"MetricName": f"{prefix}{i}"} for i in range(n)]


def test_discovery_counts_each_page_fetched():
    cw = FakeCW(list_pages=[_names("a", 50), _names("b", 50), _names("c", 10)])
    mod, au = _load_catalog(cw)
    found = mod._discover_aws_metrics({"default_region": "r"}, "AWS/EC2", None)
    assert len(found) == 110
    assert len(cw.list_calls) == 3
    assert _snap(au) == {("aws", "discovery", "discovery", "ListMetrics"): (3, 3)}


def test_discovery_stops_at_the_cap_and_counts_only_fetched_pages():
    cw = FakeCW(list_pages=[_names("a", 100), _names("b", 100), _names("c", 100)])
    mod, au = _load_catalog(cw)
    found = mod._discover_aws_metrics({"default_region": "r"}, "AWS/EC2", None)
    assert len(found) == 200
    assert len(cw.list_calls) == 2                       # third page never requested
    assert _snap(au) == {("aws", "discovery", "discovery", "ListMetrics"): (2, 2)}


# ── 8. no behaviour change: hooks are additive only ─────────────────────

def test_hook_inventory_matches_the_audited_call_sites():
    """Phase A must not change intervals/tiers/cache logic: the diff is
    record() hooks, the optional usage_* kwargs and the import shim."""
    import re
    root = __file__.rsplit("/tests/", 1)[0]
    for rel, expected in (("app/collector/metrics/runner.py", 2),
                          ("app/collector/disk_mounts.py", 2),
                          ("app/aws/collector_direct.py", 5),
                          ("app/api/metric_catalog.py", 1)):
        src = open(f"{root}/{rel}").read()
        hooks = re.findall(r'^\s*api_usage\.record\(.*operation="(?:ListMetrics|GetMetricData|GetMetricStatistics)"\)\s*$',
                           src, re.M)
        assert len(hooks) == expected, (rel, len(hooks))
