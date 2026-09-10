# tests/test_gcp_metrics_collector.py
"""
Covers app/providers/gcp/metrics_collector.py's resource resolution --
the exact logic that turned up a real bug this session (compute_instance
resource matching never worked because Cloud Monitoring's gce_instance
type only exposes a numeric instance ID, not the name resources.resource_id
is built from). These tests exist so that bug, and the other 3 core
services' resolvers, can't silently regress.
"""
import json
import sys
from datetime import datetime

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub


class _FakeCredsCls:
    def from_service_account_info(self, info, scopes=None):
        return object()


class _RoutingCursor:
    def __init__(self, resources_by_service):
        self.resources_by_service = resources_by_service
        self._next = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "FROM resources" in normalized:
            account_id, service = params
            self._next = self.resources_by_service.get(service, [])
        else:
            self._next = []

    def fetchall(self):
        return self._next

    def close(self):
        pass


class _RoutingConn:
    def __init__(self, resources_by_service):
        self.resources_by_service = resources_by_service

    def cursor(self, dictionary=True):
        return _RoutingCursor(self.resources_by_service)

    def close(self):
        pass


class _FakeVal:
    def __init__(self, value):
        self.double_value = value

        class Pb:
            def WhichOneof(_self, name):
                return "double_value"
        self._pb = Pb()


class _FakeInterval:
    def __init__(self, end_time):
        self.end_time = end_time


class _FakePoint:
    def __init__(self, value, ts):
        self.value = _FakeVal(value)
        self.interval = _FakeInterval(ts)


def _stub_common():
    install_stub("google.cloud.monitoring_v3",
                 MetricsQueryResult=object, MetricAggregationType=type("A", (), {"AVERAGE": "Average"}))
    install_stub("google.oauth2.service_account", Credentials=_FakeCredsCls())
    install_stub("app.collector.metrics_writer",
                 write_metrics_batch=lambda rows: None,
                 write_metric_history_batch=lambda rows: None)
    # metrics_collector.py imports EXTENDED_RESOLVERS from this submodule at
    # module level (added by a later change than this test file -- the
    # isolated loader's fake "app" package tree doesn't resolve real
    # submodule imports, so this was raising ImportError on every test in
    # this file until stubbed). Empty dict keeps these tests scoped to the
    # 4 core resolvers only, matching test_extended_tier_service_has_no_resolver's
    # existing assertion of exactly {"compute_instance","gcs_bucket",
    # "cloudsql_instance","cloud_run_service"}.
    install_stub("app.providers.gcp.metrics_extended", EXTENDED_RESOLVERS={})


def _load_collector():
    return load_module("app/providers/gcp/metrics_collector.py")


def test_compute_instance_resolves_by_numeric_id_not_name():
    """
    The actual bug this session found: Cloud Monitoring's gce_instance
    labels only ever include a NUMERIC instance_id -- discovery.py now
    persists that into resources.tags._gcp_numeric_id so it can be
    matched back. This confirms that round-trip actually works.
    """
    resources = {
        "compute_instance": [
            {"id": 201, "resource_id": "projects/p/zones/z/instances/web-1",
             "tags": json.dumps({"_gcp_numeric_id": "5106847938295940291"})},
        ],
    }
    install_stub("app.db", get_connection=lambda: _RoutingConn(resources))
    install_stub("app.credentials", load_credential=lambda a: json.dumps({"type": "service_account"}))
    _stub_common()

    mod = _load_collector()

    numeric_id_map = {"5106847938295940291": 201}
    resource_id_map = {r["resource_id"]: r["id"] for r in resources["compute_instance"]}

    result = mod._resolve_compute_instance(
        "p", {"project_id": "p", "instance_id": "5106847938295940291", "zone": "z"},
        resource_id_map, numeric_id_map,
    )
    assert result == 201

    # An instance whose numeric ID was never discovered (stale/pre-fix
    # resources.tags row) must NOT silently match the wrong thing.
    result_missing = mod._resolve_compute_instance(
        "p", {"project_id": "p", "instance_id": "999999999", "zone": "z"},
        resource_id_map, numeric_id_map,
    )
    assert result_missing is None


def test_gcs_bucket_cloudsql_cloud_run_resolve_by_name():
    install_stub("app.db", get_connection=lambda: _RoutingConn({}))
    install_stub("app.credentials", load_credential=lambda a: json.dumps({"type": "service_account"}))
    _stub_common()

    mod = _load_collector()

    bucket_map = {"projects/p/buckets/my-bucket": 301}
    assert mod._resolve_gcs_bucket("p", {"bucket_name": "my-bucket"}, bucket_map, {}) == 301
    assert mod._resolve_gcs_bucket("p", {}, bucket_map, {}) is None

    sql_map = {"projects/p/instances/prod-db": 302}
    assert mod._resolve_cloudsql_instance("p", {"database_id": "prod-db"}, sql_map, {}) == 302

    run_map = {"projects/p/locations/us-central1/services/api-svc": 303}
    assert mod._resolve_cloud_run_service(
        "p", {"location": "us-central1", "service_name": "api-svc"}, run_map, {}
    ) == 303
    assert mod._resolve_cloud_run_service("p", {"location": "us-central1"}, run_map, {}) is None


def test_point_value_extracts_scalar():
    install_stub("app.db", get_connection=lambda: _RoutingConn({}))
    install_stub("app.credentials", load_credential=lambda a: "{}")
    _stub_common()
    mod = _load_collector()

    assert mod._point_value(_FakePoint(72.5, datetime(2026, 1, 1))) == 72.5


class _AccountLevelCursor:
    """
    Routes BOTH queries collect_account_metrics() issues against a real
    cursor: _enabled_gcp_metrics's "FROM metric_catalog" (returns the
    fixed `enabled` rows) and _build_resource_maps's per-service
    "FROM resources" (returns resources_by_service.get(service, [])).
    Needed (over the simpler _RoutingCursor above) because this test
    drives the whole collect_account_metrics() function, not just a
    bare _resolve_* call.
    """
    def __init__(self, enabled_rows, resources_by_service):
        self.enabled_rows = enabled_rows
        self.resources_by_service = resources_by_service
        self._next = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "FROM metric_catalog" in normalized:
            self._next = self.enabled_rows
        elif "FROM resources" in normalized:
            service = params[1]
            self._next = self.resources_by_service.get(service, [])
        else:
            self._next = []

    def fetchall(self):
        return self._next

    def close(self):
        pass


class _AccountLevelConn:
    def __init__(self, enabled_rows, resources_by_service):
        self.enabled_rows = enabled_rows
        self.resources_by_service = resources_by_service

    def cursor(self, dictionary=True):
        return _AccountLevelCursor(self.enabled_rows, self.resources_by_service)

    def close(self):
        pass


class _FakeMetricServiceClient:
    """Records every filter it's called with; never returns real series
    (empty iterable is enough -- these tests only assert on CALLS made,
    not on downstream row-writing)."""
    calls = []

    def __init__(self, credentials=None):
        pass

    def list_time_series(self, request):
        _FakeMetricServiceClient.calls.append(request["filter"])
        return []


def _stub_monitoring_v3_for_client_test():
    class _View:
        FULL = "FULL"

    class _ListTimeSeriesRequest:
        TimeSeriesView = _View

    install_stub(
        "google.cloud.monitoring_v3",
        MetricServiceClient=_FakeMetricServiceClient,
        TimeInterval=lambda d: d,
        ListTimeSeriesRequest=_ListTimeSeriesRequest,
    )
    install_stub("google.oauth2.service_account", Credentials=_FakeCredsCls())
    install_stub("app.collector.metrics_writer",
                 write_metrics_batch=lambda rows: None,
                 write_metric_history_batch=lambda rows: None)
    install_stub("app.providers.gcp.metrics_extended", EXTENDED_RESOLVERS={})


def test_zero_resource_service_skips_list_time_series_call():
    """
    The fix this test guards: collect_account_metrics() must NOT call
    the paid list_time_series() for a (metric_type, service) pair when
    this account has zero resources of that service -- the call can
    only ever return series that fail to match anyone afterward. Mirrors
    Azure's existing "if not resources: continue" and AWS's
    grouped-from-DB-rows dispatch (see this file's module docstring).
    Two enabled metrics: compute_instance (0 resources -- must be
    skipped, zero calls) and gcs_bucket (1 resource -- must fire).
    """
    _FakeMetricServiceClient.calls = []
    enabled_rows = [
        {"namespace": "compute.googleapis.com", "service": "compute_instance", "metric_name": "cpu/utilization"},
        {"namespace": "storage.googleapis.com", "service": "gcs_bucket", "metric_name": "storage/object_count"},
    ]
    resources_by_service = {
        "gcs_bucket": [{"id": 301, "resource_id": "projects/p/buckets/my-bucket", "tags": None}],
        # compute_instance: deliberately absent -> zero resources
    }
    install_stub("app.db", get_connection=lambda: _AccountLevelConn(enabled_rows, resources_by_service))
    install_stub("app.credentials", load_credential=lambda a: json.dumps({"type": "service_account"}))
    _stub_monitoring_v3_for_client_test()

    mod = _load_collector()
    result = mod.collect_account_metrics({"id": 1, "project_id": "p"})

    assert _FakeMetricServiceClient.calls == ["metric.type = \"storage.googleapis.com/storage/object_count\""]
    assert result["metric_types_queried"] == 2  # both counted, only 1 actually called
    assert not result["errors"]


def test_extended_tier_service_has_no_resolver():
    """
    Services this app's discovery.py doesn't collect (GKE, Pub/Sub, etc.)
    must have no resolver -- collect_account_metrics() is expected to
    skip and count these, never guess a resolver for them.
    """
    install_stub("app.db", get_connection=lambda: _RoutingConn({}))
    install_stub("app.credentials", load_credential=lambda a: "{}")
    _stub_common()
    mod = _load_collector()

    assert "gke_cluster" not in mod._RESOLVERS
    assert set(mod._RESOLVERS.keys()) == {"compute_instance", "gcs_bucket", "cloudsql_instance", "cloud_run_service"}
