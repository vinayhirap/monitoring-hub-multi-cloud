# tests/test_audit_p11_incident_root.py
"""Audit B3/F2: an incident is named for, and starts at, the EARLIEST breach - not whichever alert the loop held."""
import datetime as dt
import sys

import app          # noqa: F401
import app.auth     # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub  # noqa: E402


def _m():
    install_stub("app.db", get_connection=lambda: None)
    return load_module("app/collector/correlate.py")


T0 = dt.datetime(2026, 9, 29, 16, 40, 33)
T1 = T0 + dt.timedelta(minutes=12)


def _alert(**kw):
    base = {"id": 20, "resource_id": "vol-0952", "metric_name": "VolumeQueueLength", "severity": "WARNING",
            "created_at": T1, "aws_account_id": 10}
    base.update(kw)
    return base


def _partner(**kw):
    base = {"other_alert_id": 10, "other_severity": "WARNING", "other_resource_id": "i-046f",
            "other_metric": "disk_used_percent", "other_triggered": T0}
    base.update(kw)
    return base


def test_the_earlier_partner_becomes_the_root_even_when_the_loop_holds_the_later_alert():
    m = _m()
    root = m.pick_root(_alert(), _partner())
    assert root["resource_id"] == "i-046f" and root["metric_name"] == "disk_used_percent" and root["triggered_at"] == T0
    assert m.incident_title(root) == "disk_used_percent breach on i-046f and related resource(s)"


def test_when_the_held_alert_is_earliest_it_stays_the_root():
    m = _m()
    root = m.pick_root(_alert(created_at=T0 - dt.timedelta(minutes=5)), _partner())
    assert root["resource_id"] == "vol-0952" and root["triggered_at"] == T0 - dt.timedelta(minutes=5)


def test_ties_break_on_severity_then_id_deterministically():
    m = _m()
    a, p = _alert(created_at=T0, severity="WARNING", id=20), _partner(other_severity="CRITICAL", other_alert_id=10)
    assert m.pick_root(a, p)["resource_id"] == "i-046f"                    # critical wins the tie
    a, p = _alert(created_at=T0, severity="WARNING", id=5), _partner(other_severity="WARNING", other_alert_id=10)
    assert m.pick_root(a, p)["resource_id"] == "vol-0952"                  # same severity: lower id
    # and the same answer regardless of which of the two the loop happens to hold
    a2, p2 = _alert(resource_id="i-046f", metric_name="disk_used_percent", created_at=T0, severity="CRITICAL", id=10), \
             _partner(other_resource_id="vol-0952", other_metric="VolumeQueueLength", other_triggered=T0,
                      other_severity="WARNING", other_alert_id=5)
    assert m.pick_root(a2, p2)["resource_id"] == "i-046f"


def test_missing_partner_details_fall_back_to_the_old_behaviour():
    m = _m()
    assert m.pick_root(_alert(), {"other_alert_id": 10})["resource_id"] == "vol-0952"


def test_title_is_bounded_and_survives_a_missing_metric():
    m = _m()
    assert m.incident_title({"resource_id": "i-1", "metric_name": None}) == "Correlated breach on i-1 and related resource(s)"
    assert len(m.incident_title({"resource_id": "x" * 400, "metric_name": "m"})) == 255


def test_incident_row_uses_the_roots_time_and_the_query_selects_the_details():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app/collector/correlate.py").read_text()
    assert "root = pick_root(alert, partner)" in src and "root[\"triggered_at\"]," in src
    assert "a2.metric_name AS other_metric" in src and "a.metric_name, a.severity" in src
