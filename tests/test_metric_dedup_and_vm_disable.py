# tests/test_metric_dedup_and_vm_disable.py
"""
Regression coverage for the fixes in monitoring-hub-metric-audit.md §8/§10:

  1. EC2 critical-tier (CPU/Network) and ELB tasks must fire on the
     "critical" tier ONLY, not also on "standard" -- they were previously
     dispatched via `tier in ("critical","standard")`, causing a duplicate
     GetMetricData call every time a "standard" cycle coincided with a
     "critical" one.
  2. EBS must fire on the "standard" tier ONLY, not also on "low" -- same
     duplicate-call bug, `tier in ("standard","low")`.
  3. describe_polling.py's VM push and scheduler.py's VM sync must not
     fire while VictoriaMetrics is stopped, without deleting either
     function (only the call sites are gated).
  4. Azure/GCP "categories" filtering must actually restrict the SQL
     WHERE clause, so the core/extended tiering in multicloud_scheduler.py
     is real, not a no-op.

Without a live AWS/DB, these tests exercise the actual dispatch logic in
app/collector/metrics/runner.py::_collect_account by monkeypatching the
per-task collector functions it calls by (module-global) name -- since
_DISPATCH is rebuilt from those names on every call, patching
mod._collect_ec2_critical etc. before invoking _collect_account is
sufficient to observe exactly which task types each tier triggers.
"""
import sys
from unittest.mock import MagicMock

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub


# ── 1 & 2: AWS tier-dispatch dedup ──────────────────────────────────

class _ResourceCursor:
    """Returns one resource of every relevant type, regardless of the
    tier-specific WHERE clause differences -- the test only cares which
    _collect_* functions _collect_account() decides to call for a given
    tier, not the SQL filtering itself (covered structurally, not by
    fixture data, since both tier branches in
    _get_resources_for_account() select the same columns)."""

    _ROWS = [
        {"id": 1, "resource_id": "i-aaa", "resource_type": "ec2", "name": "i-aaa",
         "region": "ap-south-1", "tags": "{}"},
        {"id": 2, "resource_id": "vol-aaa", "resource_type": "ebs", "name": "vol-aaa",
         "region": "ap-south-1", "tags": "{}"},
        {"id": 3, "resource_id": "db-aaa", "resource_type": "rds", "name": "db-aaa",
         "region": "ap-south-1", "tags": "{}"},
        {"id": 4, "resource_id": "arn:aws:elasticloadbalancing:...:loadbalancer/app/x",
         "resource_type": "elb", "name": "lb-aaa", "region": "ap-south-1", "tags": "{}"},
        {"id": 5, "resource_id": "fn-aaa", "resource_type": "lambda", "name": "fn-aaa",
         "region": "ap-south-1", "tags": "{}"},
    ]

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return list(self._ROWS)

    def close(self):
        pass


class _FakeConn:
    def cursor(self, dictionary=True):
        return _ResourceCursor()

    def commit(self):
        pass

    def close(self):
        pass


def _load_runner_and_record_tasks(tier):
    """Loads runner.py fresh, patches every _collect_* function to just
    record its own name, runs _collect_account(tier=tier) with a fake
    account, and returns the set of task-function names that fired."""
    install_stub("app.db", get_connection=lambda: _FakeConn())
    install_stub("app.aws.sts", get_boto3_session=lambda account: MagicMock())
    install_stub("app.collector.metrics_writer",
                 write_metric=lambda *a, **k: None,
                 write_metric_history_batch=lambda *a, **k: None)
    install_stub("app.collector.disk_mounts",
                 all_cwagent_disk_dims=lambda cw, iid: [],
                 ensure_disk_mount_metric_registered=lambda *a, **k: None)
    mod = load_module("app/collector/metrics/runner.py")

    fired = set()
    for fn_name in ("_collect_ec2_critical", "_collect_ec2_low",
                     "_collect_ec2_cwagent_mem", "_collect_ec2_cwagent_disk",
                     "_collect_ebs", "_collect_rds", "_collect_elb",
                     "_collect_lambda_standard", "_collect_lambda_low"):
        def _make_recorder(name):
            def _recorder(*a, **k):
                fired.add(name)
            return _recorder
        setattr(mod, fn_name, _make_recorder(fn_name))

    account = {"account_name": "test", "default_region": "ap-south-1", "id": 1}
    mod._collect_account(account, tier=tier)
    return fired


def test_critical_tier_fires_ec2_critical_and_elb_but_not_ebs():
    fired = _load_runner_and_record_tasks("critical")
    assert "_collect_ec2_critical" in fired
    assert "_collect_elb" in fired
    assert "_collect_rds" in fired  # RDS runs on every tier -- revenue-critical
    assert "_collect_ebs" not in fired
    assert "_collect_ec2_low" not in fired
    assert "_collect_lambda_standard" not in fired


def test_standard_tier_fires_ebs_and_rds_but_not_ec2_critical_or_elb():
    """The actual bug fix under test: before the fix, "standard" also
    re-triggered ec2_critical and elb (dispatch was
    `tier in ("critical","standard")`), duplicating a GetMetricData call
    the "critical" tier's own independent 2-min loop had already just
    made. Confirms that's gone."""
    fired = _load_runner_and_record_tasks("standard")
    assert "_collect_ebs" in fired
    assert "_collect_rds" in fired
    assert "_collect_lambda_standard" in fired
    assert "_collect_ec2_critical" not in fired, \
        "standard tier must not re-poll EC2 CPU/Network -- critical tier already covers it"
    assert "_collect_elb" not in fired, \
        "standard tier must not re-poll ALB -- critical tier already covers it"


def test_low_tier_fires_ec2_low_and_cwagent_but_not_ebs():
    """The other half of the fix: "low" previously also re-triggered ebs
    (dispatch was `tier in ("standard","low")`), duplicating a read of
    EBS's 5-min-resolution data that "standard" had already just fetched."""
    fired = _load_runner_and_record_tasks("low")
    assert "_collect_ec2_low" in fired
    assert "_collect_ec2_cwagent_mem" in fired
    assert "_collect_ec2_cwagent_disk" in fired
    assert "_collect_lambda_low" in fired
    assert "_collect_rds" in fired
    assert "_collect_ebs" not in fired, \
        "low tier must not re-poll EBS -- standard tier already covers its 5-min-resolution data"


# ── 3: VM call sites disabled by default ────────────────────────────

def test_describe_polling_vm_push_is_a_noop_by_default():
    """_push_to_vm() itself is untouched (module not cleaned up yet, per
    instruction) but must not attempt a network call while
    _VM_PUSH_ENABLED is False."""
    install_stub("app.db", get_connection=lambda: _FakeConn())
    install_stub("app.clients.vm_client", VM_URL="http://fake-vm")
    install_stub("app.aws.collector_direct", get_session=MagicMock())
    install_stub("app.collector.metrics_writer",
                 write_metrics_batch=lambda rows: None,
                 write_metric_history_batch=lambda rows: None)
    mod = load_module("app/aws/describe_polling.py")

    assert mod._VM_PUSH_ENABLED is False

    calls = []
    mod.requests.post = lambda *a, **k: calls.append((a, k))
    mod._push_to_vm(["some_metric 1 12345"])
    assert calls == [], "no HTTP call should be attempted while VM is stopped"


def test_scheduler_vm_sync_disabled_by_default():
    """_VM_SYNC_ENABLED gates the call site in run_once(); the import of
    sync_metrics_from_vm itself only happens when the flag is True, so
    with it False the (stopped) VM is never touched even indirectly."""
    install_stub("app.db", get_connection=lambda: _FakeConn())
    mod = load_module("app/collector/scheduler.py")
    assert mod._VM_SYNC_ENABLED is False


# ── 4: Azure/GCP category filtering is real, not a no-op ───────────

class _CapturingCursor:
    def __init__(self):
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params))

    def fetchall(self):
        return []

    def close(self):
        pass


def test_azure_enabled_metrics_applies_category_filter_when_given():
    install_stub("app.credentials", load_credential=lambda a: "secret")
    install_stub("app.collector.metrics_writer",
                 write_metrics_batch=lambda rows: None,
                 write_metric_history_batch=lambda rows: None)
    install_stub("app.db", get_connection=lambda: None)
    mod = load_module("app/providers/azure/metrics_collector.py")

    cur = _CapturingCursor()
    mod._enabled_azure_metrics(cur, 1, categories=("core",))
    sql, params = cur.queries[0]
    assert "mc.category IN (%s)" in sql
    assert params == [1, "core"]

    cur2 = _CapturingCursor()
    mod._enabled_azure_metrics(cur2, 1, categories=None)
    sql2, params2 = cur2.queries[0]
    assert "mc.category" not in sql2
    assert params2 == [1]


def test_gcp_enabled_metrics_applies_category_filter_when_given():
    install_stub("google.cloud.monitoring_v3",
                 MetricsQueryResult=object, MetricAggregationType=type("A", (), {"AVERAGE": "Average"}))
    install_stub("google.oauth2.service_account", Credentials=object())
    install_stub("app.credentials", load_credential=lambda a: "{}")
    install_stub("app.collector.metrics_writer",
                 write_metrics_batch=lambda rows: None,
                 write_metric_history_batch=lambda rows: None)
    install_stub("app.providers.gcp.metrics_extended", EXTENDED_RESOLVERS={})
    install_stub("app.db", get_connection=lambda: None)
    mod = load_module("app/providers/gcp/metrics_collector.py")

    cur = _CapturingCursor()
    mod._enabled_gcp_metrics(cur, 1, categories=("extended", "directory"))
    sql, params = cur.queries[0]
    assert "mc.category IN (%s,%s)" in sql
    assert params == [1, "extended", "directory"]
