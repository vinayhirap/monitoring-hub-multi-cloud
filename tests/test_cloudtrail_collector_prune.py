# tests/test_cloudtrail_collector_prune.py
"""
D02 audit: cloud_events had no retention/prune job anywhere in the
codebase (grepped app/ for DELETE FROM cloud_events -- only hit was the
one-time cleanup in admin/accounts.py's delete_account, which only
fires when an account is removed entirely). Every other append-only
table the low-tier scheduler populates (metric_history, op_events,
synthetic_check_results) already has its own prune_*() wired in; this
covers the new prune_cloud_events() the same way
tests/test_audit_b14_scheduler_pipeline.py covers prune_metric_history.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub


def _load_collector():
    install_stub("app.aws.sts", get_boto3_session=lambda *a, **k: None)
    return load_module("app/aws/cloudtrail_collector.py")


def test_prune_cloud_events_deletes_rows_older_than_retain_days():
    executed = []

    class _Cur:
        rowcount = 0

        def execute(self, sql, params=None):
            executed.append((sql, params))
            _Cur.rowcount = 42

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    install_stub("app.db", get_connection=lambda: _Conn())
    mod = _load_collector()

    deleted = mod.prune_cloud_events(retain_days=90)

    assert deleted == 42
    assert len(executed) == 1
    sql, params = executed[0]
    assert "DELETE FROM cloud_events" in sql
    assert "event_time" in sql
    assert params == (90,)


def test_prune_cloud_events_rolls_back_on_error():
    class _Cur:
        def execute(self, sql, params=None):
            raise RuntimeError("simulated DB error")

        def close(self):
            pass

    rolled_back = []

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            rolled_back.append(True)

        def close(self):
            pass

    install_stub("app.db", get_connection=lambda: _Conn())
    mod = _load_collector()

    try:
        mod.prune_cloud_events()
        assert False, "expected RuntimeError to propagate"
    except RuntimeError:
        pass

    assert rolled_back == [True]
