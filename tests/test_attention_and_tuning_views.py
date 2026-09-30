# tests/test_attention_and_tuning_views.py
"""2026-09-30: the Overview 'Need Attention' and 'Flapping (Self-Tuning)' tiles become
clickable. The Alerts page gets two matching tabs, each row is tagged, and the numbers
on the tile / tab badge / rows all come from ONE definition."""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app.alert_rules  # noqa: F401,E402
import app.aws.metric_catalog_data  # noqa: F401,E402  (state_sql() needs the REAL package; the helper stubs app.aws.federation)
import app.collector.polling_model  # noqa: F401,E402
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn  # noqa: E402
from tests.test_audit_b06_alerts_incidents import _alerts_mod  # noqa: E402


def _alerts(flap_ids=()):
    mod, log, conns = _alerts_mod(None, [])
    mod.get_flapping_alert_ids = lambda: set(flap_ids)
    return mod


def test_attention_tab_is_firing_alerts_on_critical_health_resources():
    mod = _alerts()
    w = mod._tab_where("attention")
    assert "= 'firing'" in w and "resource_health" in w and "health_score < 70" in w


def test_tuning_tab_lists_exactly_the_flapping_ids():
    mod = _alerts(flap_ids=[5, 3, 9])
    w = mod._tab_where("tuning")
    assert "a.id IN (3,5,9)" in w and "= 'firing'" in w


def test_tuning_tab_with_no_flapping_alerts_is_empty_not_everything():
    assert "1 = 0" in _alerts(flap_ids=[])._tab_where("tuning")


def test_new_tabs_are_accepted_and_old_tabs_unchanged():
    mod = _alerts()
    assert "attention" in mod._TABS and "tuning" in mod._TABS
    assert mod._tab_where("all") == "" and "= 'firing'" in mod._tab_where("active")


def test_special_tabs_do_not_compute_flapping_ids_for_other_tabs():
    # the heavy flapping query must only run when the tuning tab / counts need it
    calls = []
    mod = _alerts()
    mod.get_flapping_alert_ids = lambda: calls.append(1) or set()
    for t in ("all", "active", "stale", "critical", "acknowledged", "resolved", "suppressed", "attention"):
        mod._tab_where(t)
    assert calls == []


def test_counts_expose_attention_and_tuning_and_respect_account_scope():
    mod = _alerts()
    mod.get_accessible_account_ids = lambda user: {7}
    rows = [{"account_id": 7, "account_name": "A", "all_count": 5, "active_count": 4, "stale_count": 0,
             "critical_count": 1, "acknowledged_count": 0, "resolved_count": 1, "suppressed_count": 0,
             "attention_count": 2, "tuning_count": 3},
            {"account_id": 9, "account_name": "B", "all_count": 9, "active_count": 9, "stale_count": 0,
             "critical_count": 0, "acknowledged_count": 0, "resolved_count": 0, "suppressed_count": 0,
             "attention_count": 6, "tuning_count": 7}]
    out = mod._aggregate_counts_for_user(rows, {"username": "x"})
    assert out["attention"] == 2 and out["tuning"] == 3          # account 9 must not leak in
    assert out["active"] == 4


def test_counts_sql_uses_the_same_predicates_as_the_tabs():
    mod, log, _ = _alerts_mod(None, [(lambda n: n.startswith("SELECT account_id, account_name"), [], 0)])
    mod.get_flapping_alert_ids = lambda: {4, 8}
    mod._fetch_counts_from_db()
    sql = log[-1][0]
    assert "attention_count" in sql and "tuning_count" in sql
    assert "alert_id IN (4,8)" in sql and "health_score < 70" in sql


# ── GET /api/incidents/fleet-detail ─────────────────────────────────

def _incidents(accessible, forecasts=(), critical=(), names=()):
    import app  # noqa: F401
    import app.auth  # noqa: F401
    seen = {"sql": []}

    class _Cur(FakeCursor):
        def execute(self, sql, params=None):
            n = " ".join(sql.split())
            seen["sql"].append((n, tuple(params or ())))
            if "FROM resource_health h" in n:
                self._pending = [dict(r) for r in critical]
            elif "FROM resources r LEFT JOIN aws_accounts" in n:
                self._pending = [dict(r) for r in names]
            else:
                self._pending = []

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cur([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: accessible)
    captured = {}

    def fake_forecasts(**kw):
        captured.update(kw)
        return [dict(f) for f in forecasts]
    install_stub("app.collector.trend", compute_capacity_forecasts=fake_forecasts)
    install_stub("app.collector.threshold_tuning", count_likely_flapping_alerts=lambda **k: 0)
    return load_module("app/api/incidents.py"), seen, captured


def test_fleet_detail_route_is_registered_before_the_account_id_catchall():
    mod, _, _ = _incidents(None)
    paths = [getattr(r, "path", "") for r in mod.router.routes]
    detail = next(p for p in paths if p.endswith("/fleet-detail"))
    catchall = next(p for p in paths if p.endswith("/{account_id}"))
    assert paths.index(detail) < paths.index(catchall)


def test_fleet_detail_lists_critical_resources_and_capacity_risks_with_names():
    mod, seen, captured = _incidents(
        {7},
        forecasts=[{"resource_id": "i-1", "aws_account_id": 7, "metric_name": "disk_used_percent",
                    "current_value": 83.0, "slope_per_day": 0.4, "days_to_exhaustion": 42.5},
                   {"resource_id": "i-2", "aws_account_id": 7, "metric_name": "disk_used_percent",
                    "current_value": 70.0, "slope_per_day": 1.0, "days_to_exhaustion": 12.0}],
        critical=[{"resource_id": "i-9", "aws_account_id": 7, "health_score": 41, "score_reason": '{"critical_alerts": 2}',
                   "resource_name": "Cloudops_Prod", "resource_type": "ec2", "account_name": "AuroGov"}],
        names=[{"aws_account_id": 7, "resource_id": "i-1", "name": "JUMP", "resource_type": "ec2", "account_name": "U4RAD"}])
    out = mod.fleet_health_detail(current_user={"username": "u"})
    assert out["critical_resources"][0]["resource_name"] == "Cloudops_Prod"
    assert out["critical_resources"][0]["score_reason"] == {"critical_alerts": 2}     # parsed, not a raw JSON string
    assert [r["resource_id"] for r in out["capacity_risks"]] == ["i-2", "i-1"]        # soonest to run out first
    assert out["capacity_risks"][1]["resource_name"] == "JUMP" and out["capacity_risks"][0]["resource_name"] == "i-2"
    assert captured["aws_account_ids"] == {7}                                          # scoped like fleet-summary
    assert any("h.aws_account_id IN (%s)" in n for n, _ in seen["sql"])


def test_fleet_detail_for_a_viewer_with_no_accounts_runs_no_query():
    mod, seen, _ = _incidents(set())
    assert mod.fleet_health_detail(current_user={"username": "u"}) == {"critical_resources": [], "capacity_risks": []}
    assert seen["sql"] == []
