# tests/test_alert_priority_integration.py
"""
REAL-DATABASE tests for phase 4b patch 1 (alert priority P1-P4). Same setup as
tests/test_alert_lifecycle_integration.py (MH_TEST_DB=1, scratch database built by scripts/build_test_db.py).
Covers what the fake-cursor unit tests cannot: migration 101's column, the evaluator hook, the list/summary SQL.
"""
import os

import pytest

from tests.test_alert_lifecycle_integration import db, run_eval, _api, USER  # noqa: F401  (db is a fixture)

pytestmark = pytest.mark.skipif(not os.getenv("MH_TEST_DB"), reason="set MH_TEST_DB=1 to run real-DB tests")


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("PRIORITY_ENABLED", "true")
    monkeypatch.delenv("ENV_AWARE_CLASSIFICATION", raising=False)


def _prio(db, aid):
    return db.one("SELECT priority FROM alerts WHERE id=%s", (aid,))["priority"]


def test_evaluator_cycle_sets_priority_for_any_open_alert(db):
    r1 = db.resource(1, "ec2", "i-crit-prod", tags={"Environment": "prod"})
    db.resource(1, "ec2", "i-dev", tags={"Environment": "dev"})
    db.resource(1, "ec2", "i-dev-crit", tags={"Environment": "dev", "Criticality": "critical"})
    a1 = db.alert(1, "i-crit-prod", "cpuutilization", "CRITICAL")
    a2 = db.alert(1, "i-dev", "cpuutilization", "INFO")
    a3 = db.alert(1, "i-dev-crit", "cpuutilization", "INFO")
    db.x("UPDATE alerts SET environment='dev' WHERE id IN (%s,%s)", (a2, a3))
    run_eval()
    assert (_prio(db, a1), _prio(db, a2), _prio(db, a3)) == (1, 4, 3)


def test_flag_off_leaves_the_column_alone(db, monkeypatch):
    monkeypatch.setenv("PRIORITY_ENABLED", "false")
    db.resource(1, "ec2", "i-x")
    a = db.alert(1, "i-x", "cpuutilization", "CRITICAL")
    run_eval()
    assert _prio(db, a) is None


def test_reclassifying_a_resource_moves_its_open_alert(db, monkeypatch):
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    db.resource(1, "ec2", "i-move")
    a = db.alert(1, "i-move", "cpuutilization", "CRITICAL")
    db.x("UPDATE resources SET env_resolved='prod' WHERE resource_id='i-move'")
    run_eval()
    assert _prio(db, a) == 1
    db.x("UPDATE resources SET env_resolved='dev' WHERE resource_id='i-move'")
    run_eval()
    assert _prio(db, a) == 3


def test_resolved_alerts_are_not_touched(db):
    db.resource(1, "ec2", "i-old")
    a = db.alert(1, "i-old", "cpuutilization", "CRITICAL", status="resolved")
    run_eval()
    assert _prio(db, a) is None


def test_list_orders_p1_first_filters_and_exposes_priority(db):
    for rid in ("i-a", "i-b", "i-c"):
        db.resource(1, "ec2", rid)
    low = db.alert(1, "i-a", "cpuutilization", "INFO")
    top = db.alert(1, "i-b", "cpuutilization", "CRITICAL")
    mid = db.alert(1, "i-c", "cpuutilization", "WARNING")
    db.x("UPDATE alerts SET priority=4 WHERE id=%s", (low,))
    db.x("UPDATE alerts SET priority=1 WHERE id=%s", (top,))
    db.x("UPDATE alerts SET priority=2 WHERE id=%s", (mid,))
    api = _api()
    rows, total = api._fetch_alerts_from_db(USER, "all")
    assert [r["priority"] for r in rows] == [1, 2, 4] and total == 3
    rows, total = api._fetch_alerts_from_db(USER, "all", priority=2)
    assert total == 1 and rows[0]["resource"] == "i-c"


def test_list_without_flag_has_no_priority_field_and_ignores_the_filter(db, monkeypatch):
    monkeypatch.setenv("PRIORITY_ENABLED", "false")
    db.resource(1, "ec2", "i-a")
    db.alert(1, "i-a", "cpuutilization", "CRITICAL")
    api = _api()
    rows, total = api._fetch_alerts_from_db(USER, "all", priority=3)
    assert total == 1 and "priority" not in rows[0]


def test_priority_summary_counts_only_firing(db):
    for rid in ("i-1", "i-2", "i-3"):
        db.resource(1, "ec2", rid)
    f = db.alert(1, "i-1", "cpuutilization", "CRITICAL")
    ack = db.alert(1, "i-2", "cpuutilization", "CRITICAL", status="acknowledged")
    res = db.alert(1, "i-3", "cpuutilization", "CRITICAL", status="resolved")
    db.x("UPDATE alerts SET priority=1 WHERE id IN (%s,%s,%s)", (f, ack, res))
    api = _api()
    out = api.priority_summary(account_id=None, current_user=USER)
    assert out["enabled"] and out["counts"]["1"] == 1 and out["total"] == 1


def test_priority_summary_is_empty_when_flag_off(db, monkeypatch):
    monkeypatch.setenv("PRIORITY_ENABLED", "false")
    api = _api()
    assert api.priority_summary(account_id=None, current_user=USER) == {"enabled": False, "counts": {}, "total": 0}


def test_backfill_dry_run_then_apply(db):
    db.resource(1, "ec2", "i-bf")
    a = db.alert(1, "i-bf", "cpuutilization", "CRITICAL")
    from app.collector import priority_backfill
    import tests.test_alert_lifecycle_integration as lc
    conn = lc._connect()
    cur = conn.cursor(dictionary=True)
    res = priority_backfill.run(cur, apply=False)
    assert res["changed"] == 1 and _prio(db, a) is None
    res = priority_backfill.run(cur, apply=True)
    conn.commit()
    assert _prio(db, a) == 1
    assert priority_backfill.run(cur, apply=True)["changed"] == 0     # idempotent
    cur.close()
    conn.close()
