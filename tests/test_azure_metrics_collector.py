# tests/test_azure_metrics_collector.py
"""
Covers app/providers/azure/metrics_collector.py's write-path: every
datapoint should land in metric_history, the latest per (resource,
metric) should land in metrics, and the metric_catalog's own metric_name
string (not a reconstructed name) must be what's written -- confirmed
against what metrics_vm_sync.py's alert-evaluation join expects.
"""
import json
from datetime import datetime
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub


class _RoutingCursor:
    def __init__(self):
        self._next = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "FROM metric_catalog" in normalized:
            self._next = [{"namespace": "Microsoft.Compute/virtualMachines", "service": "vm",
                            "metric_name": "Percentage CPU"}]
        elif "FROM resources" in normalized:
            self._next = [
                {"id": 101, "resource_id": "/subscriptions/x/resourceGroups/y/vm1", "name": "vm1"},
            ]
        else:
            self._next = []

    def fetchall(self):
        return self._next

    def close(self):
        pass


class _RoutingConn:
    def cursor(self, dictionary=True):
        return _RoutingCursor()

    def close(self):
        pass


class _FakeDatapoint:
    def __init__(self, average, timestamp):
        self.average = average
        self.timestamp = timestamp


class _FakeTimeseries:
    def __init__(self, data):
        self.data = data


class _FakeMetric:
    def __init__(self, name, timeseries):
        self.name = name
        self.timeseries = timeseries


class _FakeQueryResult:
    def __init__(self, metrics):
        self.metrics = metrics


def _stub_common(query_resources_fn):
    install_stub("app.db", get_connection=lambda: _RoutingConn())
    install_stub("app.credentials", load_credential=lambda a: "fake-secret")
    written = {"metrics": [], "history": []}
    install_stub(
        "app.collector.metrics_writer",
        write_metrics_batch=lambda rows: written["metrics"].extend(rows),
        write_metric_history_batch=lambda rows: written["history"].extend(rows),
    )
    install_stub("azure.identity", ClientSecretCredential=lambda tenant_id=None, client_id=None, client_secret=None: object())

    class _Agg:
        AVERAGE = "Average"

    class _FakeClient:
        def query_resources(self, resource_ids, metric_namespace, metric_names, timespan, granularity, aggregations):
            return query_resources_fn(resource_ids)

    install_stub("azure.monitor.query", MetricsClient=lambda endpoint, cred: _FakeClient(),
                 MetricAggregationType=_Agg)
    return written


def _load_collector():
    return load_module("app/providers/azure/metrics_collector.py")


def test_writes_latest_to_metrics_and_full_series_to_history():
    def query_resources(resource_ids):
        results = []
        for _ in resource_ids:
            ts = _FakeTimeseries([
                _FakeDatapoint(40.0, datetime(2026, 9, 8, 10, 0, 0)),
                _FakeDatapoint(55.5, datetime(2026, 9, 8, 10, 1, 0)),  # latest
            ])
            results.append(_FakeQueryResult([_FakeMetric("Percentage CPU", [ts])]))
        return results

    written = _stub_common(query_resources)
    mod = _load_collector()

    account = {"id": 5, "tenant_id": "t", "client_id": "c", "client_secret_ref": "s",
               "subscription_id": "s", "default_region": "centralindia"}
    import unittest.mock as um
    with um.patch.object(mod, "_enabled_azure_metrics",
                          return_value={("Microsoft.Compute/virtualMachines", "vm"): {"Percentage CPU"}}):
        result = mod.collect_account_metrics(account)

    assert result["pushed"] == 1
    assert written["metrics"] == [(101, "Percentage CPU", 55.5)]
    assert (101, "Percentage CPU", 40.0, datetime(2026, 9, 8, 10, 0, 0)) in written["history"]
    assert (101, "Percentage CPU", 55.5, datetime(2026, 9, 8, 10, 1, 0)) in written["history"]
    assert result["errors"] == []


def test_no_datapoints_is_not_an_error():
    def query_resources(resource_ids):
        return [_FakeQueryResult([_FakeMetric("Percentage CPU", [_FakeTimeseries([])])]) for _ in resource_ids]

    written = _stub_common(query_resources)
    mod = _load_collector()

    account = {"id": 5, "tenant_id": "t", "client_id": "c", "client_secret_ref": "s",
               "subscription_id": "s", "default_region": "centralindia"}
    import unittest.mock as um
    with um.patch.object(mod, "_enabled_azure_metrics",
                          return_value={("Microsoft.Compute/virtualMachines", "vm"): {"Percentage CPU"}}):
        result = mod.collect_account_metrics(account)

    assert result["pushed"] == 0
    assert written["metrics"] == []
    assert written["history"] == []
    assert result["errors"] == []


def test_malicious_default_region_is_refused_not_used_as_ssrf_endpoint():
    """
    SECURITY REGRESSION TEST: default_region is only ever required to be
    non-empty at onboarding (app/api/admin/accounts.py), so this
    collector must not trust it's a real Azure region short-name.
    Before the fix, a default_region like "attacker.example.com" would
    have been placed directly into the MetricsClient endpoint
    (f"https://{region}.metrics.monitor.azure.com"), making this
    server issue a real, credentialed HTTPS request -- carrying this
    account's live Azure OAuth bearer token -- to an attacker-
    controlled host on every collection cycle. Asserts that a
    non-region-shaped value is rejected before MetricsClient is ever
    constructed, and that no metrics get silently dropped in a way
    that could mask the refusal.
    """
    def query_resources(resource_ids):
        raise AssertionError(
            "MetricsClient.query_resources must never be called when "
            "default_region fails validation -- reaching this point "
            "means the SSRF guard was bypassed."
        )

    written = _stub_common(query_resources)
    mod = _load_collector()

    for malicious_region in ["attacker.example.com", "evil.com/", "a b", "", "east.us"]:
        account = {"id": 5, "tenant_id": "t", "client_id": "c", "client_secret_ref": "s",
                   "subscription_id": "s", "default_region": malicious_region}
        import unittest.mock as um
        with um.patch.object(mod, "_enabled_azure_metrics",
                              return_value={("Microsoft.Compute/virtualMachines", "vm"): {"Percentage CPU"}}):
            result = mod.collect_account_metrics(account)

        assert result["pushed"] == 0, f"region {malicious_region!r} should push nothing"
        assert result["errors"], f"region {malicious_region!r} should report an error, not silently no-op"

    assert written["metrics"] == []
    assert written["history"] == []
