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


def _history_with_joint_anomaly_at_end(n=80):
    """Same stable history, but the LAST reading jointly shifts all
    three metrics far outside anything seen before -- individually each
    value might still look plausible-ish, but the combination should
    not."""
    rows = _stable_history_rows(n=n)
    last_ts = rows[-1]["metric_timestamp"] + timedelta(minutes=5)
    rows.append({"metric_name": "CPUUtilization", "metric_timestamp": last_ts, "metric_value": 95})
    rows.append({"metric_name": "NetworkIn", "metric_timestamp": last_ts, "metric_value": 9000})
    rows.append({"metric_name": "DiskReadOps", "metric_timestamp": last_ts, "metric_value": 2000})
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
            elif "metric_name = 'multivariate_anomaly' AND status = 'active'" in normalized and "SELECT id FROM alerts" in normalized:
                self._pending = [existing_alert] if existing_alert else []
            elif normalized.startswith("SELECT resource_type, tags FROM resources"):
                self._pending = [{"resource_type": "ec2_instance", "tags": '{"environment":"prod"}'}]
            elif normalized.startswith("INSERT INTO alerts"):
                inserted.append(params)
                self._pending = []
            elif normalized.startswith("UPDATE alerts SET current_value"):
                updated.append(params)
                self._pending = []
            elif normalized.startswith("SELECT id, resource_id FROM alerts"):
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
    # (resource_id, environment, group_key, score)
    assert params[0] == "i-anomalous-1"
    assert params[1] == "prod"
    assert params[2] == "7:ec2_instance:multivariate_anomaly"
    assert isinstance(params[3], float)


def test_joint_anomaly_updates_existing_alert_instead_of_duplicating():
    rows = _history_with_joint_anomaly_at_end(n=80)
    inserted, updated, resolved = _install_db_stub(
        rows, resource_id="i-anomalous-1", existing_alert={"id": 999},
        active_anomaly_alerts=[{"id": 999, "resource_id": "i-anomalous-1"}],
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
        active_anomaly_alerts=[{"id": 555, "resource_id": "i-recovered-1"}],
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
            elif normalized.startswith("SELECT id, resource_id FROM alerts"):
                self._pending = []
            else:
                raise AssertionError(f"unexpected query with no candidates: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/multivariate_anomaly.py")
    assert mod.detect_multivariate_anomalies() == 0
