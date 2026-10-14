# tests/test_account_service_counts.py
"""
/api/live/accounts now carries, per account row, `service_counts` and
`service_labels` so the Overview card can draw one chip per service the
health ring counts (it used to draw a fixed EC2/EBS/S3/Lambda four).

Pins:
  * counts use the same normalize_service_key() grouping as the Services page
    and are limited to the account's active services (chips == ring);
  * a failing count query never takes the Overview down and never leaks the
    pooled connection;
  * the new fields ride the slow 60 s cache layer like active_services does.

ISOLATION: every module live_data.py imports is replaced through
monkeypatch.setitem(sys.modules, ...) with a throwaway stub, which pytest undoes
after each test. Nothing here imports a real `app` module or sets attributes on
one, so this file can run first, last or alone without changing what any other
test sees (conftest.install_stub overrides attributes on an already-imported real
module, which is how a stub can leak into the rest of the session).
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_module  # noqa: E402


class _AnyAttr(types.ModuleType):
    """A stub module where `from stub import anything` yields a no-op callable."""
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


def _mod(name, **attrs):
    m = _AnyAttr(name)
    m.__dict__.update(attrs)
    return m


class _FingerprintCache:
    def __init__(self, *a, **k):
        pass
    def get(self, fn):
        return fn()


@pytest.fixture
def mod(monkeypatch):
    alert_rules = _mod(
        "app.alert_rules",
        firing_where=lambda: "1=1",
        base_where=lambda: "1=1",
        service_ring=lambda active, roll: {
            "unit": "service", "total": len(active or ()), "critical": 0, "warning": 0,
            "healthy": len(active or ()), "critical_services": [], "warning_services": []},
    )
    stubs = {
        "app": _mod("app", alert_rules=alert_rules),
        "app.alert_rules": alert_rules,
        "app.auth.permissions": _mod("app.auth.permissions", require_permission=lambda code: (lambda: None)),
        "app.auth.authorization": _mod("app.auth.authorization", get_accessible_account_ids=lambda user: None),
        "app.aws.collector_direct": _mod("app.aws.collector_direct", get_account_summary=lambda *a, **k: {}),
        "app.db": _mod("app.db", get_connection=lambda: None),
        "app.alert_visibility": _mod("app.alert_visibility", hidden_metrics_sql=lambda: "''"),
        "app.threshold_defaults": _mod("app.threshold_defaults", normalize_service_key=lambda rt, rid="": rt),
        "app.utils.time_json": _mod("app.utils.time_json", to_utc_iso=lambda d: d.isoformat()),
        "app.metric_display": _mod("app.metric_display", RANGE_LADDER_HOURS=(1,)),
        "app.metric_meta": _mod("app.metric_meta"),
        "app.alert_cache": _mod("app.alert_cache", FingerprintCache=_FingerprintCache),
    }
    for name, stub in stubs.items():
        monkeypatch.setitem(sys.modules, name, stub)
    return load_module("app/api/live_data.py")


class _Cursor:
    """Answers the two queries _get_service_counts_by_account_unsafe makes, in order."""
    def __init__(self, resources, labels):
        self._results = [resources, labels]
    def execute(self, *a, **k):
        pass
    def fetchall(self):
        return self._results.pop(0)
    def close(self):
        pass


class _Conn:
    def __init__(self, resources=(), labels=()):
        self.closed = False
        self._resources, self._labels = list(resources), list(labels)
    def cursor(self, dictionary=False):
        return _Cursor(self._resources, self._labels)
    def close(self):
        self.closed = True


class _RaisingConn:
    def __init__(self):
        self.closed = False
    def cursor(self, dictionary=False):
        raise RuntimeError("simulated query failure")
    def close(self):
        self.closed = True


def _wire_account(mod, active, counts, labels, acc_id=7):
    mod._get_db_accounts = lambda: [
        {"id": acc_id, "account_name": "A", "account_id": "1", "default_region": "ap-south-1",
         "role_arn": "r", "external_id": None, "created_at": None, "last_synced_at": None}]
    mod.get_account_summary = lambda *a, **k: {}
    mod._get_active_services_by_account = lambda: active
    mod._get_service_counts_by_account = lambda: {"counts": counts, "labels": labels}
    mod._get_active_alert_counts_by_account = lambda: {}
    mod._get_ec2_instance_health_by_account = lambda: {}
    mod.get_accessible_account_ids = lambda user: None


def test_counts_are_grouped_per_account_by_normalized_service_key(mod):
    mod.normalize_service_key = lambda rt, rid: ("alb" if rt == "elb" and "/app/" in rid else rt)
    conn = _Conn(
        resources=[
            {"aws_account_id": 1, "resource_type": "ec2", "resource_id": "i-1"},
            {"aws_account_id": 1, "resource_type": "ec2", "resource_id": "i-2"},
            {"aws_account_id": 1, "resource_type": "elb", "resource_id": "arn:.../loadbalancer/app/x"},
            {"aws_account_id": 2, "resource_type": "s3", "resource_id": "b1"},
        ],
        labels=[{"service": "dynamodb", "display_service": "DynamoDB"},
                {"service": "ec2", "display_service": None}],
    )
    mod.get_connection = lambda: conn
    out = mod._get_service_counts_by_account()
    assert out["counts"] == {1: {"ec2": 2, "alb": 1}, 2: {"s3": 1}}
    assert out["labels"] == {"dynamodb": "DynamoDB"}        # a catalog row without a label is skipped
    assert conn.closed is True


def test_count_query_failure_is_swallowed_and_connection_closed(mod):
    conn = _RaisingConn()
    mod.get_connection = lambda: conn
    assert mod._get_service_counts_by_account() == {"counts": {}, "labels": {}}
    assert conn.closed is True


def test_account_rows_carry_counts_limited_to_active_services(mod):
    # eni is discovered but is not a service tile, so it must not appear on the card
    _wire_account(
        mod,
        active={7: {"ec2", "alb"}},
        counts={7: {"ec2": 15, "alb": 3, "eni": 40}},
        labels={"alb": "Application LB", "eni": "ENI", "s3": "S3"},
    )
    row = mod.live_accounts(current_user={"id": 1, "role": "admin"})[0]
    assert row["service_counts"] == {"ec2": 15, "alb": 3}
    assert row["service_labels"] == {"alb": "Application LB"}


def test_account_with_no_discovered_resources_gets_empty_maps_not_missing_keys(mod):
    _wire_account(mod, active={}, counts={}, labels={}, acc_id=9)
    row = mod.live_accounts(current_user={"id": 1, "role": "admin"})[0]
    assert row["service_counts"] == {}
    assert row["service_labels"] == {}


def test_service_fields_are_cached_with_layer_one_not_requeried_per_request(mod):
    _wire_account(mod, active={7: {"ec2"}}, counts={7: {"ec2": 2}}, labels={})
    calls = {"n": 0}

    def counts():
        calls["n"] += 1
        return {"counts": {7: {"ec2": 2}}, "labels": {}}

    mod._get_service_counts_by_account = counts
    admin = {"id": 1, "role": "admin"}
    mod.live_accounts(current_user=admin)
    mod.live_accounts(current_user=admin)
    mod.live_accounts(current_user=admin)
    assert calls["n"] == 1
