"""
REAL-DATABASE tests for scripts/flag_impact_report.py (workstream D1).

Same setup as test_alert_lifecycle_integration.py (MH_TEST_DB=1, scripts/build_test_db.py).

Two kinds of test:
  * hand-built fixtures with known expected counts (the numbers below are worked out by hand in the
    comments, not derived from the code under test);
  * a PARITY test that runs the REAL evaluator with the flag off and on and checks the report's
    prediction against what the evaluator actually wrote.
"""
import csv
import json
import os

import pytest

from tests.conftest import load_module
from tests.test_alert_lifecycle_integration import (  # noqa: F401  (fixtures/helpers)
    db, run_eval, _connect,
)

pytestmark = pytest.mark.skipif(not os.getenv("MH_TEST_DB"), reason="set MH_TEST_DB=1 to run real-DB tests")

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(_HERE, "..", "scripts", "flag_impact_report.py")


def _report_module():
    return load_module("scripts/flag_impact_report.py")


def _engine():
    """The real evaluator module and the real classifier, loaded WITHOUT running a cycle (a cycle would
    resolve/alter the seeded alerts). Stubs mirror run_eval() in test_alert_lifecycle_integration."""
    from tests.test_alert_lifecycle_integration import _get_db_cursor
    from tests.conftest import install_stub
    install_stub("app.db", get_connection=_connect, get_db_cursor=_get_db_cursor)
    install_stub("app.ws.publisher", publish_alert=lambda **k: None, publish_alert_resolved=lambda **k: None)
    install_stub("app.api.live_data", invalidate_accounts_cache=lambda: None)
    install_stub("app.api.alerts", _invalidate_cache=lambda: None)
    ev = load_module("app/collector/alert_evaluator.py")
    import app.environment_classifier as envc
    return ev, envc


def _open_alert(db, acct, rid, sev, stored_env="prod", status="active", metric="cpuutilization"):
    aid = db.alert(acct, rid, metric, sev, status=status)
    db.x("UPDATE alerts SET environment=%s, group_key=%s WHERE id=%s",
         (stored_env, f"{acct}:ec2:{metric}", aid))
    return aid


def _seed(db):
    """Nine open alerts + one resolved (must be ignored). Expected results are in the tests."""
    # account 1 ------------------------------------------------------------------------------
    db.resource(1, "ec2", "dev-web")                                   # name token dev  -> dev
    db.resource(1, "ec2", "prod-db-1")                                 # name token prod -> prod
    db.resource(1, "ec2", "i-0abc")                                    # nothing         -> unknown
    db.resource(1, "ec2", "staging-api")                               # name token      -> uat
    db.resource(1, "ec2", "i-tagged-dev", tags={"Environment": "dev"})  # tag             -> dev
    r = db.resource(1, "ec2", "i-manual-prod", tags={"Environment": "dev"})
    db.x("UPDATE resources SET env_resolved='prod', env_source='manual' WHERE id=%s", (r,))  # manual wins
    db.resource(1, "ec2", "dev-crit")                                  # dev, but CRITICAL
    # account 2 ------------------------------------------------------------------------------
    db.resource(2, "ec2", "dev-other")                                 # dev
    # (no resource row at all for "ghost")

    _open_alert(db, 1, "dev-web", "WARNING")                           # stored prod label, WARNING
    _open_alert(db, 1, "prod-db-1", "WARNING")
    _open_alert(db, 1, "i-0abc", "WARNING")
    _open_alert(db, 1, "staging-api", "WARNING", status="acknowledged")
    _open_alert(db, 1, "i-tagged-dev", "INFO", stored_env="dev")       # flag-off label already 'dev'
    _open_alert(db, 1, "i-manual-prod", "INFO", stored_env="dev")
    _open_alert(db, 1, "dev-crit", "CRITICAL")
    _open_alert(db, 2, "dev-other", "WARNING")
    _open_alert(db, 2, "ghost", "WARNING")
    resolved = _open_alert(db, 1, "dev-web", "WARNING", status="resolved", metric="memory")
    return resolved


def _run(db, account_id=None):
    ev, envc = _engine()
    mod = _report_module()
    conn = _connect()
    conn.autocommit = True
    try:
        rows, summary = mod.build_report(conn, ev, envc, account_id)
    finally:
        conn.close()
    return mod, ev, rows, summary


# ── counts match the hand-built fixture ─────────────────────────────────────────────────────────

def test_counts_for_all_accounts_match_the_hand_worked_numbers(db):
    _seed(db)
    _, _, rows, s = _run(db)

    # open = 9 (the resolved one is excluded); only "ghost" has no resource row
    assert s["open_alerts"] == 9
    assert s["no_resource_row"] == 1
    # WARNING->INFO: dev-web, staging-api (acknowledged counts as open), dev-other           = 3
    # INFO->WARNING: i-manual-prod (manual 'prod' beats its dev tag)                          = 1
    assert s["severity_changes"] == {"WARNING->INFO": 3, "INFO->WARNING": 1}
    # label changes: dev-web prod->dev, i-0abc prod->unknown, staging-api prod->uat,
    #                i-manual-prod dev->prod, dev-crit prod->dev, dev-other prod->dev        = 6
    # (prod-db-1 prod->prod and i-tagged-dev dev->dev are unchanged; ghost has no resource)
    assert s["label_changes"] == 6
    assert s["label_pairs"][("prod", "dev")] == 3
    assert s["label_pairs"][("prod", "unknown")] == 1
    assert s["label_pairs"][("prod", "uat")] == 1
    assert s["label_pairs"][("dev", "prod")] == 1
    assert s["critical_open"] == 1
    # the open CRITICAL on a dev resource lands on WARNING today but INFO with the flag on
    assert s["critical_deescalation_differs"] == 1


def test_unknown_stays_warning_and_dev_named_flips_warning_to_info(db):
    """The two cases the acceptance criteria call out."""
    _seed(db)
    _, _, rows, _ = _run(db)
    by_res = {r["resource_id"]: r for r in rows}
    unknown = by_res["i-0abc"]
    assert unknown["flag_on_environment"] == "unknown"
    assert unknown["severity_if_raised_flag_on"] == "WARNING"      # never weaker than prod
    assert unknown["severity_change"] == ""                        # stored WARNING stays WARNING
    dev = by_res["dev-web"]
    assert dev["flag_on_environment"] == "dev"
    assert dev["severity_change"] == "WARNING->INFO"


def test_account_filter(db):
    _seed(db)
    _, _, rows, s = _run(db, account_id=1)
    assert s["open_alerts"] == 7
    assert s["no_resource_row"] == 0
    assert s["severity_changes"] == {"WARNING->INFO": 2, "INFO->WARNING": 1}
    assert s["label_changes"] == 5
    assert {r["account_id"] for r in rows} == {1}


def test_grouping_by_account_environment_and_type(db):
    _seed(db)
    _, _, _, s = _run(db)
    assert s["by_account"]["U4RAD"]["alerts"] == 7 and s["by_account"]["AuroGov"]["alerts"] == 1
    assert s["by_account"]["U4RAD"]["WARNING->INFO"] == 2
    assert s["by_account"]["AuroGov"]["WARNING->INFO"] == 1
    assert s["by_environment"]["dev"]["alerts"] == 4          # dev-web, i-tagged-dev, dev-crit, dev-other
    assert s["by_environment"]["prod"]["INFO->WARNING"] == 1
    assert s["by_resource_type"]["ec2"]["alerts"] == 8        # ghost has no type and is not counted


def test_top_resources_are_ranked_and_capped(db):
    _seed(db)
    # five more alerts on one resource: it must lead the list
    for m in ("a", "b", "c", "d", "e"):
        _open_alert(db, 1, "dev-web", "WARNING", metric=m)
    _, _, _, s = _run(db)
    top = s["top_resources"][0]
    assert top["sample"]["resource_id"] == "dev-web" and top["affected"] == 6 and top["alerts"] == 6
    assert len(s["top_resources"]) <= 20


def test_text_and_csv_output(db, tmp_path):
    _seed(db)
    mod, ev, rows, s = _run(db)
    text = mod.render_text(s, None, mod._in_place_note(ev))
    assert "open alerts (active + acknowledged)        : 9" in text
    assert "WARNING->INFO" in text and "backfill_alert_priority.py" in text
    out = tmp_path / "impact.csv"
    mod.write_csv(rows, str(out))
    with open(out, newline="", encoding="utf-8") as f:
        got = list(csv.DictReader(f))
    assert len(got) == 9
    dev_web = next(r for r in got if r["resource_id"] == "dev-web")
    assert dev_web["stored_severity"] == "WARNING" and dev_web["severity_change"] == "WARNING->INFO"
    assert dev_web["flag_on_environment"] == "dev"


# ── read-only guarantees ────────────────────────────────────────────────────────────────────────

def test_report_changes_nothing_in_the_database(db):
    _seed(db)

    def snapshot():
        return (db.q("SELECT id, severity, environment, status FROM alerts ORDER BY id"),
                db.q("SELECT id, env_resolved, env_source, env_override, tag_state FROM resources ORDER BY id"))
    before = snapshot()
    _run(db)
    assert snapshot() == before


def test_script_source_contains_no_write_statements():
    src = open(SCRIPT, encoding="utf-8").read().upper()
    for word in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER ", "TRUNCATE", "REPLACE INTO"):
        assert word not in src, f"read-only script must not contain {word!r}"


# ── parity with the REAL evaluator ──────────────────────────────────────────────────────────────

def _breaching_resources(db):
    """Warning-level breaches (80 vs 70/90) on a spread of environments."""
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent")
    out = {}
    for rid, tags in (("dev-web", None), ("prod-db-1", None), ("i-0abc", None), ("staging-api", None),
                      ("i-tagged-dev", {"Environment": "dev"}), ("i-tagged-prod", {"Environment": "prod"}),
                      ("i-tagged-weird", {"Environment": "Qa"})):
        pk = db.resource(1, "ec2", rid, tags=tags)
        db.metric(pk, "cpuutilization", 80)
        out[rid] = pk
    return out


def _evaluator_result(db):
    return {r["resource_id"]: (r["severity"], r["environment"])
            for r in db.q("SELECT resource_id, severity, environment FROM alerts")}


def _predicted(db):
    ev, envc = _engine()
    mod = _report_module()
    conn = _connect()
    conn.autocommit = True
    try:
        rows, _ = mod.build_report(conn, ev, envc)
    finally:
        conn.close()
    return {r["resource_id"]: r for r in rows}


def test_report_prediction_matches_what_the_real_evaluator_does(db, monkeypatch):
    _breaching_resources(db)

    # flag OFF: the evaluator raises the alerts the production box has today
    monkeypatch.delenv("ENV_AWARE_CLASSIFICATION", raising=False)
    run_eval()
    off_actual = _evaluator_result(db)
    assert len(off_actual) == 7
    predicted = _predicted(db)
    for rid, (sev, label) in off_actual.items():
        assert predicted[rid]["severity_if_raised_flag_off"] == sev, rid
        assert predicted[rid]["flag_off_label"] == label, rid
        assert predicted[rid]["stored_severity"] == sev and predicted[rid]["stored_environment"] == label

    # flag ON: wipe the alerts and let the evaluator raise them again
    db.x("DELETE FROM alerts")
    db.x("DELETE FROM alert_pending")
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    run_eval()
    on_actual = _evaluator_result(db)
    assert len(on_actual) == 7
    for rid, (sev, label) in on_actual.items():
        assert predicted[rid]["severity_if_raised_flag_on"] == sev, rid
        assert predicted[rid]["flag_on_environment"] == label, rid
    # sanity on the interesting ones, straight from the evaluator
    assert on_actual["dev-web"] == ("INFO", "dev")
    assert on_actual["i-0abc"] == ("WARNING", "unknown")
    assert on_actual["staging-api"] == ("INFO", "uat")
    assert on_actual["i-tagged-weird"] == ("INFO", "uat")      # "Qa" is a uat token
