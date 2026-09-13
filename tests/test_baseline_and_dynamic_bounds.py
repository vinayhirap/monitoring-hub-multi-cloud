# tests/test_baseline_and_dynamic_bounds.py
"""
Regression coverage for the 2026-09-14 ML/dynamic-threshold hardening:

  1. app/collector/baseline.py: recompute_baselines() must upsert every
     bucket the (sigma-clipped) SELECT returns, using the POST-clip
     sample_count (not the raw count) as what gets written to
     metric_baseline.sample_count -- that's the value alert_evaluator.py
     later reads for confidence blending, so writing the wrong count
     would silently break blending downstream.

  2. app/collector/alert_evaluator.py::_dynamic_bounds(): confidence
     blending between the dynamic (mean +/- k*stddev) band and a static
     threshold, gated on metric_baseline.sample_count vs
     CONFIDENT_SAMPLES. Covers: full-confidence (pure dynamic),
     low-confidence (blended), cold-start (no bucket row at all -- still
     None), and flat-line (stddev 0 -- still None), for both ">" and
     "<" comparison directions.

Without a live MySQL/MariaDB instance, these tests exercise the actual
Python-level logic in both modules (loop/upsert shape in baseline.py,
the blend arithmetic in alert_evaluator.py) via conftest's
load_module()/FakeCursor -- the SQL's own sigma-clip arithmetic runs
inside MySQL itself and is not re-executed by these fakes (same
limitation every other test in this suite already has for its own
SQL -- see conftest.py's own docstring on why this repo tests this way).
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub, FakeCursor, FakeConn, contains


# ── 1. baseline.py: recompute_baselines() upsert shape ─────────────

def test_recompute_baselines_upserts_every_bucket_with_postclip_count():
    select_result = [
        # Bucket A: no clipping happened (sample_count == raw_sample_count)
        {"resource_id": "i-aaa", "metric_name": "CPUUtilization",
         "hour_of_day": 9, "day_of_week": 1,
         "mean_value": 42.0, "stddev_value": 5.0,
         "sample_count": 12, "raw_sample_count": 12},
        # Bucket B: one outlier reading was clipped out by the SQL's
        # pass-2 filter -- sample_count < raw_sample_count.
        {"resource_id": "i-bbb", "metric_name": "CPUUtilization",
         "hour_of_day": 9, "day_of_week": 1,
         "mean_value": 30.0, "stddev_value": 4.0,
         "sample_count": 11, "raw_sample_count": 12},
    ]

    inserts = []

    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT"):
                self._pending = select_result
            elif normalized.startswith("INSERT INTO metric_baseline"):
                inserts.append(params)
                self._pending = []
            else:
                raise AssertionError(f"unexpected query: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/baseline.py")

    written = mod.recompute_baselines()

    assert written == 2
    assert len(inserts) == 2
    # sample_count written must be the POST-clip value (index 6 in the
    # positional INSERT params: resource_id, metric_name, hour_of_day,
    # day_of_week, mean_value, stddev_value, sample_count).
    written_sample_counts = {p[0]: p[6] for p in inserts}
    assert written_sample_counts["i-aaa"] == 12
    assert written_sample_counts["i-bbb"] == 11  # NOT 12 (the raw count)


def test_recompute_baselines_writes_nothing_when_no_bucket_clears_min_samples():
    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT"):
                self._pending = []  # HAVING clause filtered everything out
            else:
                raise AssertionError(f"unexpected query when no buckets: {normalized!r}")

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    mod = load_module("app/collector/baseline.py")

    assert mod.recompute_baselines() == 0


# ── 2. alert_evaluator.py: _dynamic_bounds confidence blending ─────

def _load_alert_evaluator():
    install_stub("app.db", get_connection=lambda: FakeConn([]))
    install_stub("app.ws.publisher", publish_alert=lambda *a, **k: None,
                 publish_alert_resolved=lambda *a, **k: None)
    return load_module("app/collector/alert_evaluator.py")


def _baseline_cursor(mean, stddev, sample_count):
    row = [{"mean_value": mean, "stddev_value": stddev, "sample_count": sample_count}]
    return FakeCursor([(contains("FROM metric_baseline"), row)])


def test_dynamic_bounds_cold_start_returns_none():
    mod = _load_alert_evaluator()
    cursor = FakeCursor([(contains("FROM metric_baseline"), [])])  # no bucket row
    assert mod._dynamic_bounds(cursor, "i-aaa", "CPUUtilization", ">", 3.0) is None


def test_dynamic_bounds_flatline_returns_none():
    mod = _load_alert_evaluator()
    cursor = _baseline_cursor(mean=50.0, stddev=0, sample_count=100)
    assert mod._dynamic_bounds(cursor, "i-aaa", "CPUUtilization", ">", 3.0) is None


def test_dynamic_bounds_full_confidence_is_pure_dynamic():
    mod = _load_alert_evaluator()
    cursor = _baseline_cursor(mean=40.0, stddev=5.0, sample_count=mod.CONFIDENT_SAMPLES)
    warning, critical = mod._dynamic_bounds(
        cursor, "i-aaa", "CPUUtilization", ">", 3.0,
        static_warning=70.0, static_critical=90.0,
    )
    assert critical == 40.0 + 3.0 * 5.0
    assert warning == 40.0 + (3.0 * 0.66) * 5.0


def test_dynamic_bounds_low_confidence_blends_toward_static():
    mod = _load_alert_evaluator()
    half_confidence = mod.CONFIDENT_SAMPLES // 2
    cursor = _baseline_cursor(mean=40.0, stddev=5.0, sample_count=half_confidence)

    dyn_critical = 40.0 + 3.0 * 5.0     # 55.0
    static_critical = 90.0
    weight = half_confidence / mod.CONFIDENT_SAMPLES  # 0.5

    warning, critical = mod._dynamic_bounds(
        cursor, "i-aaa", "CPUUtilization", ">", 3.0,
        static_warning=70.0, static_critical=static_critical,
    )
    expected_critical = weight * dyn_critical + (1 - weight) * static_critical
    assert abs(critical - expected_critical) < 1e-9
    # Blended value must sit strictly between the pure-dynamic and
    # pure-static critical values, not equal either extreme.
    assert min(dyn_critical, static_critical) < critical < max(dyn_critical, static_critical)


def test_dynamic_bounds_without_static_args_falls_back_to_pure_dynamic():
    """Backward compatibility: a caller that doesn't pass static_warning/
    static_critical (e.g. any future non-blending caller) still gets the
    raw dynamic band, never a crash from missing kwargs."""
    mod = _load_alert_evaluator()
    cursor = _baseline_cursor(mean=40.0, stddev=5.0, sample_count=2)
    warning, critical = mod._dynamic_bounds(cursor, "i-aaa", "CPUUtilization", ">", 3.0)
    assert critical == 40.0 + 3.0 * 5.0


def test_dynamic_bounds_low_direction_comparison():
    """"<" metrics (e.g. free-disk-percent) blend on the low side."""
    mod = _load_alert_evaluator()
    cursor = _baseline_cursor(mean=40.0, stddev=5.0, sample_count=mod.CONFIDENT_SAMPLES)
    warning, critical = mod._dynamic_bounds(
        cursor, "i-aaa", "FreeDiskPercent", "<", 3.0,
        static_warning=15.0, static_critical=5.0,
    )
    assert critical == 40.0 - 3.0 * 5.0
    assert warning == 40.0 - (3.0 * 0.66) * 5.0
