"""A metric polled every 6h / 1d / 1w must show the smallest preset range that has data, not an empty 1H chart."""
from app.metric_display import widen_window_hours

NOW = 1_000_000_000
H = 3600


def test_daily_metric_at_1h_widens_to_the_week_when_only_one_point_per_day_is_in_a_day():
    pts = [NOW - 3 * H, NOW - 27 * H]          # polled daily: 1 point in 1D, 2 in 1W
    assert widen_window_hours(pts, NOW, 1, 720) == 168


def test_six_hourly_metric_at_1h_widens_to_1d():
    pts = [NOW - 2 * H, NOW - 8 * H, NOW - 14 * H]
    assert widen_window_hours(pts, NOW, 1, 720) == 24


def test_single_old_point_falls_back_to_smallest_window_holding_it():
    assert widen_window_hours([NOW - 30 * H], NOW, 1, 720) == 168


def test_no_points_at_all_returns_none():
    assert widen_window_hours([], NOW, 1, 720) is None


def test_never_narrows_or_returns_the_requested_window():
    assert widen_window_hours([NOW - 10 * H, NOW - 20 * H], NOW, 24, 720) == 168
