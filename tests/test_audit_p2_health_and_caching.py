# tests/test_audit_p2_health_and_caching.py
"""
Audit Phase 2 (C3, C5):
  * /api/health/{live,ready,detail}
  * /api/live/accounts: stale-while-revalidate, invalidate keeps data
  * fleet-summary/fleet-detail share one cached capacity forecast per scope
"""
import sys
import threading
import time

import app          # noqa: F401
import app.auth     # noqa: F401
import app.utils    # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub  # noqa: E402
from tests import test_live_data_accounts_cache as _ld  # noqa: E402


# ── health ───────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, row=None, boom=False):
        self.row, self.boom = row, boom
    def execute(self, sql, params=None):
        if self.boom:
            raise RuntimeError("db down")
    def fetchone(self):
        return self.row
    def close(self):
        pass


class _Conn:
    def __init__(self, row=None, boom=False):
        self.row, self.boom = row, boom
    def cursor(self, dictionary=False):
        return _Cur(self.row, self.boom)
    def close(self):
        pass


def _health(conn):
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.db", get_connection=lambda: conn)
    return load_module("app/api/health.py")


def test_live_needs_no_database():
    mod = _health(None)          # get_connection would return None -> proves it is not called
    assert mod.live() == {"status": "ok"}


def test_ready_ok_and_minimal_body():
    mod = _health(_Conn(row=(1,)))
    assert mod.ready() == {"status": "ok"}


def test_ready_503_when_db_down_without_leaking_detail():
    mod = _health(_Conn(boom=True))
    resp = mod.ready()
    assert resp.status_code == 503
    assert b"db down" not in resp.body and b"unavailable" in resp.body


def _row(**kw):
    base = {"active_accounts": 2, "never_synced": 0, "newest_age_s": 120,
            "oldest_age_s": 600, "stale_accounts": 0}
    base.update(kw)
    return base


def test_detail_ok_when_all_accounts_fresh():
    mod = _health(_Conn(row=_row()))
    d = mod.detail(current_user={"role": "admin"})
    assert d["status"] == "ok" and d["collector"]["status"] == "ok"
    assert d["database"]["ok"] is True


def test_detail_degraded_when_an_account_is_stale():
    mod = _health(_Conn(row=_row(stale_accounts=1, oldest_age_s=99999)))
    d = mod.detail(current_user={"role": "admin"})
    assert d["status"] == "degraded" and d["collector"]["stale_accounts"] == 1


def test_never_synced_counts_as_stale():
    mod = _health(_Conn(row=_row(never_synced=1)))
    assert mod.detail(current_user={})["collector"]["status"] == "stale"


def test_detail_idle_with_no_active_accounts():
    mod = _health(_Conn(row=_row(active_accounts=0, newest_age_s=None, oldest_age_s=None)))
    assert mod.detail(current_user={})["collector"]["status"] == "idle"


def test_detail_down_when_db_unreachable():
    mod = _health(_Conn(boom=True))
    assert mod.detail(current_user={})["status"] == "down"


def test_detail_is_permission_gated_and_live_ready_are_not():
    src = open(__file__.rsplit("/tests/", 1)[0] + "/app/api/health.py").read()
    assert 'Depends(require_permission("operations.view"))' in src
    main = open(__file__.rsplit("/tests/", 1)[0] + "/app/main.py").read()
    assert "app.include_router(health_router)" in main
    assert "health_router, " not in main      # not wrapped in the auth dependency


# ── live accounts: stale-while-revalidate ────────────────────────────

def _live():
    # app.metric_meta (imported by live_data) needs get_db_cursor on the app.db stub
    install_stub("app.db", get_connection=lambda: None, get_db_cursor=lambda *a, **k: None)
    # only ADDS names metric_meta imports; never overrides the real module's functions
    import sys as _s
    if "app.threshold_defaults" not in _s.modules:
        install_stub("app.threshold_defaults", is_static_only_metric=lambda n: False,
                     is_placeholder_threshold=lambda *a: False,
                     resolve_db_metric_name=lambda rt, n: (n or "").lower(),
                     is_capacity_percent_metric=lambda n: False)
    mod = _ld._load_live_data()
    accounts = [{"id": 1, "account_name": "a1", "account_id": "111", "default_region": "r",
                 "role_arn": "x", "external_id": None, "created_at": None, "last_synced_at": None}]
    import types
    mod._alert_rules = types.SimpleNamespace(service_ring=lambda services, counts: [])  # this copy only
    mod._get_db_accounts = lambda: accounts
    mod._get_active_services_by_account = lambda: {}
    mod._get_active_alert_counts_by_account = lambda: {}
    mod._get_ec2_instance_health_by_account = lambda: {}
    mod.get_accessible_account_ids = lambda user: None
    return mod


def test_expired_cache_serves_stale_instantly_and_refreshes_in_background():
    mod = _live()
    gate, started, calls = threading.Event(), threading.Event(), {"n": 0}

    def slow_summary(*a, **k):
        calls["n"] += 1
        if calls["n"] > 1:            # every build after the first is "slow AWS"
            started.set()
            gate.wait(5)
        return {"ec2_total": calls["n"]}
    mod.get_account_summary = slow_summary

    admin = {"id": 1, "role": "admin"}
    first = mod.live_accounts(current_user=admin)            # cold: synchronous build
    assert first[0]["ec2_total"] == 1

    mod._accounts_cache["ts"] = 0                            # expire it
    t0 = time.time()
    second = mod.live_accounts(current_user=admin)           # must NOT wait for the slow build
    assert time.time() - t0 < 1.0
    assert second[0]["ec2_total"] == 1                       # stale value served
    assert started.wait(2)                                   # background refresh did start
    gate.set()
    for _ in range(50):
        if mod._accounts_cache["data"][0]["ec2_total"] == 2:
            break
        time.sleep(0.05)
    assert mod.live_accounts(current_user=admin)[0]["ec2_total"] == 2


def test_invalidate_expires_but_keeps_data():
    mod = _live()
    mod.get_account_summary = lambda *a, **k: {"ec2_total": 3}
    mod.live_accounts(current_user={"id": 1})
    mod.invalidate_accounts_cache()
    assert mod._accounts_cache["data"] is not None and mod._accounts_cache["ts"] == 0


def test_concurrent_refreshes_are_single_flight():
    mod = _live()
    n = {"builds": 0}

    def summary(*a, **k):
        n["builds"] += 1
        time.sleep(0.2)
        return {}
    mod.get_account_summary = summary
    threads = [threading.Thread(target=mod._refresh_accounts_cache) for _ in range(5)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert n["builds"] == 1


# ── fleet endpoints share one forecast ───────────────────────────────

def _incidents():
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    install_stub("app.db", get_connection=lambda: None)
    return load_module("app/api/incidents.py")


def test_forecast_computed_once_per_scope_within_ttl():
    mod = _incidents()
    calls = []
    install_stub("app.collector.trend",
                 compute_capacity_forecasts=lambda aws_account_ids=None, aws_resource_id=None:
                 calls.append(aws_account_ids) or [{"resource_id": "i-1"}])
    a = mod._capacity_forecasts_cached({1, 2})
    b = mod._capacity_forecasts_cached({2, 1})      # same scope, different order/object
    c = mod._capacity_forecasts_cached(None)        # admin scope is a different key
    d = mod._capacity_forecasts_cached({3})         # another scope is never served from {1,2}
    assert len(calls) == 3
    assert a is b


def test_forecast_cache_expires():
    mod = _incidents()
    calls = []
    install_stub("app.collector.trend",
                 compute_capacity_forecasts=lambda aws_account_ids=None, aws_resource_id=None: calls.append(1) or [])
    mod._capacity_forecasts_cached(None)
    mod._forecast_cache[None] = (time.time() - mod._FORECAST_TTL - 1, [])
    mod._capacity_forecasts_cached(None)
    assert len(calls) == 2
