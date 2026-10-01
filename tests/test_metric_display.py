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


# ── 60 s -> 300 s period cutover (EBS rate metrics) ──────────────────────
def _at(ts_iso, v):
    return {"t": ts_iso, "v": v}


def test_cutover_default_is_prod_value(monkeypatch):
    monkeypatch.delenv("METRIC_PERIOD_CUTOVER_UTC", raising=False)
    from datetime import datetime, timezone
    assert md.period_cutover_epoch() == datetime(2026, 9, 29, 10, 55, tzinfo=timezone.utc).timestamp()


def test_cutover_env_override_off_and_garbage(monkeypatch):
    monkeypatch.setenv("METRIC_PERIOD_CUTOVER_UTC", "none")
    assert md.period_cutover_epoch() is None
    monkeypatch.setenv("METRIC_PERIOD_CUTOVER_UTC", "not a date")
    assert md.period_cutover_epoch() is None          # degrades, never raises
    monkeypatch.setenv("METRIC_PERIOD_CUTOVER_UTC", "2026-09-30T00:00:00Z")
    assert md.period_cutover_epoch() is not None


def test_points_before_cutover_use_60s_after_use_300s(monkeypatch):
    monkeypatch.delenv("METRIC_PERIOD_CUTOVER_UTC", raising=False)
    spec = md.display_spec("aws", "ebs", "VolumeReadOps", "Count", "Sum")
    assert abs(spec["legacy_scale"] - 1 / 60) < 1e-12
    cut = md.period_cutover_epoch()
    # 600 ops in a 60 s window and 3000 ops in a 300 s window are the SAME 10 ops/s
    old = md.bucketize([_at("2026-09-29T09:00:00+00:00", 600)], 3600, "Sum", spec["scale"], True, spec["legacy_scale"], cut)
    new = md.bucketize([_at("2026-09-30T09:00:00+00:00", 3000)], 3600, "Sum", spec["scale"], True, spec["legacy_scale"], cut)
    assert old[0]["v"] == new[0]["v"] == 10.0


def test_bucket_spanning_cutover_is_scaled_per_point(monkeypatch):
    monkeypatch.delenv("METRIC_PERIOD_CUTOVER_UTC", raising=False)
    spec = md.display_spec("aws", "ebs", "VolumeWriteOps", "Count", "Sum")
    cut = md.period_cutover_epoch()
    pts = [_at("2026-09-29T10:50:00+00:00", 600), _at("2026-09-29T11:05:00+00:00", 3000)]
    out = md.bucketize(pts, 7200, "Sum", spec["scale"], True, spec["legacy_scale"], cut)
    assert len(out) == 1 and out[0]["a"] == 10.0 and out[0]["mx"] == 10.0 and out[0]["n"] == 2


def test_non_rate_metrics_ignore_cutover(monkeypatch):
    monkeypatch.delenv("METRIC_PERIOD_CUTOVER_UTC", raising=False)
    spec = md.display_spec("aws", "ec2", "CPUUtilization", "Percent", "Average")
    assert spec["legacy_scale"] is None
    out = md.bucketize([_at("2026-09-29T09:00:00+00:00", 42)], 3600, "Average", spec["scale"], False, None, md.period_cutover_epoch())
    assert out[0]["v"] == 42.0
