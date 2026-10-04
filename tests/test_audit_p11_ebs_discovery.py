# tests/test_audit_p11_ebs_discovery.py
"""Audit B4/F4: unattached EBS volumes are discovered (so counts agree), never polled, and reported as idle spend."""
import sys
import types
from pathlib import Path

import app          # noqa: F401
import app.auth     # noqa: F401

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class _Pager:
    def __init__(self, pages):
        self.pages = pages
    def paginate(self):
        return iter(self.pages)


class _Ec2:
    def __init__(self, vols):
        self.vols = vols
    def get_paginator(self, name):
        assert name == "describe_volumes"
        return _Pager([{"Volumes": self.vols}])


class _Session:
    def __init__(self, vols):
        self.vols = vols
    def client(self, svc, region_name=None, config=None):
        return _Ec2(self.vols)


VOLS = [
    {"VolumeId": "vol-att", "State": "in-use", "Size": 100, "Attachments": [{"InstanceId": "i-1"}],
     "Tags": [{"Key": "Name", "Value": "root"}]},
    {"VolumeId": "vol-free1", "State": "available", "Size": 500, "VolumeType": "gp3", "Attachments": [],
     "Tags": [{"Key": "Name", "Value": "old-data"}, {"Key": "Environment", "Value": "prod"}]},
    {"VolumeId": "vol-free2", "State": "available", "Size": 8, "Attachments": []},
]


def _discovery():
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.aws.sts", get_boto3_session=lambda a: None)
    install_stub("app.aws.boto_config", STANDARD_RETRY=None)
    return load_module("app/collector/discovery/runner.py")


def test_only_unattached_volumes_are_added_and_attached_ones_keep_the_instance_tags():
    m = _discovery()
    calls = []
    m._upsert_resource = lambda cur, acct, rtype, rid, name, tags, region: calls.append((rtype, rid, name, tags, region))
    m._discover_ebs(_Session(VOLS), {"id": 10, "account_name": "U4RAD"}, "ap-south-1", None)
    ids = [c[1] for c in calls]
    assert ids == ["vol-free1", "vol-free2"]                       # vol-att is left to _discover_ec2 (instance tags)
    free1 = calls[0]
    assert free1[0] == "ebs" and free1[2] == "old-data"            # Name tag becomes the display name
    assert free1[3]["_ebs_state"] == "available" and free1[3]["_ebs_size_gib"] == "500"
    assert calls[1][2] == "vol-free2"                              # no Name tag -> the id


def test_discovery_failure_is_contained():
    m = _discovery()
    class Boom:
        def client(self, *a, **k):
            raise RuntimeError("AccessDenied")
    m._discover_ebs(Boom(), {"id": 1, "account_name": "A"}, "r", None)       # must not raise


def test_metrics_polling_skips_available_volumes_but_keeps_null_tag_rows():
    sql = (ROOT / "app/collector/metrics/runner.py").read_text()
    assert "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(tags, '$._ebs_state')), '') = 'available'" in sql
    assert "AND NOT (resource_type = 'ebs'" in sql                # NULL-safe form (a bare x = 'available' would drop NULLs)


class _Ec2Cspm(_Ec2):
    pass


def _cspm():
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.aws.sts", get_boto3_session=lambda a: None)
    install_stub("app.aws.boto_config", STANDARD_RETRY=None)
    return load_module("app/collector/cspm.py")


def test_unattached_volume_finding():
    m = _cspm()
    import datetime
    vols = [dict(v) for v in VOLS]
    vols[1]["CreateTime"] = datetime.datetime(2025, 3, 1)
    out = m._check_unattached_ebs(_Session(vols), "ap-south-1")
    assert [f["resource_id"] for f in out] == ["vol-free1", "vol-free2"]
    f = out[0]
    assert f["check_id"] == "ebs_unattached" and f["severity"] == "LOW" and f["region"] == "ap-south-1"
    assert "500 GiB" in f["description"] and "2025-03-01" in f["description"] and "billed" in f["description"]


def test_check_is_registered_labelled_and_console_linked():
    assert 'run.run("ebs_unattached", _check_unattached_ebs' in (ROOT / "app/collector/cspm.py").read_text()
    assert "ebs_unattached" in (ROOT / "frontend/src/pages/SecurityFindings.jsx").read_text()
    assert 'check_id in ("ebs_unencrypted", "ebs_unattached")' in (ROOT / "app/api/security.py").read_text()
