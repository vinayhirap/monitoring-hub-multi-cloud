# tests/test_audit_p1_static_only_metrics.py
"""Audit A5: availability / absolute-limit metrics must never use a learned band."""
import sys
sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
import app  # noqa: F401
import app.threshold_defaults as td  # noqa: E402
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_availability_and_limit_metrics_are_static_only():
    for name in ("HealthyHostCount", "healthyhosts_describe", "UnHealthyHostCount",
                 "unhealthyhosts_describe", "StatusCheckFailed", "StatusCheckFailed_System",
                 "DaysToExpiry", "NumberOfBackupJobsFailed", "NumberOfRestoreJobsFailed",
                 "NumberOfNotificationsFailed", "ErrorPortAllocation", "FreeStorageSpace",
                 "HealthCheckPercentageHealthy"):
        assert td.is_static_only_metric(name), name


def test_capacity_percent_still_static_only():
    assert td.is_static_only_metric("disk_used_percent")
    assert td.is_static_only_metric("mem_used_percent")
    assert td.is_static_only_metric("disk_used_percent__data")


def test_ordinary_metrics_may_still_be_dynamic():
    for name in ("CPUUtilization", "NetworkIn", "RequestCount", "VolumeWriteBytes",
                 "HTTPCode_Target_5XX_Count", "TargetResponseTime", "", None):
        assert not td.is_static_only_metric(name), name


def test_all_three_enforcement_points_use_the_guard():
    for rel in ("app/collector/threshold_tuning.py", "app/collector/alert_evaluator.py",
                "app/metric_meta.py", "app/api/settings.py"):
        assert "is_static_only_metric" in (ROOT / rel).read_text(), rel
