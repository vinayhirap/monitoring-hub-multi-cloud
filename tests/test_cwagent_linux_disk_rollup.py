# tests/test_cwagent_linux_disk_rollup.py
"""Prod 2026-09-29 (i-046fecd2..., i-0a3aca62...): CWAgent's bare-{InstanceId}
rollup of disk_used_percent (no `path`) was treated as the root mount "/" and
raced the real "/" series depending on ListMetrics order. It must be ignored,
and the choice among real mounts must not depend on response order."""
import sys
from unittest.mock import MagicMock

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub  # noqa: E402

IID = "i-046fecd2da9485b99"


def _load():
    install_stub("app.db", get_connection=lambda: MagicMock())
    install_stub("app.threshold_defaults", DEFAULT_THRESHOLDS={})
    return load_module("app/collector/disk_mounts.py")


def _m(path=None, device=None, fstype=None):
    dims = [{"Name": "InstanceId", "Value": IID}]
    if path is not None:
        dims += [{"Name": "path", "Value": path}, {"Name": "device", "Value": device},
                 {"Name": "fstype", "Value": fstype}]
    return {"MetricName": "disk_used_percent", "Dimensions": dims}


def _cw(metrics):
    cw = MagicMock()
    cw.list_metrics.side_effect = lambda Namespace, MetricName, Dimensions: {
        "Metrics": list(metrics) if MetricName == "disk_used_percent" else []}
    return cw


def test_bare_rollup_is_never_treated_as_root_in_either_order():
    mod = _load()
    real, rollup, boot = _m("/", "nvme0n1p1", "ext4"), _m(), _m("/boot", "nvme0n1p16", "ext4")
    for order in ([rollup, real, boot], [real, boot, rollup], [boot, rollup, real]):
        out = mod.all_cwagent_disk_dims(_cw(order), IID)
        assert sorted(o[1] for o in out) == ["/", "/boot"], order
        root = [o for o in out if o[1] == "/"][0]
        assert {d["Name"]: d["Value"] for d in root[0]}["device"] == "nvme0n1p1"
        assert root[2] == "disk_used_percent" and root[4] is False


def test_only_a_rollup_yields_no_mounts():
    mod = _load()
    assert mod.all_cwagent_disk_dims(_cw([_m()]), IID) == []


def test_same_path_under_two_devices_is_order_independent():
    mod = _load()
    a, b = _m("/data", "nvme1n1", "ext4"), _m("/data", "nvme2n1", "xfs")
    r1 = mod.all_cwagent_disk_dims(_cw([a, b]), IID)
    r2 = mod.all_cwagent_disk_dims(_cw([b, a]), IID)
    assert len(r1) == len(r2) == 1 and r1[0][0] == r2[0][0]


def test_pseudo_filesystems_still_filtered():
    mod = _load()
    out = mod.all_cwagent_disk_dims(_cw([_m("/", "nvme0n1p1", "ext4"),
                                         _m("/snap/core20/2866", "loop12", "squashfs"),
                                         _m("/run", "tmpfs", "tmpfs")]), IID)
    assert [o[1] for o in out] == ["/"]
