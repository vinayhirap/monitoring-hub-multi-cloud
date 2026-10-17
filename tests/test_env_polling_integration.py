# tests/test_env_polling_integration.py
"""REAL-DATABASE checks for Phase 4c (skipped unless MH_TEST_DB=1; see
tests/test_alert_lifecycle_integration.py for the scratch DB recipe). The scratch schema needs the
fixture PLUS migrations 074..083 and 086 (resources.env_resolved).

  * a UAT resource polled less often is NOT reported stale / 'no data' between its polls
  * the evaluation window and the required breach cycles follow the effective interval
  * P1 metrics on UAT and everything on prod are evaluated exactly as before
  * DEV non-critical metrics are not evaluated; DEV critical ones are
  * scripts/polling_plan_report.py runs read-only against the DB and its numbers equal an
    independent count of what the collectors would request
"""
import json
import os
import subprocess
import sys
from datetime import timedelta

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("MH_TEST_DB"), reason="set MH_TEST_DB=1 to run real-DB tests")

if os.getenv("MH_TEST_DB"):
    from tests.test_alert_lifecycle_integration import DB, run_eval, _connect, utcnow  # noqa: F401
    import app.alert_rules as alert_rules

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def db(monkeypatch):
    for v in ("POLL_UAT_MULTIPLIER", "POLL_DEV_INTERVAL_SECONDS", "POLL_DEV_CRITICAL_INTERVAL_SECONDS",
              "POLL_DEV_NONCRITICAL_MODE", "POLL_DEV_EVAL_CRITICAL_ONLY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("ENV_POLLING_ENABLED", raising=False)
    monkeypatch.delenv("ENV_AWARE_CLASSIFICATION", raising=False)
    alert_rules._ENV_EXTRA_CACHE.clear()
    d = DB()
    d.reset()
    d.account(1, "Acct")
    yield d
    d.conn.close()


def _age(db, minutes):
    """Move one evaluation cycle into the past. The evaluator's cycle gate is cycle_at (migration
    074); DB.age() only shifts last_seen_at, which is not enough to advance a counter."""
    db.x("UPDATE alerts SET cycle_at = COALESCE(cycle_at, triggered_at) - INTERVAL %s MINUTE, "
         "last_seen_at = last_seen_at - INTERVAL %s MINUTE", (minutes, minutes))
    db.x("UPDATE alert_pending SET cycle_at = COALESCE(cycle_at, first_breach_at) - INTERVAL %s MINUTE, "
         "last_seen_at = last_seen_at - INTERVAL %s MINUTE, first_breach_at = first_breach_at - INTERVAL %s MINUTE",
         (minutes, minutes, minutes))


def _flag(monkeypatch, on):
    if on:
        monkeypatch.setenv("ENV_POLLING_ENABLED", "true")
        monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    alert_rules._ENV_EXTRA_CACHE.clear()


def _res(db, rid, rtype, env):
    pk = db.resource(1, rtype, rid)
    db.x("UPDATE resources SET env_resolved=%s, env_source='tag' WHERE id=%s", (env, pk))
    return pk


def _alerts(db):
    return db.q("SELECT resource_id, metric_name, severity, status FROM alerts ORDER BY id")


# ── evaluation window ───────────────────────────────────────────────────
@pytest.mark.parametrize("flag", [False, True])
def test_prod_reading_outside_the_window_is_not_evaluated_either_way(db, monkeypatch, flag):
    _flag(monkeypatch, flag)
    pk = _res(db, "i-prod", "ec2", "prod")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent")
    db.metric(pk, "cpuutilization", 95, age_min=15)           # core window is 10 min
    run_eval()
    assert _alerts(db) == []


def test_uat_reading_inside_the_extended_window_is_evaluated_only_with_flag(db, monkeypatch):
    for flag, expect in ((False, 0), (True, 1)):
        db.reset(); db.account(1, "Acct")
        _flag(monkeypatch, flag)
        pk = _res(db, "i-uat", "ec2", "uat")
        db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent", period=5)
        db.metric(pk, "cpuutilization", 95, age_min=15)       # window 10 -> 20 min for a 3x slowed metric
        run_eval()
        pending = db.one("SELECT COUNT(*) n FROM alert_pending")["n"]
        assert pending == expect, (flag, pending)
        if flag:
            assert _alerts(db) == []                          # 3 cycles required, one seen: no alert yet


# ── staleness (the "no data" risk) ──────────────────────────────────────
def _state(db, aid):
    sql = f"SELECT {alert_rules.state_sql()} AS state {alert_rules.alert_base_from()} WHERE a.id = %s"
    return db.one(sql, (aid,))["state"]


def test_uat_alert_between_polls_is_firing_not_stale(db, monkeypatch):
    pk_u = _res(db, "i-uat", "ec2", "uat")
    pk_p = _res(db, "i-prod", "ec2", "prod")
    a_u = db.alert(1, "i-uat", "cpuutilization", seen_min_ago=28)
    a_p = db.alert(1, "i-prod", "cpuutilization", seen_min_ago=28)
    _flag(monkeypatch, False)
    assert _state(db, a_u) == "stale" and _state(db, a_p) == "stale"        # today: 20 min limit
    _flag(monkeypatch, True)
    assert _state(db, a_u) == "firing"                                      # uat polled every 15 min: 30 min limit
    assert _state(db, a_p) == "stale"                                       # prod untouched
    db.x("UPDATE alerts SET last_seen_at = UTC_TIMESTAMP() - INTERVAL 45 MINUTE WHERE id=%s", (a_u,))
    assert _state(db, a_u) == "stale"                                       # a REAL outage still goes stale


def test_dev_hourly_metric_stale_limit_follows_its_poll(db, monkeypatch):
    _res(db, "i-dev", "ec2", "dev")
    aid = db.alert(1, "i-dev", "cpuutilization", seen_min_ago=70)
    _flag(monkeypatch, True)
    assert _state(db, aid) == "firing"                                      # 20 + 55 = 75 min
    db.x("UPDATE alerts SET last_seen_at = UTC_TIMESTAMP() - INTERVAL 80 MINUTE WHERE id=%s", (aid,))
    assert _state(db, aid) == "stale"


def test_unknown_unclassified_and_p1_uat_keep_the_prod_limit(db, monkeypatch):
    _res(db, "i-unk", "ec2", "unknown")
    _res(db, "i-null", "ec2", None)
    _res(db, "db-uat", "rds", "uat")
    ids = [db.alert(1, "i-unk", "cpuutilization", seen_min_ago=28),
           db.alert(1, "i-null", "cpuutilization", seen_min_ago=28),
           db.alert(1, "db-uat", "cpuutilization", seen_min_ago=28)]
    _flag(monkeypatch, True)
    assert [_state(db, i) for i in ids] == ["stale", "stale", "stale"]


def test_slow_and_extended_classes_and_describe_metrics_are_not_touched(db, monkeypatch):
    _res(db, "bkt", "s3", "uat")
    aid = db.alert(1, "bkt", "bucketsizebytes", seen_min_ago=60 * 40)       # daily class: 3000 min limit
    _flag(monkeypatch, False)
    before = _state(db, aid)
    _flag(monkeypatch, True)
    assert _state(db, aid) == before


# ── required breach cycles (alert-for) ──────────────────────────────────
def _cycles_to_alert(db, rid, limit=14):
    for n in range(1, limit):
        _age(db, 6)
        run_eval()
        if _alerts(db):
            return n
    return None


def test_uat_needs_three_times_the_breach_cycles_prod_unchanged(db, monkeypatch):
    _flag(monkeypatch, True)
    pu = _res(db, "i-uat", "ec2", "uat")
    pp = _res(db, "i-prod", "ec2", "prod")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent", period=5)
    db.metric(pu, "cpuutilization", 95)
    db.metric(pp, "cpuutilization", 95)
    run_eval()
    rows = _alerts(db)
    assert [r["resource_id"] for r in rows] == ["i-prod"]                  # prod: first cycle, as before
    _age(db, 6); db.metric(pu, "cpuutilization", 95); run_eval()                   # cycle 2
    assert [r["resource_id"] for r in _alerts(db)] == ["i-prod"]
    _age(db, 6); db.metric(pu, "cpuutilization", 95); run_eval()                   # cycle 3
    assert sorted(r["resource_id"] for r in _alerts(db)) == ["i-prod", "i-uat"]    # uat: third cycle


def test_uat_p1_metric_alerts_on_the_first_cycle(db, monkeypatch):
    _flag(monkeypatch, True)
    pk = _res(db, "db-uat", "rds", "uat")
    db.threshold(1, "rds", "cpuutilization", 70, 90, unit="Percent", period=5)
    db.metric(pk, "cpuutilization", 95)
    run_eval()
    assert [r["resource_id"] for r in _alerts(db)] == ["db-uat"]


def test_flag_off_uat_alerts_on_the_first_cycle(db, monkeypatch):
    _flag(monkeypatch, False)
    pk = _res(db, "i-uat", "ec2", "uat")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent", period=5)
    db.metric(pk, "cpuutilization", 95)
    run_eval()
    assert len(_alerts(db)) == 1


def test_dev_non_critical_is_not_evaluated_dev_critical_is(db, monkeypatch):
    _flag(monkeypatch, True)
    pe = _res(db, "i-dev", "ec2", "dev")
    pr = _res(db, "db-dev", "rds", "dev")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent", period=5)
    db.threshold(1, "rds", "cpuutilization", 70, 90, unit="Percent", period=5)
    db.metric(pe, "cpuutilization", 95)
    db.metric(pr, "cpuutilization", 95)
    for _ in range(4):
        _age(db, 6); db.metric(pe, "cpuutilization", 95); db.metric(pr, "cpuutilization", 95); run_eval()
    got = {r["resource_id"] for r in _alerts(db)}
    assert got == {"db-dev"}                                               # 2 cycles needed, 4 given


def test_stale_slowed_alert_recovers_normally_with_scaled_healthy_streak(db, monkeypatch):
    _flag(monkeypatch, True)
    pk = _res(db, "i-uat", "ec2", "uat")
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent", period=5)
    aid = db.alert(1, "i-uat", "cpuutilization", seen_min_ago=1)
    db.metric(pk, "cpuutilization", 10)
    for n in range(1, 4):
        _age(db, 6); db.metric(pk, "cpuutilization", 10); run_eval()
        status = db.get(aid)["status"]
        assert status == ("resolved" if n == 3 else "active"), (n, status)


# ── report script ───────────────────────────────────────────────────────
def _report(scenario="current"):
    env = dict(os.environ, DB_HOST=os.getenv("MH_TEST_DB_HOST", "127.0.0.1"),
               DB_PORT=os.getenv("MH_TEST_DB_PORT", "3306"), DB_USER=os.getenv("MH_TEST_DB_USER", "mh"),
               DB_PASSWORD=os.getenv("MH_TEST_DB_PASSWORD", "mh"), DB_NAME=os.getenv("MH_TEST_DB_NAME", "mh_test"),
               POLL_DEV_NONCRITICAL_MODE="slow")
    for v in ("POLL_UAT_MULTIPLIER", "POLL_DEV_INTERVAL_SECONDS", "POLL_DEV_CRITICAL_INTERVAL_SECONDS"):
        env.pop(v, None)
    p = subprocess.run([sys.executable, "scripts/polling_plan_report.py", "--json", "--scenario", scenario],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def _seed_fleet(db):
    for i, env in enumerate(["prod", "prod", "uat", "uat", "uat", "dev", None]):
        pk = db.resource(1, "ec2", f"i-{i}")
        db.x("UPDATE resources SET env_resolved=%s, env_source=%s WHERE id=%s",
             (env, "account" if env == "uat" else "tag", pk))
    for i, env in enumerate(["prod", "uat"]):
        pk = db.resource(1, "rds", f"db-{i}")
        db.x("UPDATE resources SET env_resolved=%s, env_source='tag' WHERE id=%s", (env, pk))


def _row(plan, tier, env, rtype):
    return next(r for r in plan["rows"] if (r["tier"], r["env"], r["resource_type"]) == (tier, env, rtype))


def test_report_matches_an_independent_count_and_writes_nothing(db):
    _seed_fleet(db)
    before = {t: db.one(f"SELECT COUNT(*) n FROM {t}")["n"] for t in ("resources", "metrics", "alerts", "alert_pending")}
    plan = _report()
    # ec2 standard tier: CPU + NetworkIn + NetworkOut = 3 metrics / instance, 288 collections a day
    assert _row(plan, "standard", "prod-like", "ec2")["resources"] == 3            # prod, prod, unclassified
    assert _row(plan, "standard", "prod-like", "ec2")["off_units_day"] == 3 * 3 * 288
    assert _row(plan, "standard", "prod-like", "ec2")["on_units_day"] == 3 * 3 * 288      # prod-like: no saving
    uat = _row(plan, "standard", "uat", "ec2")
    assert uat["resources"] == 3 and uat["off_units_day"] == 3 * 3 * 288
    assert uat["effective_interval"] == 900 and uat["on_units_day"] == 3 * 3 * 96
    dev = _row(plan, "standard", "dev", "ec2")
    assert dev["effective_interval"] == 3600 and dev["on_units_day"] == 1 * 3 * 24
    # rds: 3 critical metrics every 2 min stay at 720/day on prod AND uat (P1 class), dev is 10 min
    assert _row(plan, "critical", "uat", "rds")["on_units_day"] == _row(plan, "critical", "uat", "rds")["off_units_day"]
    assert _row(plan, "critical", "prod-like", "rds")["off_units_day"] == 1 * 3 * 720
    # the report only reads
    after = {t: db.one(f"SELECT COUNT(*) n FROM {t}")["n"] for t in before}
    assert after == before
    assert plan["totals"]["on_units_day"] < plan["totals"]["off_units_day"]


def test_report_defaults_cleared_scenario_is_the_safe_lower_bound(db):
    _seed_fleet(db)
    cur = _report("current")["totals"]
    low = _report("defaults-cleared")["totals"]
    assert low["off_units_day"] == cur["off_units_day"]                            # same baseline
    assert low["on_units_day"] >= cur["on_units_day"]                              # fewer slowed -> less saving


def test_report_prod_only_fleet_shows_zero_saving(db):
    for i in range(4):
        pk = db.resource(1, "ec2", f"p-{i}")
        db.x("UPDATE resources SET env_resolved='prod', env_source='tag' WHERE id=%s", (pk,))
    t = _report()["totals"]
    assert t["on_units_day"] == t["off_units_day"] and t["on_calls_day"] == t["off_calls_day"]
