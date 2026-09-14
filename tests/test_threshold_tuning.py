# tests/test_threshold_tuning.py
"""
Coverage for app/collector/threshold_tuning.py -- the fix for
chronically-miscalibrated static thresholds (2026-09-14), and its
same-day revision after a real production diagnosis showed the
original majority-only trigger never fired for the exact case it was
built for: only 2 of ~19 EC2 instances in the account ran genuinely
high NetIn/NetOut traffic, so the 60% majority bar was never met and
those two alerts stayed active for over a week. This suite now covers
both trigger paths: the original majority path, and the new
chronic-single-resource path that doesn't need a majority of peers to
also be loud.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn


def _install_stub(threshold_rows, baseline_rows, chronic_alert_resource_ids=None):
    """chronic_alert_resource_ids: resource_ids for which
    _has_chronic_active_alert() should report a qualifying (6h+) active
    alert exists. Defaults to none -- no resource has one unless a test
    explicitly opts in."""
    chronic_alert_resource_ids = set(chronic_alert_resource_ids or [])
    updates = []

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT t.id, t.aws_account_id"):
                self._pending = threshold_rows
            elif normalized.startswith("SELECT b.resource_id, AVG"):
                self._pending = baseline_rows
            elif normalized.startswith("SELECT id FROM alerts"):
                # _has_chronic_active_alert()'s own query -- params[0] is
                # the resource_id being checked.
                self._pending = [{"id": 1}] if params[0] in chronic_alert_resource_ids else []
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


# ── Majority path ────────────────────────────────────────────────────

def test_majority_breach_switches_threshold_to_dynamic():
    """2 of 2 confidently-baselined resources typically run well past
    the critical value -- majority path should switch."""
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
    assert updates[0][0] == 501


def test_occasional_outlier_without_chronic_alert_does_not_switch():
    """1 of 3 resources runs hot (below CHRONIC_BREACH_FRACTION for the
    majority path) and has NO qualifying chronic active alert either --
    neither path should fire."""
    threshold = _threshold_row()
    baselines = [
        {"resource_id": "i-loud", "typical_value": 2_000_000, "total_samples": 40},
        {"resource_id": "i-normal-1", "typical_value": 400_000, "total_samples": 40},
        {"resource_id": "i-normal-2", "typical_value": 350_000, "total_samples": 40},
    ]
    updates = _install_stub([threshold], baselines, chronic_alert_resource_ids=[])
    mod = load_module("app/collector/threshold_tuning.py")

    switched = mod.auto_tune_static_thresholds()

    assert switched == 0
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


# ── Chronic-single-resource path (the actual production fix) ────────

def test_single_chronic_resource_switches_without_a_majority():
    """THE REAL PRODUCTION CASE: only 1 of many resources runs hot
    (nowhere near the 60% majority bar), but it has been continuously
    alerting for 6+ hours with a confident baseline -- must switch on
    its own, without needing any peer to also be loud."""
    threshold = _threshold_row()
    baselines = [
        {"resource_id": "i-aurionpro-dev-finops", "typical_value": 2_300_000, "total_samples": 40},
        {"resource_id": "i-normal-1", "typical_value": 400_000, "total_samples": 40},
        {"resource_id": "i-normal-2", "typical_value": 350_000, "total_samples": 40},
        {"resource_id": "i-normal-3", "typical_value": 500_000, "total_samples": 40},
        {"resource_id": "i-normal-4", "typical_value": 450_000, "total_samples": 40},
    ]
    updates = _install_stub([threshold], baselines,
                             chronic_alert_resource_ids=["i-aurionpro-dev-finops"])
    mod = load_module("app/collector/threshold_tuning.py")

    switched = mod.auto_tune_static_thresholds()

    assert switched == 1
    assert len(updates) == 1
    assert updates[0][0] == 501


def test_single_loud_resource_without_chronic_active_alert_does_not_switch():
    """A resource can have a confidently-high BASELINE (its typical
    traffic is high) without currently having an alert stuck active on
    it -- e.g. if the static threshold sits just above its normal range
    most of the time. That alone should NOT trigger the single-resource
    path; only an ACTUAL long-running active alert does."""
    threshold = _threshold_row()
    baselines = [
        {"resource_id": "i-loud-but-not-alerting", "typical_value": 2_000_000, "total_samples": 40},
    ]
    updates = _install_stub([threshold], baselines, chronic_alert_resource_ids=[])
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 0
    assert updates == []


def test_fresh_active_alert_not_yet_chronic_does_not_switch():
    """An active alert that hasn't been breaching long enough yet
    (< CHRONIC_ALERT_AGE_HOURS) should not trigger the single-resource
    path -- _has_chronic_active_alert() itself only returns True for
    alerts old enough to query for, so a fresh one (not in
    chronic_alert_resource_ids) correctly does not switch."""
    threshold = _threshold_row()
    baselines = [{"resource_id": "i-just-started-alerting", "typical_value": 2_000_000, "total_samples": 40}]
    updates = _install_stub([threshold], baselines, chronic_alert_resource_ids=[])
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 0
    assert updates == []


def test_single_resource_no_confident_baseline_makes_no_decision():
    """A resource with too few samples for a confident baseline is
    filtered out before ever reaching either trigger path (HAVING
    total_samples >= MIN_CONFIDENT_SAMPLES in the SQL itself) -- this
    test simulates that filtering having already excluded everyone."""
    threshold = _threshold_row()
    updates = _install_stub([threshold], baseline_rows=[])
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 0
    assert updates == []
