# tests/test_fleet_health_summary.py
"""
Coverage for GET /api/incidents/fleet-summary (2026-09-14) -- the
one-call, RBAC-scoped aggregate powering Overview.jsx's fleet health
tiles. Also guards the FastAPI route-ordering requirement this
endpoint depends on: it must be registered BEFORE @router.get(
"/{account_id}") or a request to /fleet-summary would incorrectly
match the account_id path param first and 422 on the int-cast, never
reaching this handler.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app          # noqa: F401 -- real, empty __init__.py, safe (see
import app.auth     # noqa: F401 -- test_resource_health_json_parsing.py's
                     # own comment for why this must happen before
                     # install_stub() touches any app.auth.* leaf module.

from tests.conftest import load_module, install_stub, FakeCursor, FakeConn


def _install_common_stubs():
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))


def test_route_registered_before_account_id_catchall():
    """Guards the actual routing bug class: /fleet-summary must appear
    in the router BEFORE /{account_id}, or FastAPI's first-match
    routing would send a real /fleet-summary request into
    list_incidents(account_id="fleet-summary") and 422 on the int cast
    instead of ever reaching this endpoint."""
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    _install_common_stubs()
    mod = load_module("app/api/incidents.py")

    paths = [route.path for route in mod.router.routes]
    fleet_index = paths.index("/api/incidents/fleet-summary")
    account_index = paths.index("/api/incidents/{account_id}")
    assert fleet_index < account_index


def test_fleet_summary_scoped_to_zero_accounts_returns_empty_without_querying():
    """A user with access to no accounts at all must get an empty
    summary WITHOUT any DB query being attempted."""
    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            raise AssertionError("should not query the DB when accessible == empty set")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: set())
    _install_common_stubs()
    mod = load_module("app/api/incidents.py")

    result = mod.fleet_health_summary(current_user={"username": "restricted"})
    assert result == {
        "unhealthy_resource_count": 0, "critical_resource_count": 0,
        "capacity_risk_count": 0, "likely_flapping_count": 0, "worst_resources": [],
    }


def test_fleet_summary_aggregates_counts_and_parses_score_reason():
    counts_row = {"total": 4, "critical": 2}
    worst_rows = [
        {"resource_id": "i-a", "aws_account_id": 7, "health_score": 20,
         "score_reason": '{"critical_alerts": 2, "warning_alerts": 0}'},
        {"resource_id": "i-b", "aws_account_id": 7, "health_score": 44,
         "score_reason": '{"critical_alerts": 1, "warning_alerts": 1}'},
    ]

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT COUNT(*) AS total"):
                self._pending = [counts_row]
            elif normalized.startswith("SELECT resource_id, aws_account_id, health_score, score_reason"):
                self._pending = worst_rows
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    install_stub("app.collector.trend", compute_capacity_forecasts=lambda **kwargs: [
        {"resource_id": "i-c", "metric_name": "DiskSpaceUtilization", "days_to_exhaustion": 5.0},
    ])
    install_stub("app.collector.threshold_tuning", count_likely_flapping_alerts=lambda **kwargs: 0)
    _install_common_stubs()
    mod = load_module("app/api/incidents.py")

    result = mod.fleet_health_summary(current_user={"username": "admin"})

    assert result["unhealthy_resource_count"] == 4
    assert result["critical_resource_count"] == 2
    assert result["capacity_risk_count"] == 1
    assert result["likely_flapping_count"] == 0
    assert len(result["worst_resources"]) == 2
    # score_reason must come back as a real dict, not the raw JSON
    # string the DB driver returns (same bug class as
    # test_resource_health_json_parsing.py -- must not regress here too).
    assert isinstance(result["worst_resources"][0]["score_reason"], dict)
    assert result["worst_resources"][0]["score_reason"]["critical_alerts"] == 2


def test_fleet_summary_scopes_forecast_query_to_accessible_accounts():
    """A restricted viewer's capacity-risk count must be computed
    against ONLY their accessible accounts -- verifies
    compute_capacity_forecasts() is actually called with
    aws_account_ids, not left unscoped (which would leak another
    account's capacity-risk count into this user's aggregate)."""
    captured_kwargs = {}

    def _fake_forecasts(**kwargs):
        captured_kwargs.update(kwargs)
        return []

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT COUNT(*) AS total"):
                self._pending = [{"total": 0, "critical": 0}]
            else:
                self._pending = []

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: {7, 9})
    install_stub("app.collector.trend", compute_capacity_forecasts=_fake_forecasts)
    install_stub("app.collector.threshold_tuning", count_likely_flapping_alerts=lambda **kwargs: 0)
    _install_common_stubs()
    mod = load_module("app/api/incidents.py")

    mod.fleet_health_summary(current_user={"username": "restricted"})

    assert captured_kwargs.get("aws_account_ids") == {7, 9}
