# tests/test_multivariate_anomaly.py
"""
Coverage for app/collector/multivariate_anomaly.py -- AIOps roadmap
Phase 2 (2026-09-14).

Unlike the Phase 1 tests (which mock every query result because the
logic under test is pure Python/SQL-shaping), these tests generate
REAL synthetic metric_history rows and let the actual pandas pivot +
scikit-learn IsolationForest run end-to-end -- the thing worth testing
here is the numerical behavior itself (does an obviously-joint-shifted
reading get flagged, does stable multi-metric data NOT get flagged),
which a mocked model would trivially hide. Only the DB layer
(get_connection) is faked, via the same FakeCursor/FakeConn/
install_stub convention as every other test in this suite.
"""
import sys
from datetime import datetime, timedelta

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn


def _stable_history_rows(resource_id="i-stable-1", n=80, start=None):
    """n rows x 3 metrics, all mild noise around a fixed baseline --
    nothing here should ever look anomalous to the model."""
    import random
    random.seed(42)
    start = start or datetime(2026, 9, 1, 0, 0, 0)
    rows = []
    for i in range(n):
        ts = start + timedelta(minutes=5 * i)
        rows.append({"metric_name": "CPUUtilization", "metric_timestamp": ts,
                     "metric_value": 40 + random.uniform(-2, 2)})
        rows.append({"metric_name": "NetworkIn", "metric_timestamp": ts,
                     "metric_value": 1000 + random.uniform(-50, 50)})
        rows.append({"metric_name": "DiskReadOps", "metric_timestamp": ts,
                     "metric_value": 200 + random.uniform(-10, 10)})
    return rows


def _history_with_joint_anomaly_at_end(n=80, buckets=3):
    """Same stable history, but the LAST `buckets` readings jointly shift all
    three metrics far outside anything seen before -- individually each
    value might still look plausible-ish, but the combination should
    not. Phase 2 (2026-10-02): the detector now needs the deviation to be
    SUSTAINED across MIN_CONSECUTIVE_BUCKETS (3) buckets, so the default
    here is 3; buckets=1 models a one-reading blip that must be ignored."""
    rows = _stable_history_rows(n=n)
    last_ts = rows[-1]["metric_timestamp"]
    for i in range(1, buckets + 1):
        ts = last_ts + timedelta(minutes=5 * i)
        rows.append({"metric_name": "CPUUtilization", "metric_timestamp": ts, "metric_value": 95})
        rows.append({"metric_name": "NetworkIn", "metric_timestamp": ts, "metric_value": 9000})
        rows.append({"metric_name": "DiskReadOps", "metric_timestamp": ts, "metric_value": 2000})
    return rows


def _install_db_stub(history_rows, resource_id="i-1", account_id=7,
                      existing_alert=None, active_anomaly_alerts=None):
    inserted = []
    updated = []
    resolved = []

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if "COUNT(DISTINCT h.metric_name)" in normalized:
                self._pending = [{"resource_id": resource_id, "aws_account_id": account_id, "metric_count": 3}]
            elif normalized.startswith("SELECT h.metric_name, h.metric_timestamp"):
                self._pending = history_rows
            elif "metric_name = 'multivariate_anomaly'" in normalized and normalized.startswith("SELECT id FROM alerts WHERE aws_account_id"):
                self._pending = [existing_alert] if existing_alert else []
            elif normalized.startswith("SELECT resource_type, tags FROM resources"):
                self._pending = [{"resource_type": "ec2_instance", "tags": '{"environment":"prod"}'}]
            elif normalized.startswith("INSERT INTO alerts"):
                inserted.append(params)
                self._pending = []
            elif normalized.startswith("UPDATE alerts SET current_value"):
                updated.append(params)
                self._pending = []
            elif normalized.startswith("SELECT id, aws_account_id, resource_id FROM alerts"):
                self._pending = active_anomaly_alerts or []
            elif normalized.startswith("UPDATE alerts SET status = 'resolved'"):
                resolved.append(params)
                self._pending = []
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    return inserted, updated, resolved


def test_stable_multivariate_history_is_not_flagged():
    rows = _stable_history_rows(n=80)
    inserted, updated, resolved = _install_db_stub(rows)
    mod = load_module("app/collector/multivariate_anomaly.py")

    anomalous_count = mod.detect_multivariate_anomalies()

    assert anomalous_count == 0
    assert inserted == []


def test_joint_anomaly_creates_new_alert():
    rows = _history_with_joint_anomaly_at_end(n=80)
    inserted, updated, resolved = _install_db_stub(rows, resource_id="i-anomalous-1", account_id=7)
    mod = load_module("app/collector/multivariate_anomaly.py")

    anomalous_count = mod.detect_multivariate_anomalies()

    assert anomalous_count == 1
    assert len(inserted) == 1
    params = inserted[0]
    # (aws_account_id, resource_id, environment, group_key, score)
    # aws_account_id MUST be written: since migration 048 every reader joins
    # on it, so an INSERT without it is an invisible alert.
    assert params[0] == 7
    assert params[1] == "i-anomalous-1"
    assert params[2] == "prod"
    assert params[3] == "7:ec2_instance:multivariate_anomaly"
    assert isinstance(params[4], float)


def test_joint_anomaly_updates_existing_alert_instead_of_duplicating():
    rows = _history_with_joint_anomaly_at_end(n=80)
    inserted, updated, resolved = _install_db_stub(
        rows, resource_id="i-anomalous-1", existing_alert={"id": 999},
        active_anomaly_alerts=[{"id": 999, "aws_account_id": 7, "resource_id": "i-anomalous-1"}],
    )
    mod = load_module("app/collector/multivariate_anomaly.py")

    mod.detect_multivariate_anomalies()

    assert inserted == []  # no duplicate INSERT
    assert len(updated) == 1
    assert updated[0][-1] == 999  # UPDATE ... WHERE id = 999


def test_resource_no_longer_anomalous_gets_auto_resolved():
    """A resource with a currently-active multivariate_anomaly alert,
    but whose latest reading is no longer anomalous, should have that
    alert auto-resolved."""
    rows = _stable_history_rows(n=80)  # nothing anomalous this cycle
    inserted, updated, resolved = _install_db_stub(
        rows, resource_id="i-recovered-1",
        active_anomaly_alerts=[{"id": 555, "aws_account_id": 7, "resource_id": "i-recovered-1"}],
    )
    mod = load_module("app/collector/multivariate_anomaly.py")

    mod.detect_multivariate_anomalies()

    assert len(resolved) == 1
    assert resolved[0][0] == 555


def test_insufficient_metrics_never_queried_for_history():
    """A resource reporting fewer than MIN_METRICS_PER_RESOURCE metrics
    should be filtered out at the candidate-selection query itself
    (HAVING clause) -- this test just confirms the module doesn't crash
    when the candidate list is empty."""
    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if "COUNT(DISTINCT h.metric_name)" in normalized:
                self._pending = []  # HAVING filtered everything out
            elif normalized.startswith("SELECT id, aws_account_id, resource_id FROM alerts"):
                self._pending = []
            else:
                raise AssertionError(f"unexpected query with no candidates: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/multivariate_anomaly.py")
    assert mod.detect_multivariate_anomalies() == 0


# -- Phase 2 AI/ML audit (2026-10-02) ---------------------------------------

def test_single_bucket_blip_is_not_flagged():
    rows = _history_with_joint_anomaly_at_end(n=80, buckets=1)
    inserted, updated, resolved = _install_db_stub(rows)
    mod = load_module("app/collector/multivariate_anomaly.py")
    assert mod.detect_multivariate_anomalies() == 0
    assert inserted == []


def test_two_bucket_blip_is_not_flagged_but_three_is():
    rows2 = _history_with_joint_anomaly_at_end(n=80, buckets=2)
    inserted, _, _ = _install_db_stub(rows2)
    mod = load_module("app/collector/multivariate_anomaly.py")
    assert mod.detect_multivariate_anomalies() == 0 and inserted == []

    rows3 = _history_with_joint_anomaly_at_end(n=80, buckets=3)
    inserted, _, _ = _install_db_stub(rows3)
    mod = load_module("app/collector/multivariate_anomaly.py")
    assert mod.detect_multivariate_anomalies() == 1 and len(inserted) == 1


def test_normal_daily_cycle_is_not_flagged():
    """14 days of a strong daily CPU/network cycle, last 3 buckets at a normal peak hour."""
    import math, random
    random.seed(7)
    rows = []
    start = datetime(2026, 9, 1, 0, 0, 0)
    for i in range(14 * 288 - 3):
        ts = start + timedelta(minutes=5 * i)
        phase = math.sin(2 * math.pi * (ts.hour + ts.minute / 60) / 24)
        rows.append({"metric_name": "CPUUtilization", "metric_timestamp": ts, "metric_value": 40 + 25 * phase + random.uniform(-2, 2)})
        rows.append({"metric_name": "NetworkIn", "metric_timestamp": ts, "metric_value": 1000 + 600 * phase + random.uniform(-40, 40)})
        rows.append({"metric_name": "DiskReadOps", "metric_timestamp": ts, "metric_value": 200 + 100 * phase + random.uniform(-8, 8)})
    inserted, _, _ = _install_db_stub(rows)
    mod = load_module("app/collector/multivariate_anomaly.py")
    assert mod.detect_multivariate_anomalies() == 0
    assert inserted == []


def test_deviation_report_names_the_metrics_that_moved():
    import pandas as pd
    install_stub("app.db", get_connection=lambda: None)
    mod = load_module("app/collector/multivariate_anomaly.py")
    idx = pd.date_range("2026-09-01", periods=60, freq="5min")
    hist = pd.DataFrame({"cpu": [40.0 + (i % 5) for i in range(60)], "net": [1000.0 + (i % 7) * 10 for i in range(60)],
                         "errs": [0.0] * 60}, index=idx)
    recent = pd.DataFrame({"cpu": [90.0] * 3, "net": [1020.0] * 3, "errs": [0.0] * 3},
                          index=pd.date_range("2026-09-01 05:00", periods=3, freq="5min"))
    dev = mod._deviation_report(hist, recent, ["cpu", "net", "errs"])
    assert dev[0][0] == "cpu" and dev[0][1] > 3.5
    assert dict(dev)["errs"] == 0.0           # constant-zero metric that stayed zero is not "moved"
    assert "cpu +" in mod._format_why(dev)


def test_outlier_where_fewer_than_two_metrics_moved_is_dismissed(monkeypatch):
    """Only one metric shifts materially: that is baseline.py's job, not a multivariate anomaly."""
    rows = _stable_history_rows(n=80)
    last_ts = rows[-1]["metric_timestamp"]
    for i in range(1, 4):
        ts = last_ts + timedelta(minutes=5 * i)
        rows.append({"metric_name": "CPUUtilization", "metric_timestamp": ts, "metric_value": 95})
        rows.append({"metric_name": "NetworkIn", "metric_timestamp": ts, "metric_value": 1000})
        rows.append({"metric_name": "DiskReadOps", "metric_timestamp": ts, "metric_value": 200})
    inserted, _, _ = _install_db_stub(rows)
    mod = load_module("app/collector/multivariate_anomaly.py")
    assert mod.detect_multivariate_anomalies() == 0 and inserted == []


def test_time_features_do_not_count_as_metrics_or_appear_in_the_explanation():
    import pandas as pd
    install_stub("app.db", get_connection=lambda: None)
    mod = load_module("app/collector/multivariate_anomaly.py")
    idx = pd.date_range("2026-09-01", periods=10, freq="5min")
    base = pd.DataFrame({"a": range(10), "b": range(10), "c": range(10)}, index=idx)
    feats = mod._add_time_features(base)
    assert list(feats.columns) == ["a", "b", "c", "_hod_sin", "_hod_cos"]
    assert list(base.columns) == ["a", "b", "c"]            # input not mutated


def test_shadow_report_summarises_counts_duration_and_corroboration():
    from datetime import datetime
    mod = load_module("scripts/anomaly_shadow_report.py")
    t = datetime(2026, 10, 1, 12, 0, 0)
    rows = [
        {"id": 1, "aws_account_id": 7, "resource_id": "i-a", "status": "resolved", "triggered_at": t,
         "resolved_at": t + timedelta(minutes=30), "corroborated": 1},
        {"id": 2, "aws_account_id": 7, "resource_id": "i-a", "status": "resolved", "triggered_at": t + timedelta(days=1),
         "resolved_at": t + timedelta(days=1, minutes=90), "corroborated": 0},
        {"id": 3, "aws_account_id": 7, "resource_id": "i-b", "status": "active", "triggered_at": t + timedelta(days=1),
         "resolved_at": None, "corroborated": 0},
    ]

    class C:
        def execute(self, sql, params=None):
            assert sql.lstrip().upper().startswith("SELECT") and params == (7,)

        def fetchall(self):
            return rows

    rep = mod.build_report(C(), 7)
    assert rep["total"] == 3 and rep["still_active"] == 1 and rep["distinct_resources"] == 2
    assert rep["corroborated"] == 1 and rep["corroborated_pct"] == 33.3
    assert rep["median_duration_min"] == 60.0
    assert rep["top_resources"][0] == ("i-a", 2)
    assert "total fired" in mod.format_report(rep)


# -- cost controls (2026-10-03) -----------------------------------------------

def test_default_runs_every_cycle(monkeypatch):
    monkeypatch.delenv("ANOMALY_ENABLED", raising=False)
    monkeypatch.delenv("ANOMALY_MIN_INTERVAL_MINUTES", raising=False)
    rows = _history_with_joint_anomaly_at_end(n=80, buckets=3)
    _install_db_stub(rows)
    mod = load_module("app/collector/multivariate_anomaly.py")
    assert mod.detect_multivariate_anomalies() == 1
    assert mod.detect_multivariate_anomalies() == 1      # no interval configured: runs again


def test_min_interval_skips_a_second_run_without_touching_the_database(monkeypatch):
    monkeypatch.setenv("ANOMALY_MIN_INTERVAL_MINUTES", "60")
    rows = _history_with_joint_anomaly_at_end(n=80, buckets=3)
    _install_db_stub(rows)
    mod = load_module("app/collector/multivariate_anomaly.py")
    assert mod.detect_multivariate_anomalies() == 1
    calls = []
    install_stub("app.db", get_connection=lambda: calls.append(1))
    mod.get_connection = lambda: calls.append(1)
    assert mod.detect_multivariate_anomalies() == 0      # 15-min tick inside the 60-min window
    assert calls == []


def test_min_interval_runs_again_once_the_window_has_passed(monkeypatch):
    monkeypatch.setenv("ANOMALY_MIN_INTERVAL_MINUTES", "60")
    rows = _history_with_joint_anomaly_at_end(n=80, buckets=3)
    _install_db_stub(rows)
    mod = load_module("app/collector/multivariate_anomaly.py")
    assert mod.detect_multivariate_anomalies() == 1
    mod._last_run_monotonic -= 61 * 60                   # pretend an hour passed
    assert mod.detect_multivariate_anomalies() == 1


def test_a_cycle_a_few_seconds_early_is_not_skipped_for_a_whole_interval(monkeypatch):
    monkeypatch.setenv("ANOMALY_MIN_INTERVAL_MINUTES", "60")
    install_stub("app.db", get_connection=lambda: None)
    mod = load_module("app/collector/multivariate_anomaly.py")
    mod._last_run_monotonic = mod.time.monotonic() - (60 * 60 - 20)    # 20 s short of an hour
    assert mod._too_soon() is False


def test_disabled_skips_scoring_and_resolves_open_hidden_alerts(monkeypatch):
    monkeypatch.setenv("ANOMALY_ENABLED", "false")
    inserted, updated, resolved = _install_db_stub(
        _history_with_joint_anomaly_at_end(n=80, buckets=3),
        active_anomaly_alerts=[{"id": 11, "aws_account_id": 7, "resource_id": "i-1"}])
    mod = load_module("app/collector/multivariate_anomaly.py")
    assert mod.detect_multivariate_anomalies() == 0
    assert inserted == [] and updated == []              # nothing was scored
    assert len(resolved) == 1                            # the open hidden alert was closed once
