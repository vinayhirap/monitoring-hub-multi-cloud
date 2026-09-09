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
