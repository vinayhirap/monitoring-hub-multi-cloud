# tests/test_collector_direct.py
"""
Covers app/aws/collector_direct.py's three local-metrics query helpers,
added across Phase 4a/4b/5/final-cleanup to replace VictoriaMetrics
reads: _metric_history_query_range (chart-detail pages),
_metric_snapshot_query_all (list-view pages), and
_account_metric_snapshot (Check Thresholds Now). All three share the
same resource_id-vs-name matching distinction this session confirmed
against app/collector/discovery/runner.py -- these tests exist so a
future change can't quietly swap that distinction and break Compute
(resource_id-keyed) or ELB/Lambda (name-keyed) resources.
"""
import sys
from datetime import datetime

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub


class _RoutingCursor:
    def __init__(self, resources, history):
        self.resources = resources  # {(resource_type, match_field, identifier): row_id or None}
        self.history = history      # {(row_id, metric_name): [rows]}
        self.metrics_snapshot = {}  # {(resource_type, metric_name): [rows]}  (Phase 4b, no account scoping)
        self.account_snapshot = {}  # {(account_id, resource_type, metric_name): [rows]} (Phase 5)
        self._next = None

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "FROM resources WHERE" in normalized and "resource_type = %s AND" in normalized and len(params) == 3:
            # audit(b11): the lookup is always account-scoped now.
            resource_type, identifier, account_id = params
            match_field = "resource_id" if "resource_id = %s" in normalized else "name"
            row_id = self.resources.get((resource_type, match_field, identifier, account_id))
            self._next = [{"id": row_id}] if row_id else []
        elif "FROM metric_history" in normalized:
            resource_db_id, metric_name = params[0], params[1]
            self._next = self.history.get((resource_db_id, metric_name), [])
        elif "aws_account_id = %s AND r.resource_type = %s AND m.metric_name = %s" in normalized:
            self._next = self.account_snapshot.get(tuple(params), [])
        elif "FROM metrics m JOIN resources r" in normalized:
            self._next = self.metrics_snapshot.get(tuple(params), [])
        else:
            self._next = []

    def fetchone(self):
        return self._next[0] if self._next else None

    def fetchall(self):
        return self._next

    def close(self):
        pass


class _RoutingConn:
    def __init__(self, resources=None, history=None):
        self.cursor_obj = _RoutingCursor(resources or {}, history or {})

    def cursor(self, dictionary=True):
        return self.cursor_obj

    def close(self):
        pass


def _stub_and_load(conn):
    install_stub("app.db", get_connection=lambda: conn)
    install_stub("app.clients.vm_client", vm_query=lambda p: None, vm_query_all=lambda p, d: {})
    # collector_direct.py imports all_cwagent_disk_dims from this submodule
    # at module level -- not previously stubbed here, so every test in this
    # file raised ModuleNotFoundError via the isolated loader's fake "app"
    # package tree (real submodule imports don't resolve through it unless
    # explicitly stubbed). None of these tests exercise the CWAgent-disk
    # code path, so a stub that returns no mounts is sufficient.
    install_stub("app.collector.disk_mounts", all_cwagent_disk_dims=lambda cw, instance_id: [])
    # Same class of gap, introduced by the roadmap-phases patch adding
    # `from app.aws.boto_config import STANDARD_RETRY` to
    # collector_direct.py without updating this file's stubs to match --
    # every test here started raising "No module named 'app.aws'; 'app'
    # is not a package" the moment that import landed, since the fake
    # `app` module in sys.modules has no __path__ for a real submodule
    # to resolve against. None of these tests touch actual boto3 client
    # construction, so the value itself doesn't matter -- it only needs
    # to exist so the import resolves. CONCURRENT_CLIENT_RETRY was added
    # alongside it (S3 connection-pool fix) for the same reason.
    install_stub("app.aws.boto_config", STANDARD_RETRY=None, CONCURRENT_CLIENT_RETRY=None)
    return load_module("app/aws/collector_direct.py")


# ── _metric_history_query_range (Phase 4a: chart-detail pages) ──────────

def test_chart_range_resolves_ec2_by_resource_id():
    conn = _RoutingConn(
        resources={("ec2", "resource_id", "i-abc", 7): 501},
        history={(501, "cpuutilization"): [
            {"metric_value": 42.5, "metric_timestamp": datetime(2026, 9, 8, 10, 0)},
            {"metric_value": 55.0, "metric_timestamp": datetime(2026, 9, 8, 10, 5)},
        ]},
    )
    mod = _stub_and_load(conn)
    result = mod._metric_history_query_range("ec2", "i-abc", "cpuutilization",
                                              datetime(2026, 9, 8, 9), datetime(2026, 9, 8, 11),
                                              account_id=7)
    assert result == [
        {"t": "2026-09-08T10:00:00", "v": 42.5},
        {"t": "2026-09-08T10:05:00", "v": 55.0},
    ]


def test_chart_range_resolves_elb_by_name_not_resource_id():
    conn = _RoutingConn(
        resources={("elb", "name", "my-app-lb", 7): 502},
        history={(502, "requestcount"): [
            {"metric_value": 1200.0, "metric_timestamp": datetime(2026, 9, 8, 10, 0)},
        ]},
    )
    mod = _stub_and_load(conn)
    result = mod._metric_history_query_range("elb", "my-app-lb", "requestcount",
                                              datetime(2026, 9, 8, 9), datetime(2026, 9, 8, 11),
                                              match_field="name", account_id=7)
    assert result == [{"t": "2026-09-08T10:00:00", "v": 1200.0}]


def test_chart_range_no_match_returns_empty_not_error():
    conn = _RoutingConn(resources={}, history={})
    mod = _stub_and_load(conn)
    result = mod._metric_history_query_range("ec2", "i-does-not-exist", "cpuutilization",
                                              datetime(2026, 9, 8, 9), datetime(2026, 9, 8, 11),
                                              account_id=7)
    assert result == []


def test_chart_range_matching_resource_but_uncollected_metric_returns_empty():
    """EBS burst_balance case: resource exists, metric was never collected."""
    conn = _RoutingConn(resources={("ebs", "resource_id", "vol-1", 7): 601}, history={})
    mod = _stub_and_load(conn)
    result = mod._metric_history_query_range("ebs", "vol-1", "volumeburstbalance",
                                              datetime(2026, 9, 8, 9), datetime(2026, 9, 8, 11),
                                              account_id=7)
    assert result == []


# ── _metric_snapshot_query_all (Phase 4b: list-view pages) ──────────────

def test_list_view_snapshot_keys_by_resource_id():
    conn = _RoutingConn()
    conn.cursor_obj.metrics_snapshot[("ec2", "cpuutilization", 7)] = [
        {"resource_id": "i-aaa", "metric_value": 33.3},
        {"resource_id": "i-bbb", "metric_value": 71.0},
    ]
    mod = _stub_and_load(conn)
    result = mod._metric_snapshot_query_all("ec2", "cpuutilization", account_id=7)
    assert result == {"i-aaa": 33.3, "i-bbb": 71.0}


def test_list_view_snapshot_empty_on_no_data():
    conn = _RoutingConn()
    mod = _stub_and_load(conn)
    assert mod._metric_snapshot_query_all("ebs", "volumeburstbalance", account_id=7) == {}


# ── _account_metric_snapshot (Phase 5: Check Thresholds Now) ────────────

def test_account_snapshot_is_scoped_and_keyed_correctly():
    conn = _RoutingConn()
    conn.cursor_obj.account_snapshot[(7, "ec2", "cpuutilization")] = [
        {"key_val": "i-abc", "metric_value": 91.0},
    ]
    conn.cursor_obj.account_snapshot[(7, "elb", "requestcount")] = [
        {"key_val": "my-lb", "metric_value": 500.0},
    ]
    mod = _stub_and_load(conn)

    assert mod._account_metric_snapshot(7, "ec2", "cpuutilization", "resource_id") == {"i-abc": 91.0}
    assert mod._account_metric_snapshot(7, "elb", "requestcount", "name") == {"my-lb": 500.0}
    # A different account_id must NOT see account 7's data.
    assert mod._account_metric_snapshot(99, "ec2", "cpuutilization", "resource_id") == {}


def test_status_check_failed_is_in_local_metric_stub():
    """
    Regression test for the bug found after Phase 5 shipped: this metric
    must be resolvable locally (describe_polling.py writes it), not left
    to silently fall through to a real billed CloudWatch call.
    """
    conn = _RoutingConn()
    mod = _stub_and_load(conn)
    import inspect
    src = inspect.getsource(mod.check_and_write_alerts)
    stub_block = src.split("LOCAL_METRIC_STUB = {")[1].split("\n    }")[0]
    assert '"StatusCheckFailed"' in stub_block or "'StatusCheckFailed'" in stub_block


def test_burst_balance_and_alb_4xx_absent_from_local_stub():
    """
    These two must stay OUT of LOCAL_METRIC_STUB -- Phase 1 never
    collects them, so their absence is what makes them correctly fall
    through to a real GMD/boto3 check instead of a permanent silent miss.
    """
    conn = _RoutingConn()
    mod = _stub_and_load(conn)
    import inspect
    src = inspect.getsource(mod.check_and_write_alerts)
    stub_block = src.split("LOCAL_METRIC_STUB = {")[1].split("\n    }")[0]
    assert "BurstBalance" not in stub_block
    assert "HTTPCode_Target_4XX_Count" not in stub_block


def test_vm_query_is_not_imported_anymore():
    """vm_client is fully retired from this file (final-cleanup script)."""
    conn = _RoutingConn()
    mod = _stub_and_load(conn)
    assert not hasattr(mod, "vm_query")


# ── _s3_raw connection-pool fix ──────────────────────────────────────
# Regression test for the "Connection pool is full, discarding
# connection: s3.*.amazonaws.com. Connection pool size: 10" warnings
# seen in production. Root cause: _s3_raw() shares ONE boto3 S3 client
# across up to 20 ThreadPoolExecutor workers, but the client was built
# with no Config at all -- botocore's default max_pool_connections is
# 10, so more than 10 concurrent workers silently drop the excess
# connections instead of reusing them. Source-inspected (like the
# LOCAL_METRIC_STUB tests above) rather than exercised end-to-end,
# since actually driving 20 concurrent workers through a real/mocked
# boto3 S3 client is a lot of test weight for what is fundamentally a
# "this one Config kwarg is present" check.

def test_s3_raw_client_uses_the_concurrent_pool_config():
    conn = _RoutingConn()
    mod = _stub_and_load(conn)
    import inspect
    src = inspect.getsource(mod._s3_raw)
    assert 'client(\n                      "s3", config=CONCURRENT_CLIENT_RETRY)' in src \
        or 'config=CONCURRENT_CLIENT_RETRY' in src, (
        "_s3_raw's shared S3 client must pass config=CONCURRENT_CLIENT_RETRY "
        "-- without it, botocore's default max_pool_connections=10 is too "
        "small for this function's 20-worker ThreadPoolExecutor and "
        "connections get silently dropped under load"
    )


def test_concurrent_client_retry_config_has_adequate_pool_and_adaptive_retry():
    """
    Pins the actual values in app/aws/boto_config.py directly, loaded
    in isolation via load_module() rather than a plain `from
    app.aws.boto_config import ...` -- this file's other tests stub
    "app.aws.boto_config" in sys.modules (see _stub_and_load above),
    and a plain import here would be vulnerable to picking up that
    stub's STANDARD_RETRY=None/CONCURRENT_CLIENT_RETRY=None under
    full-suite test ordering instead of the real values. A future edit
    could weaken the pool size or drop the retry mode without any test
    here noticing otherwise.
    """
    from tests.conftest import load_module
    mod = load_module("app/aws/boto_config.py")
    assert mod.CONCURRENT_CLIENT_RETRY.max_pool_connections > 10
    assert mod.CONCURRENT_CLIENT_RETRY.retries["mode"] == "adaptive"
    # Must still be a superset of the app-wide retry policy, not a
    # separate one-off that could drift from it.
    assert mod.CONCURRENT_CLIENT_RETRY.retries == mod.STANDARD_RETRY.retries
