# tests/test_threshold_tuning.py
"""
Coverage for app/collector/threshold_tuning.py -- the fix for
chronically-miscalibrated static thresholds (2026-09-14), the real
production case being: EC2 NetIn/NetOut alerting repeatedly at a
threshold of 1,000,000 while normal traffic runs 1.3M-4.3M.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn


def _install_stub(threshold_rows, baseline_rows_by_threshold_id):
    updates = []

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT t.id, t.aws_account_id"):
                self._pending = threshold_rows
            elif normalized.startswith("SELECT b.resource_id, AVG"):
                # params[0]=account_id is not used to key our fixture --
                # tests here use one threshold row at a time, so map by
                # whatever fixture was configured for "the" threshold.
                self._pending = baseline_rows_by_threshold_id
            elif normalized.startswith("UPDATE thresholds SET use_dynamic"):
                updates.append(params)
                self._pending = []
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    install_stub("app.audit", write_audit=lambda **kwargs: None)
    install_stub("app.collector.op_log", log_event=lambda *a, **k: None)
    return updates


def _threshold_row(**overrides):
    row = {
        "id": 501, "aws_account_id": 7, "resource_type": "ec2_instance",
        "metric_id": 9, "warning_value": 800000, "critical_value": 1000000,
        "comparison": ">", "metric_name": "NetIn",
    }
    row.update(overrides)
    return row


def test_chronic_breach_switches_threshold_to_dynamic():
    """The real production case: 2 of 2 confidently-baselined resources
    typically run well past the critical value -- should auto-switch."""
    threshold = _threshold_row()
    baselines = [
        {"resource_id": "i-dev-finops", "typical_value": 2_300_000, "total_samples": 40},
        {"resource_id": "i-finops", "typical_value": 3_100_000, "total_samples": 55},
    ]
    updates = _install_stub([threshold], baselines)
    mod = load_module("app/collector/threshold_tuning.py")

    switched = mod.auto_tune_static_thresholds()

    assert switched == 1
    assert len(updates) == 1
    assert updates[0][0] == 501  # threshold id passed to UPDATE ... WHERE id = %s


def test_occasional_outlier_does_not_switch_threshold():
    """Only 1 of 3 resources runs hot -- below CHRONIC_BREACH_FRACTION,
    should NOT switch (dynamic mode would have caught that one resource
    fine on its own merits without touching the other two)."""
    threshold = _threshold_row()
    baselines = [
        {"resource_id": "i-loud", "typical_value": 2_000_000, "total_samples": 40},
        {"resource_id": "i-normal-1", "typical_value": 400_000, "total_samples": 40},
        {"resource_id": "i-normal-2", "typical_value": 350_000, "total_samples": 40},
    ]
    updates = _install_stub([threshold], baselines)
    mod = load_module("app/collector/threshold_tuning.py")

    switched = mod.auto_tune_static_thresholds()

    assert switched == 0
    assert updates == []


def test_too_few_confident_resources_makes_no_decision():
    threshold = _threshold_row()
    baselines = [{"resource_id": "i-only-one", "typical_value": 5_000_000, "total_samples": 40}]
    updates = _install_stub([threshold], baselines)
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 0
    assert updates == []


def test_low_direction_comparison():
    """"<" metrics (e.g. free-disk-percent) breach when typical value is
    BELOW the critical line, not above."""
    threshold = _threshold_row(metric_name="FreeDiskPercent", comparison="<", critical_value=10.0)
    baselines = [
        {"resource_id": "vol-1", "typical_value": 3.0, "total_samples": 40},
        {"resource_id": "vol-2", "typical_value": 4.5, "total_samples": 40},
    ]
    updates = _install_stub([threshold], baselines)
    mod = load_module("app/collector/threshold_tuning.py")

    switched = mod.auto_tune_static_thresholds()
    assert switched == 1


def test_no_static_thresholds_returns_zero():
    updates = _install_stub([], [])
    mod = load_module("app/collector/threshold_tuning.py")
    assert mod.auto_tune_static_thresholds() == 0
    assert updates == []
