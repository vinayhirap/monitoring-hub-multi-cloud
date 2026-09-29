# tests/test_alert_sync_and_health_ring.py
"""2026-09-29 -- (1) the Overview health ring is in units of SERVICES (the tiles
on the Services page) and coloured by firing alerts; (2) every alert-derived
number is validated against the alerts table on each request so the Overview
can never be older than the Alerts page, across uvicorn workers."""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app.alert_rules  # noqa: F401,E402  (real `app` package before conftest stubs)
import app.threshold_defaults  # noqa: F401,E402
from app.alert_rules import service_ring  # noqa: E402
from tests.conftest import load_module, install_stub  # noqa: E402


# ── service_ring ────────────────────────────────────────────────────

def test_ring_matches_the_users_example_10_services_1_warning_1_critical():
    active = {f"svc{i}" for i in range(10)}
    roll = {"svc0": {"critical": 2, "warning": 5}, "svc1": {"critical": 0, "warning": 3}}
    r = service_ring(active, roll)
    assert (r["total"], r["critical"], r["warning"], r["healthy"]) == (10, 1, 1, 8)
    assert r["critical_services"] == ["svc0"] and r["warning_services"] == ["svc1"]


def test_worst_severity_wins_and_a_service_is_never_counted_twice():
    r = service_ring({"ebs"}, {"ebs": {"critical": 1, "warning": 40}})
    assert (r["critical"], r["warning"], r["healthy"]) == (1, 0, 0)


def test_info_stale_ack_muted_do_not_colour_a_service():
    # rollup only carries counts of FIRING rows in critical/warning/info; the
    # others are separate keys and must be ignored.
    r = service_ring({"ec2"}, {"ec2": {"critical": 0, "warning": 0, "info": 3, "stale": 2,
                                        "acknowledged": 4, "suppressed": 1}})
    assert (r["critical"], r["warning"], r["healthy"]) == (0, 0, 1)


def test_alerting_service_outside_the_active_set_is_still_shown():
    r = service_ring({"ec2"}, {"wafv2": {"critical": 0, "warning": 1}})
    assert r["total"] == 2 and r["warning_services"] == ["wafv2"] and r["healthy"] == 1


def test_no_services_no_alerts_is_an_empty_ring():
    assert service_ring(set(), {})["total"] == 0
    assert service_ring(None, None)["healthy"] == 0


# ── FingerprintCache (cross-worker coherence) ───────────────────────

def _cache_module(fp_values):
    """Load app/alert_cache.py with alerts_fingerprint driven by a list."""
    install_stub("app.db", get_connection=lambda: None)
    mod = load_module("app/alert_cache.py")
    it = {"i": 0}

    def fake_fp():
        v = fp_values[min(it["i"], len(fp_values) - 1)]
        it["i"] += 1
        return v
    mod.alerts_fingerprint = fake_fp
    return mod


def test_cache_reused_while_fingerprint_unchanged():
    mod = _cache_module([("a",), ("a",), ("a",)])
    calls = []
    c = mod.FingerprintCache(ttl=60)
    for _ in range(3):
        c.get(lambda: calls.append(1) or "v")
    assert len(calls) == 1


def test_cache_recomputes_immediately_when_another_worker_changes_alerts():
    # fingerprint changes between the 1st and 2nd request although this
    # worker's cache is well inside its TTL and nobody called clear().
    mod = _cache_module([("old",), ("new",)])
    c = mod.FingerprintCache(ttl=60)
    assert c.get(lambda: "snapshot-1") == "snapshot-1"
    assert c.get(lambda: "snapshot-2") == "snapshot-2"


def test_cache_falls_back_to_ttl_when_the_fingerprint_query_fails():
    mod = _cache_module([("a",)])

    def boom():
        raise RuntimeError("db down")
    mod.alerts_fingerprint = boom
    calls = []
    c = mod.FingerprintCache(ttl=60)
    c.get(lambda: calls.append(1) or "v")
    c.get(lambda: calls.append(1) or "v")
    assert len(calls) == 1


def test_cache_respects_ttl_for_time_based_stale_transitions():
    mod = _cache_module([("a",)] * 5)
    c = mod.FingerprintCache(ttl=0)     # everything is instantly too old
    calls = []
    c.get(lambda: calls.append(1) or "v")
    c.get(lambda: calls.append(1) or "v")
    assert len(calls) == 2


# ── live_accounts: alert fields are fresh inside the slow-cache window ──

def _live_data():
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: None)
    install_stub("app.db", get_connection=lambda: None)
    mod = load_module("app/api/live_data.py")
    mod._get_db_accounts = lambda: [
        {"id": 7, "account_name": "A", "account_id": "1", "default_region": "ap-south-1",
         "role_arn": "r", "external_id": None, "created_at": None, "last_synced_at": None}]
    mod.get_account_summary = lambda *a, **k: {"ec2_total": 3, "ec2_running": 3, "ec2_avg_cpu": 1.0}
    mod._get_active_services_by_account = lambda: {7: {"ec2", "ebs", "s3", "lambda"}}
    mod._get_ec2_instance_health_by_account = lambda: {}
    mod.get_accessible_account_ids = lambda user: None
    return mod


def test_overview_reflects_a_new_alert_on_the_very_next_request_within_cache_ttl():
    mod = _live_data()
    db_calls = []
    orig = mod._get_db_accounts
    mod._get_db_accounts = lambda: db_calls.append(1) or orig()

    mod._get_active_alert_counts_by_account = lambda: {}
    before = mod.live_accounts(current_user={})[0]
    assert before["status"] == "healthy" and before["health_ring"]["healthy"] == 4

    # a critical EBS alert appears (written by the evaluator / another worker)
    mod._get_active_alert_counts_by_account = lambda: {7: {
        "critical": 1, "warning": 0, "critical_resources": 1, "warning_resources": 0,
        "stale": 0, "acknowledged": 0, "suppressed": 0,
        "services": {"ebs": {"critical": 1, "warning": 0}}}}
    after = mod.live_accounts(current_user={})[0]

    assert len(db_calls) == 1, "slow account data must still be served from the 60 s cache"
    assert after["status"] == "critical" and after["critical_alerts"] == 1
    ring = after["health_ring"]
    assert (ring["total"], ring["critical"], ring["warning"], ring["healthy"]) == (4, 1, 0, 3)
    assert ring["critical_services"] == ["ebs"]


def test_resolving_the_alert_clears_it_on_the_next_request():
    mod = _live_data()
    mod._get_active_alert_counts_by_account = lambda: {7: {
        "critical": 0, "warning": 2, "critical_resources": 0, "warning_resources": 2,
        "stale": 0, "acknowledged": 0, "suppressed": 0,
        "services": {"ebs": {"critical": 0, "warning": 2}}}}
    assert mod.live_accounts(current_user={})[0]["status"] == "warning"
    mod._get_active_alert_counts_by_account = lambda: {}
    cleared = mod.live_accounts(current_user={})[0]
    assert cleared["status"] == "healthy" and cleared["health_ring"]["warning"] == 0
