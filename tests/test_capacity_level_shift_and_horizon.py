"""
tests/test_capacity_level_shift_and_horizon.py -- 2026-10-05.

PROD finding: compute_capacity_forecasts said i-046f... (disk 84.3%) fills in 11.4 days. The series was
flat at 70.9%, jumped to 81.9% in ~90 minutes on 09-29, and has grown ~0.45 points/day since: ONE step
fed into one straight-line fit gives a slope (1.3/day) that describes neither segment. The fit now uses
only the points after the last abrupt step toward exhaustion. Separately, far-off dates (236 / 334 days)
are no longer quoted as predictions in RCA text.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_module, install_stub, FakeConn, FakeCursor  # noqa: E402


def _rows(values, start_ts=0, step=3600, rid=1, res="vol-1"):
    return [{"rid": rid, "aws_account_id": 1, "aws_resource_id": res, "ts": start_ts + i * step,
             "metric_value": float(v)} for i, v in enumerate(values)]


def _trend_module(metric, rows):
    class _Cursor(FakeCursor):
        def execute(self, sql, params=None):
            norm = " ".join(sql.split())
            self._pending = rows if ("metric_name = %s" in norm and params[0] == metric) else []

    class _Conn(FakeConn):
        def cursor(self, dictionary=True):
            return _Cursor([])

    install_stub("app.db", get_connection=lambda: _Conn([]))
    return load_module("app/collector/trend.py")


def _prod_like_series(noise_seed=3):
    """14 days hourly: flat 70.95, +10.9 step at day 8.4, then +0.44/day."""
    rng = np.random.default_rng(noise_seed)
    days = np.arange(14 * 24) / 24.0
    return np.where(days < 8.4, 70.95, 81.85 + 0.44 * (days - 8.4)) + rng.normal(0, 0.03, days.size)


def test_a_one_off_jump_no_longer_inflates_the_growth_rate():
    series = _prod_like_series()
    mod = _trend_module("disk_used_percent", _rows(series))
    f = [x for x in mod.compute_capacity_forecasts() if x["metric_name"] == "disk_used_percent"]
    assert len(f) == 1
    assert 0.3 < f[0]["slope_per_day"] < 0.6, f[0]                  # the real post-jump growth (~0.44)
    assert 25 < f[0]["days_to_exhaustion"] < 50, f[0]               # ~35 days, not the ~12 a naive fit gives
    # and prove the naive fit really would have been wrong on this exact data
    naive_slope, _ = np.polyfit(np.arange(series.size) / 24.0, series, 1)
    assert (100 - series[-1]) / naive_slope < 15


def test_a_smooth_ramp_is_untouched():
    values = [50.0 + 2.0 * (i / 4.0) for i in range(14 * 4)]
    mod = _trend_module("disk_used_percent", _rows(values, step=21600))
    f = mod.compute_capacity_forecasts()[0]
    assert abs(f["slope_per_day"] - 2.0) < 0.02


def test_right_after_a_jump_there_is_too_little_history_so_no_forecast_is_published():
    values = [70.0] * 300 + [80.0] * 6                              # step 6 hours ago
    mod = _trend_module("disk_used_percent", _rows(values))
    assert mod.compute_capacity_forecasts() == []


def test_a_cleanup_drop_is_not_treated_as_a_level_shift():
    """Sawtooth disks (grow, purge, grow) keep the old behaviour: only steps TOWARD exhaustion restart the fit."""
    up = [60.0 + 2.0 * (i / 24.0) for i in range(10 * 24)]          # 60 -> 80 over 10 days
    after = [65.0 + 2.0 * (i / 24.0) for i in range(4 * 24)]        # purge to 65, regrow 4 days
    mod = _trend_module("disk_used_percent", _rows(up + after))
    f = mod.compute_capacity_forecasts()
    assert len(f) == 1 and f[0]["slope_per_day"] > 0


def test_free_space_metrics_use_the_downward_step():
    rng = np.random.default_rng(5)
    days = np.arange(14 * 24) / 24.0
    gb = np.where(days < 8.4, 500.0, 300.0 - 5.0 * (days - 8.4)) + rng.normal(0, 0.2, days.size)
    mod = _trend_module("FreeStorageSpace", _rows(gb))
    f = [x for x in mod.compute_capacity_forecasts() if x["metric_name"] == "FreeStorageSpace"]
    assert len(f) == 1 and f[0]["slope_per_day"] < 0
    assert f[0]["days_to_exhaustion"] > 40                          # ~55 days after the drop; a naive fit says ~19


def test_noise_alone_never_counts_as_a_step():
    rng = np.random.default_rng(9)
    values = 70.0 + 0.5 * (np.arange(14 * 24) / 24.0) + rng.normal(0, 0.4, 14 * 24)
    mod = _trend_module("disk_used_percent", _rows(values))
    x, y = mod._after_last_level_shift(np.arange(values.size) * 3600.0, values, 100.0)
    assert y.size == values.size


# -- 90-day horizon in the report wording ------------------------------------

def _rca():
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.collector.rca", explain_alert=lambda i: {})
    install_stub("app.llm.summarizer", generate_rca_narrative=lambda f: None, is_enabled=lambda: False)
    install_stub("app.llm.aws_docs", get_references=lambda *a: [])
    return load_module("app/llm/rca_report.py")


def test_report_text_quotes_a_date_inside_the_horizon_and_says_slow_beyond_it():
    mod = _rca()
    near = mod._forecast_text({"days_to_exhaustion": 35.5, "slope_per_day": 0.44, "counts_up": True})
    assert "reach 100% in about 36 days" in near
    edge = mod._forecast_text({"days_to_exhaustion": 90.0, "slope_per_day": 0.1, "counts_up": True})
    assert "about 90 days" in edge
    far = mod._forecast_text({"days_to_exhaustion": 333.7, "slope_per_day": 0.0509, "counts_up": True})
    assert "growing only slowly (about 0.05 percentage points per day)" in far
    assert "not projected to fill within the next 90 days" in far and "334" not in far
    free = mod._forecast_text({"days_to_exhaustion": 120.0, "slope_per_day": -1.0, "counts_up": False})
    assert "Free space is shrinking only slowly" in free


def test_alert_summary_text_matches_the_report_wording():
    install_stub("app.db", get_connection=lambda: None)
    rca = load_module("app/collector/rca.py")
    assert "growing only slowly" in rca._forecast_sentence(
        {"days_to_exhaustion": 236.1, "slope_per_day": 0.146, "counts_up": True})
    assert "projected to reach 100% in about 12 days" in rca._forecast_sentence(
        {"days_to_exhaustion": 11.8, "slope_per_day": 1.43, "counts_up": True})
