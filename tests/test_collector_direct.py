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
        if "FROM resources WHERE" in normalized and "resource_type = %s AND" in normalized and len(params) == 2:
            resource_type, identifier = params
            match_field = "resource_id" if "resource_id = %s" in normalized else "name"
            row_id = self.resources.get((resource_type, match_field, identifier))
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
    return load_module("app/aws/collector_direct.py")


# ── _metric_history_query_range (Phase 4a: chart-detail pages) ──────────

def test_chart_range_resolves_ec2_by_resource_id():
    conn = _RoutingConn(
        resources={("ec2", "resource_id", "i-abc"): 501},
        history={(501, "cpuutilization"): [
            {"metric_value": 42.5, "metric_timestamp": datetime(2026, 9, 8, 10, 0)},
            {"metric_value": 55.0, "metric_timestamp": datetime(2026, 9, 8, 10, 5)},
        ]},
    )
    mod = _stub_and_load(conn)
    result = mod._metric_history_query_range("ec2", "i-abc", "cpuutilization",
                                              datetime(2026, 9, 8, 9), datetime(2026, 9, 8, 11))
    assert result == [
        {"t": "2026-09-08T10:00:00", "v": 42.5},
        {"t": "2026-09-08T10:05:00", "v": 55.0},
    ]


def test_chart_range_resolves_elb_by_name_not_resource_id():
    conn = _RoutingConn(
        resources={("elb", "name", "my-app-lb"): 502},
        history={(502, "requestcount"): [
            {"metric_value": 1200.0, "metric_timestamp": datetime(2026, 9, 8, 10, 0)},
        ]},
    )
    mod = _stub_and_load(conn)
    result = mod._metric_history_query_range("elb", "my-app-lb", "requestcount",
                                              datetime(2026, 9, 8, 9), datetime(2026, 9, 8, 11),
                                              match_field="name")
    assert result == [{"t": "2026-09-08T10:00:00", "v": 1200.0}]


def test_chart_range_no_match_returns_empty_not_error():
    conn = _RoutingConn(resources={}, history={})
    mod = _stub_and_load(conn)
    result = mod._metric_history_query_range("ec2", "i-does-not-exist", "cpuutilization",
                                              datetime(2026, 9, 8, 9), datetime(2026, 9, 8, 11))
    assert result == []


def test_chart_range_matching_resource_but_uncollected_metric_returns_empty():
    """EBS burst_balance case: resource exists, metric was never collected."""
    conn = _RoutingConn(resources={("ebs", "resource_id", "vol-1"): 601}, history={})
    mod = _stub_and_load(conn)
    result = mod._metric_history_query_range("ebs", "vol-1", "volumeburstbalance",
                                              datetime(2026, 9, 8, 9), datetime(2026, 9, 8, 11))
    assert result == []


# ── _metric_snapshot_query_all (Phase 4b: list-view pages) ──────────────

def test_list_view_snapshot_keys_by_resource_id():
    conn = _RoutingConn()
    conn.cursor_obj.metrics_snapshot[("ec2", "cpuutilization")] = [
        {"resource_id": "i-aaa", "metric_value": 33.3},
        {"resource_id": "i-bbb", "metric_value": 71.0},
    ]
    mod = _stub_and_load(conn)
    result = mod._metric_snapshot_query_all("ec2", "cpuutilization")
    assert result == {"i-aaa": 33.3, "i-bbb": 71.0}


def test_list_view_snapshot_empty_on_no_data():
    conn = _RoutingConn()
    mod = _stub_and_load(conn)
    assert mod._metric_snapshot_query_all("ebs", "volumeburstbalance") == {}


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
