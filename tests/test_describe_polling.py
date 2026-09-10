# tests/test_describe_polling.py
"""
Covers app/aws/describe_polling.py's poll_ec2_status() dual-write: the
existing VM push must stay byte-identical (an external Grafana dashboard
may depend on it), and the NEW local `metrics` write must correctly
thread resource_db_id through per-instance, using the exact
InstanceId -> resources.id mapping this module's own DB query provides.
"""
import sys
from unittest.mock import MagicMock

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self, dictionary=True):
        return _FakeCursor(self.rows)

    def close(self):
        pass


def _load():
    written = []
    history_written = []
    install_stub(
        "app.collector.metrics_writer",
        write_metrics_batch=lambda rows: written.extend(rows),
        # write_metric_history_batch is imported by describe_polling.py
        # (added by apply_fix_alb_healthy_hosts_history.py, after this test
        # file was first written) but was never added to this stub -- the
        # isolated module loader raised ImportError on every test in this
        # file as a result. Stubbed now the same way write_metrics_batch
        # already was, so imports succeed; not otherwise asserted on here
        # since no test in this file currently checks history-row content.
        write_metric_history_batch=lambda rows: history_written.extend(rows),
    )
    install_stub("app.clients.vm_client", VM_URL="http://fake-vm")
    install_stub("app.aws.collector_direct", get_session=MagicMock())
    mod = load_module("app/aws/describe_polling.py")
    return mod, written


def test_get_ec2_instances_by_region_returns_id_and_resource_db_id_pairs():
    rows = [
        {"account_db_id": 1, "role_arn": None, "external_id": None, "default_region": "ap-south-1",
         "resource_db_id": 501, "resource_id": "i-aaa"},
        {"account_db_id": 1, "role_arn": None, "external_id": None, "default_region": "ap-south-1",
         "resource_db_id": 502, "resource_id": "i-bbb"},
    ]
    install_stub("app.db", get_connection=lambda: _FakeConn(rows))
    mod, _ = _load()

    grouped = mod._get_ec2_instances_by_region()
    key = (1, None, None, "ap-south-1")
    assert grouped[key] == [("i-aaa", 501), ("i-bbb", 502)]


def test_poll_ec2_status_writes_locally_and_pushes_to_vm_unchanged():
    rows = [
        {"account_db_id": 1, "role_arn": None, "external_id": None, "default_region": "ap-south-1",
         "resource_db_id": 501, "resource_id": "i-aaa"},
        {"account_db_id": 1, "role_arn": None, "external_id": None, "default_region": "ap-south-1",
         "resource_db_id": 502, "resource_id": "i-bbb"},
    ]
    install_stub("app.db", get_connection=lambda: _FakeConn(rows))
    mod, written = _load()

    mod._session_for = MagicMock()
    fake_ec2 = MagicMock()
    fake_ec2.describe_instance_status.return_value = {
        "InstanceStatuses": [
            {"InstanceId": "i-aaa", "SystemStatus": {"Status": "ok"}, "InstanceStatus": {"Status": "ok"}},
            {"InstanceId": "i-bbb", "SystemStatus": {"Status": "impaired"}, "InstanceStatus": {"Status": "ok"}},
        ]
    }
    mod._session_for.return_value.client.return_value = fake_ec2
    mod._push_to_vm = MagicMock()

    total = mod.poll_ec2_status()

    assert total == 2
    assert (501, "statuscheckfailed", 0.0) in written
    assert (502, "statuscheckfailed", 1.0) in written
    assert mod._push_to_vm.called, "VM push must still happen -- an external Grafana dashboard may rely on it"
    pushed_lines = mod._push_to_vm.call_args[0][0]
    assert any("i-aaa" in line for line in pushed_lines)
    assert any("i-bbb" in line for line in pushed_lines)


def test_poll_ec2_status_handles_empty_account_gracefully():
    install_stub("app.db", get_connection=lambda: _FakeConn([]))
    mod, written = _load()
    mod._push_to_vm = MagicMock()

    total = mod.poll_ec2_status()
    assert total == 0
    assert written == []
    assert not mod._push_to_vm.called
