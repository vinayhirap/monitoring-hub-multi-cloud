from app import metric_display as md


def _s(minutes, vals):
    return [{"t": f"2026-10-01T13:{m:02d}:00+00:00", "v": v} for m, v in zip(minutes, vals)]


def test_bucket_sizes_cap_points_and_use_nice_steps():
    assert md.bucket_seconds(1) == 60
    assert md.bucket_seconds(24) == 300
    assert md.bucket_seconds(168) == 3600
    assert md.bucket_seconds(720) == 10800


def test_effective_hours_capped_by_retention():
    assert md.effective_hours(17520) == md.METRIC_HISTORY_RETENTION_DAYS * 24
    assert md.effective_hours(6) == 6


def test_bucketize_all_stats_and_native():
    out = md.bucketize(_s([0, 5, 10], [1, 3, 8]), 3600, "Maximum")
    assert len(out) == 1
    p = out[0]
    assert (p["a"], p["mn"], p["mx"], p["s"], p["n"]) == (4.0, 1.0, 8.0, 12.0, 3)
    assert p["v"] == 8.0           # native Maximum


def test_rate_metric_scaled_by_polling_period_and_uses_average():
    spec = md.display_spec("aws", "ebs", "VolumeReadOps", "Count", "Sum")
    assert spec["unit"] == "Count/Second" and spec["rate"]
    assert abs(spec["scale"] - 1 / 300) < 1e-12
    out = md.bucketize(_s([0, 5], [300, 600]), 3600, "Sum", spec["scale"], rate=True)
    assert out[0]["v"] == out[0]["a"] == 1.5


def test_unknown_inputs_never_raise():
    assert md.bucketize("junk", 300) == "junk"
    assert md.bucketize([{"t": "bad", "v": 1}], 300) == [{"t": "bad", "v": 1}]
    assert md.display_spec("zzz", "nope", "Whatever")["title"] == "Whatever"
    assert md.polling_info("zzz", "nope", "x", "x")["interval_seconds"] in (300, None)
    assert md.shape_response("nope", {"a": [], "b": None}, 6)["bucket_secs"] == 60


def test_polling_matches_polling_model():
    from app.collector import polling_model as pm
    for m in pm.AWS_CORE_METRICS:
        info = md.polling_info("aws", m.resource_type, m.cw_name, m.db_name)
        assert info["interval_seconds"] == pm.TIER_SECONDS[m.tier]
        assert info["period_seconds"] == m.period_sec


def test_stats_offered_are_honest():
    assert "Sum" not in md.stats_available("Average")
    assert "Sum" in md.stats_available("Sum")
    assert "Sum" not in md.stats_available("Sum", rate=True)
