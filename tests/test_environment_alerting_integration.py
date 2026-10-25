"""
REAL-DATABASE tests for phase 4a: the evaluator's environment handling behind
ENV_AWARE_CLASSIFICATION. Same setup as test_alert_lifecycle_integration.py
(MH_TEST_DB=1, tests/fixtures/alerts_test_schema.sql).
"""
import os

import pytest

from tests.test_alert_lifecycle_integration import db, run_eval  # noqa: F401  (fixtures/helpers)

pytestmark = pytest.mark.skipif(not os.getenv("MH_TEST_DB"), reason="set MH_TEST_DB=1 to run real-DB tests")


def _setup(db, rid, metric_value, tags=None, name=None):
    r = db.resource(1, "ec2", rid, name=name, tags=tags)
    db.threshold(1, "ec2", "cpuutilization", 70, 90, unit="Percent")
    db.metric(r, "cpuutilization", metric_value)
    return r


def _alert(db, rid):
    return db.one("SELECT severity, environment FROM alerts WHERE resource_id=%s", (rid,))


def test_flag_off_is_the_old_behaviour_untagged_means_prod_warning(db, monkeypatch):
    monkeypatch.delenv("ENV_AWARE_CLASSIFICATION", raising=False)
    _setup(db, "dev-box", 80)
    run_eval()
    a = _alert(db, "dev-box")
    assert a["severity"] == "WARNING" and a["environment"] == "prod"


def test_flag_on_dev_named_resource_warning_is_info_and_labelled_nonprod(db, monkeypatch):
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    _setup(db, "dev-box", 80)
    run_eval()
    a = _alert(db, "dev-box")
    assert a["severity"] == "INFO" and a["environment"] == "nonprod"


def test_flag_on_unplaced_is_prod_strength_and_prod(db, monkeypatch):
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    _setup(db, "i-0abc", 80)
    run_eval()
    a = _alert(db, "i-0abc")
    assert a["severity"] == "WARNING" and a["environment"] == "prod"


def test_flag_on_critical_stays_critical_in_nonprod(db, monkeypatch):
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    _setup(db, "dev-box", 95)
    run_eval()
    a = _alert(db, "dev-box")
    assert a["severity"] == "CRITICAL" and a["environment"] == "nonprod"


def test_flag_on_persisted_classification_wins_over_live_resolution(db, monkeypatch):
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    r = _setup(db, "dev-box", 80)
    db.x("UPDATE resources SET env_resolved='prod', env_source='manual' WHERE id=%s", (r,))
    run_eval()
    a = _alert(db, "dev-box")
    assert a["severity"] == "WARNING" and a["environment"] == "prod"


def test_flag_on_unrecognised_legacy_env_value_is_prod(db, monkeypatch):
    # 'ignore' no longer exists; an unrecognised stored value normalises to PROD (fail-safe).
    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    r = _setup(db, "i-0abc", 80)
    db.x("UPDATE resources SET env_resolved='ignore', env_source='manual' WHERE id=%s", (r,))
    run_eval()
    a = _alert(db, "i-0abc")
    assert a["severity"] == "WARNING" and a["environment"] == "prod"
