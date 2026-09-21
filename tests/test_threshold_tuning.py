# tests/test_threshold_tuning.py
"""
Coverage for app/collector/threshold_tuning.py across all four trigger
paths, each added after a real production diagnosis:
  1. Majority path (original).
  2. Chronic-mean path: a single resource's own average is confidently
     past the alerting line, with a real 6h+ active alert -- added
     after Aurionpro-Dev-Finops/Aurionpro-Finops's alerts stayed active
     for a week+ despite majority-path-only logic.
  3. Chronic-noise path: a resource whose AVERAGE is
     healthy but whose normal VARIABILITY (mean +/- k*stddev) already
     crosses the alerting line, with a real 6h+ active alert -- added
     after Aurionpro-Finops's NetworkOut (typical 1.78M against a 5M
     line, confidently baselined) stayed active for 30+ hours despite
     path 2 correctly not firing for a mean that low.
  4. Revision 3 (this revision): every comparison above now checks
     warning_value, not critical_value -- added after Aurionpro-Finops's
     NetworkOut alert stayed open indefinitely at WARNING severity
     (critical had been raised to 5M, but warning was left at its
     original 1M) with none of paths 1-3 ever able to see it, since all
     three only ever compared against critical_value. See
     test_chronic_mean_breaches_warning_but_not_critical_switches below
     for the exact regression case.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app.alert_rules  # noqa: F401,E402  (real `app` package before conftest's install_stub)
import app.threshold_defaults  # noqa: F401,E402
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn


def _install_stub(threshold_rows, baseline_rows, chronic_alert_resource_ids=None,
                   false_positive_marked_resource_ids=None):
    chronic_alert_resource_ids = set(chronic_alert_resource_ids or [])
    false_positive_marked_resource_ids = set(false_positive_marked_resource_ids or [])
    updates = []

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT t.id, t.aws_account_id"):
                self._pending = threshold_rows
            elif normalized.startswith("SELECT b.resource_id, AVG"):
                self._pending = baseline_rows
            elif normalized.startswith("SELECT COUNT(*) AS cnt FROM alerts"):
                # _false_positive_mark_count()'s query -- defaults to 0
                # marks (below MIN_FALSE_POSITIVE_MARKS) unless a test
                # explicitly opts a resource in.
                count = 2 if params[1] in false_positive_marked_resource_ids else 0
                self._pending = [{"cnt": count}]
            elif normalized.startswith("SELECT id FROM alerts"):
                self._pending = [{"id": 1}] if params[1] in chronic_alert_resource_ids else []
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
        "id": 501, "aws_account_id": 7, "resource_type": "ec2", "metric_id": 9,
        "warning_value": 800000, "critical_value": 1000000,
        "comparison": ">", "dynamic_k": None, "metric_name": "NetworkIn",
        "account_name": "U4RAD",
    }
    row.update(overrides)
    return row


def _baseline(resource_id, typical_value, stddev=0, samples=40):
    return {"resource_id": resource_id, "typical_value": typical_value,
            "typical_stddev": stddev, "total_samples": samples}


# ── Majority path ────────────────────────────────────────────────────

def test_majority_breach_switches_threshold_to_dynamic():
    threshold = _threshold_row()
    baselines = [_baseline("i-dev-finops", 2_300_000), _baseline("i-finops", 3_100_000)]
    updates = _install_stub([threshold], baselines)
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 1
    assert updates[0][0] == 501


def test_low_direction_comparison():
    # For "<" metrics (bad when LOW), warning is the closer-to-normal
    # line, i.e. numerically HIGHER than critical (10.0) -- 15.0 here,
    # matching _dynamic_bounds' own documented convention.
    threshold = _threshold_row(metric_name="FreeDiskPercent", comparison="<",
                                warning_value=15.0, critical_value=10.0)
    baselines = [_baseline("vol-1", 3.0), _baseline("vol-2", 4.5)]
    updates = _install_stub([threshold], baselines)
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 1


def test_manually_confirmed_false_positives_switch_without_chronic_wait():
    """A resource with NO chronic active alert and a mean that isn't
    even breaching -- but 2+ of its past alerts on this metric were
    manually marked false positive -- must switch via the
    manually_confirmed path, bypassing the 6h wait entirely."""
    threshold = _threshold_row()
    # typical_value 400_000 is well UNDER the 1_000_000 critical line --
    # would not qualify for ANY of the other three paths on its own.
    baselines = [_baseline("i-confirmed-noisy", 400_000, stddev=50_000)]
    updates = _install_stub([threshold], baselines,
                             chronic_alert_resource_ids=[],
                             false_positive_marked_resource_ids=["i-confirmed-noisy"])
    mod = load_module("app/collector/threshold_tuning.py")

    switched = mod.auto_tune_static_thresholds()

    assert switched == 1
    assert updates[0][0] == 501


def test_single_false_positive_mark_is_not_enough():
    """Below MIN_FALSE_POSITIVE_MARKS (2) -- a single mark alone must
    not be sufficient."""
    threshold = _threshold_row()
    baselines = [_baseline("i-one-mark", 400_000, stddev=50_000)]
    # _install_stub's fake always returns count=2 for any resource in
    # the marked set -- to test "1 mark," stub the count query directly.
    updates = []

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT t.id, t.aws_account_id"):
                self._pending = [threshold]
            elif normalized.startswith("SELECT b.resource_id, AVG"):
                self._pending = baselines
            elif normalized.startswith("SELECT COUNT(*) AS cnt FROM alerts"):
                self._pending = [{"cnt": 1}]  # only 1 mark, below the bar of 2
            elif normalized.startswith("SELECT id FROM alerts"):
                self._pending = []
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
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 0
    assert updates == []


def test_no_static_thresholds_returns_zero():
    updates = _install_stub([], [])
    mod = load_module("app/collector/threshold_tuning.py")
    assert mod.auto_tune_static_thresholds() == 0
    assert updates == []


# ── Chronic-mean path (Revision 1) ──────────────────────────────────

def test_single_chronic_mean_resource_switches_without_a_majority():
    """THE FIRST PRODUCTION CASE: 1 of 5 resources runs hot on average,
    nowhere near a 60% majority, but has a real 6h+ active alert."""
    threshold = _threshold_row()
    baselines = [
        _baseline("i-aurionpro-dev-finops", 2_300_000),
        _baseline("i-normal-1", 400_000), _baseline("i-normal-2", 350_000),
        _baseline("i-normal-3", 500_000), _baseline("i-normal-4", 450_000),
    ]
    updates = _install_stub([threshold], baselines, chronic_alert_resource_ids=["i-aurionpro-dev-finops"])
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 1
    assert updates[0][0] == 501


def test_high_mean_without_chronic_active_alert_does_not_switch():
    threshold = _threshold_row()
    baselines = [_baseline("i-loud-but-not-alerting", 2_000_000)]
    updates = _install_stub([threshold], baselines, chronic_alert_resource_ids=[])
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 0
    assert updates == []


# ── Warning-only chronic breach (Revision 3, THIS fix) ──────────────

def test_chronic_mean_breaches_warning_but_not_critical_switches():
    """THE THIRD PRODUCTION CASE, exact regression: Aurionpro-Finops's
    NetworkOut had critical_value raised to 5M (to quiet CRITICAL
    noise) but warning_value left at its original 1M. Mean is 1.78M --
    comfortably UNDER the 5M critical line (paths 1-3 pre-Revision-3
    all correctly saw nothing wrong here), but well OVER the 1M warning
    line, with a real 6h+ active alert. Must switch via chronic_mean,
    now that it compares against warning_value."""
    threshold = _threshold_row(metric_name="NetworkOut", warning_value=1_000_000, critical_value=5_000_000)
    baselines = [_baseline("i-052ad4c2b1578740a", 1_783_624, stddev=516_351, samples=1625)]
    updates = _install_stub([threshold], baselines, chronic_alert_resource_ids=["i-052ad4c2b1578740a"])
    mod = load_module("app/collector/threshold_tuning.py")

    switched = mod.auto_tune_static_thresholds()

    assert switched == 1
    assert updates[0][0] == 501


def test_majority_breach_of_warning_only_still_switches():
    """A majority of resources whose mean is past WARNING but under
    CRITICAL must still switch -- majority path also broadened in
    Revision 3, not just chronic-mean."""
    threshold = _threshold_row(warning_value=800_000, critical_value=5_000_000)
    baselines = [_baseline("i-a", 1_200_000), _baseline("i-b", 1_500_000)]
    updates = _install_stub([threshold], baselines)
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 1
    assert updates[0][0] == 501


def test_mean_under_both_warning_and_critical_does_not_switch():
    """Sanity check: a resource genuinely healthy relative to BOTH
    lines, with no chronic active alert, must not switch."""
    threshold = _threshold_row(warning_value=800_000, critical_value=5_000_000)
    baselines = [_baseline("i-genuinely-healthy", 400_000)]
    updates = _install_stub([threshold], baselines, chronic_alert_resource_ids=[])
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 0
    assert updates == []


# ── Chronic-noise path (Revision 2) ─────────────────────────────────

def test_flapping_resource_switches_via_noise_path():
    """THE SECOND PRODUCTION CASE: a resource whose typical value
    (1.78M) is well under a 5M warning line (would NOT match the
    chronic-mean path at all), but stddev is large enough that
    mean + 3*stddev crosses 5M, AND it has a real 6h+ active alert.
    Must switch via the noise path. (warning_value set well above the
    mean here specifically so mean_breaching stays False and this
    exercises the noise path, not chronic-mean -- see the Revision 3
    regression test above for the mean-breaches-warning case.)"""
    threshold = _threshold_row(metric_name="NetworkOut", warning_value=5_000_000, critical_value=8_000_000)
    # mean 1.78M, stddev 1.2M -> mean + 3*stddev = 1.78M + 3.6M = 5.38M > 5M warning
    baselines = [_baseline("i-052ad4c2b1578740a", 1_780_000, stddev=1_200_000, samples=1553)]
    updates = _install_stub([threshold], baselines, chronic_alert_resource_ids=["i-052ad4c2b1578740a"])
    mod = load_module("app/collector/threshold_tuning.py")

    switched = mod.auto_tune_static_thresholds()

    assert switched == 1
    assert updates[0][0] == 501


def test_stable_low_mean_resource_with_low_stddev_does_not_switch():
    """A resource whose mean AND normal variability both stay well
    under the warning line must not switch on any path -- there's
    genuinely nothing to fix here."""
    threshold = _threshold_row(metric_name="NetworkOut", warning_value=5_000_000, critical_value=8_000_000)
    # mean 1.78M, small stddev -> mean + 3*stddev = 1.78M + 300k = 2.08M, nowhere near 5M
    baselines = [_baseline("i-genuinely-fine", 1_780_000, stddev=100_000, samples=1553)]
    updates = _install_stub([threshold], baselines, chronic_alert_resource_ids=["i-genuinely-fine"])
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 0
    assert updates == []


def test_noisy_resource_without_chronic_active_alert_does_not_switch():
    """High variability alone, without an ACTUAL sustained active
    alert, must not switch -- matches the same evidence bar as the
    chronic-mean path (baseline alone is never sufficient)."""
    threshold = _threshold_row(metric_name="NetworkOut", warning_value=5_000_000, critical_value=8_000_000)
    baselines = [_baseline("i-noisy-but-not-alerting", 1_780_000, stddev=1_200_000, samples=1553)]
    updates = _install_stub([threshold], baselines, chronic_alert_resource_ids=[])
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 0
    assert updates == []


def test_noise_path_respects_custom_dynamic_k():
    """A threshold with a custom (tighter) dynamic_k should use that k,
    not the NOISE_K default, when testing whether variability crosses
    the warning line."""
    threshold = _threshold_row(metric_name="NetworkOut", warning_value=5_000_000, critical_value=8_000_000, dynamic_k=1.0)
    # mean + 1.0*stddev = 1.78M + 1.2M = 2.98M -- does NOT cross 5M at k=1.0,
    # even though it clearly would at the default k=3.0.
    baselines = [_baseline("i-tight-k", 1_780_000, stddev=1_200_000, samples=1553)]
    updates = _install_stub([threshold], baselines, chronic_alert_resource_ids=["i-tight-k"])
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.auto_tune_static_thresholds() == 0
    assert updates == []


# ── count_likely_flapping_alerts (bulk fleet-summary query) ─────────

def test_count_likely_flapping_alerts_returns_query_result():
    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT COUNT(*) AS flapping_count"):
                self._pending = [{"flapping_count": 3}]
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.count_likely_flapping_alerts() == 3


def test_count_likely_flapping_alerts_zero_accounts_skips_query():
    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            raise AssertionError("should not query when aws_account_ids is an empty set")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/threshold_tuning.py")

    assert mod.count_likely_flapping_alerts(aws_account_ids=set()) == 0


def test_count_likely_flapping_alerts_scopes_to_accounts():
    captured = {}

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split())
            captured["params"] = params
            self._pending = [{"flapping_count": 1}]

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/threshold_tuning.py")

    result = mod.count_likely_flapping_alerts(aws_account_ids=[7, 9])

    assert result == 1
    assert "aws_account_id IN (%s,%s)" in captured["sql"]
    assert 7 in captured["params"] and 9 in captured["params"]
