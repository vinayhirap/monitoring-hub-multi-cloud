# tests/test_cwagent_windows_disk.py
"""
Regression coverage for the Windows CWAgent disk-mount bug found and
fixed live against i-0424cb66e22e05a21 (U4RAD-JUMP):

CWAgent's disk metric naming/dimensions are OS-dependent (Linux:
disk_used_percent/`path`; Windows: LogicalDisk % Free Space/`instance`).
CloudWatch also auto-publishes a bare-InstanceId-only rollup metric
alongside Windows' fully-dimensioned per-drive one (confirmed live via
list_metrics). The first version of the Windows fix defaulted that
bare rollup to path="/", which collided with the real "C:" mount --
both are in ROOT_PATHS, so metric_name_for_mount() collapsed them to
the identical unslugified name, producing a duplicate CloudWatch query
Id ("disk_disk_used_percent" twice). CloudWatch rejected the entire
batched GetMetricData call with "values for parameter id ... are not
unique", silently zeroing out BOTH mem and disk data for the instance
(they share one batched call in get_ec2_metric_series).

This test locks in the fix: the bare rollup (no `instance` dimension)
must be skipped outright, leaving exactly one mount.
"""
import sys
from unittest.mock import MagicMock

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub


def _load_disk_mounts():
    install_stub("app.db", get_connection=lambda: MagicMock())
    install_stub("app.threshold_defaults", DEFAULT_THRESHOLDS={})
    return load_module("app/collector/disk_mounts.py")


def _cw_with_windows_shape(bare_rollup=True, real_mount=True, extra_drive=None):
    """Builds a mock cloudwatch client matching the exact live
    list_metrics response shape seen for a Windows instance."""
    cw = MagicMock()

    def list_metrics(Namespace, MetricName, Dimensions):
        if MetricName == "disk_used_percent":
            return {"Metrics": []}  # Linux name never found -- it's a Windows box
        if MetricName == "LogicalDisk % Free Space":
            metrics = []
            if bare_rollup:
                metrics.append({
                    "MetricName": MetricName,
                    "Dimensions": [{"Name": "InstanceId", "Value": "i-0424cb66e22e05a21"}],
                })
            if real_mount:
                metrics.append({
                    "MetricName": MetricName,
                    "Dimensions": [
                        {"Name": "instance", "Value": "C:"},
                        {"Name": "InstanceId", "Value": "i-0424cb66e22e05a21"},
                        {"Name": "ImageId", "Value": "ami-049f0f6f51145ff40"},
                        {"Name": "objectname", "Value": "LogicalDisk"},
                        {"Name": "InstanceType", "Value": "t3a.medium"},
                    ],
                })
            if extra_drive:
                metrics.append({
                    "MetricName": MetricName,
                    "Dimensions": [
                        {"Name": "instance", "Value": extra_drive},
                        {"Name": "InstanceId", "Value": "i-0424cb66e22e05a21"},
                        {"Name": "ImageId", "Value": "ami-049f0f6f51145ff40"},
                        {"Name": "objectname", "Value": "LogicalDisk"},
                        {"Name": "InstanceType", "Value": "t3a.medium"},
                    ],
                })
            return {"Metrics": metrics}
        return {"Metrics": []}

    cw.list_metrics.side_effect = list_metrics
    return cw


def test_bare_rollup_and_real_mount_produce_exactly_one_result():
    """The exact live shape: CloudWatch returns both the bare-InstanceId
    rollup and the real C: mount. Only C: should survive -- this is the
    duplicate-query-Id bug this test exists to lock in."""
    disk_mounts = _load_disk_mounts()
    cw = _cw_with_windows_shape(bare_rollup=True, real_mount=True)

    result = disk_mounts.all_cwagent_disk_dims(cw, "i-0424cb66e22e05a21")

    assert len(result) == 1, (
        f"expected exactly 1 mount (bare rollup must be skipped), got {len(result)}: "
        f"{[r[1] for r in result]} -- this is the duplicate CloudWatch query Id regression"
    )
    dims, path, metric_name, cw_metric_name, invert = result[0]
    assert path == "C:"
    assert metric_name == "disk_used_percent"  # unslugified -- C: is a ROOT_PATHS entry
    assert cw_metric_name == "LogicalDisk % Free Space"
    assert invert is True  # free% -> used% needs (100 - value)


def test_no_id_collision_across_multiple_real_drives():
    """Two genuine drives (C: and D:) must produce two DIFFERENT
    metric_names, not collide -- only C:/`/` collapse to the same name
    by design (the "one OS's root, whichever name it uses" case)."""
    disk_mounts = _load_disk_mounts()
    cw = _cw_with_windows_shape(bare_rollup=True, real_mount=True, extra_drive="D:")

    result = disk_mounts.all_cwagent_disk_dims(cw, "i-0424cb66e22e05a21")
    metric_names = [r[2] for r in result]

    assert len(result) == 2, f"expected C: and D:, got: {[r[1] for r in result]}"
    assert len(set(metric_names)) == 2, (
        f"metric_names collided: {metric_names} -- would reproduce the "
        f"duplicate CloudWatch query Id bug"
    )


def test_bare_rollup_only_no_real_dimensions_yields_nothing():
    """If CWAgent is broken/misconfigured such that CloudWatch only ever
    has the bare rollup (no genuine per-drive metric at all), there is
    truly no mount data to show -- correctly yields zero mounts rather
    than fabricating a fake "/" one."""
    disk_mounts = _load_disk_mounts()
    cw = _cw_with_windows_shape(bare_rollup=True, real_mount=False)

    result = disk_mounts.all_cwagent_disk_dims(cw, "i-0424cb66e22e05a21")

    assert result == []


def test_linux_path_still_works_unchanged():
    """Sanity check that the Windows-specific branch didn't regress the
    ordinary Linux case (dims.get("path"), no `instance` dimension at
    all, no invert)."""
    disk_mounts = _load_disk_mounts()
    cw = MagicMock()

    def list_metrics(Namespace, MetricName, Dimensions):
        if MetricName == "disk_used_percent":
            return {"Metrics": [{
                "MetricName": MetricName,
                "Dimensions": [
                    {"Name": "path", "Value": "/"},
                    {"Name": "InstanceId", "Value": "i-linux12345"},
                    {"Name": "fstype", "Value": "ext4"},
                ],
            }]}
        return {"Metrics": []}

    cw.list_metrics.side_effect = list_metrics

    result = disk_mounts.all_cwagent_disk_dims(cw, "i-linux12345")

    assert len(result) == 1
    dims, path, metric_name, cw_metric_name, invert = result[0]
    assert path == "/"
    assert metric_name == "disk_used_percent"
    assert cw_metric_name == "disk_used_percent"
    assert invert is False
