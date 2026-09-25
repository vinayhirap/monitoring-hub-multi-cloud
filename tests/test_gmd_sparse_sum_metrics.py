# tests/test_gmd_sparse_sum_metrics.py
"""
2026-09-24: GetMetricData's documented behavior for a Sum-statistic COUNT
metric (RequestCount, HTTPCode_Target_5XX_Count, Lambda Errors/Invocations/
Throttles, and the rest of AWS_CORE_METRICS in polling_model.py tagged
stat="Sum") is that a period with a true count of zero returns NO datapoint
at all -- silence, not a datapoint of 0. app/collector/metrics/runner.py's
_execute_gmd() used to treat "no datapoint" identically for every metric:
skip the write entirely, leaving metrics.metric_value AND metric_timestamp
frozen at whatever the last real (nonzero) reading was.

Found live on prod (xrai-alb): errors5xx stuck at "12" for 5 days after
errors genuinely stopped, alongside a load balancer with a real 0-healthy-
host outage. That's a stale-data / potential false-alert bug in exactly the
category the broader alerts audit exists to catch, so it's fixed the same
way: a test proving the exact failure mode, and a narrow, stat-gated fix
(Sum only -- Average/Maximum gauge metrics like CPUUtilization or
mem_used_percent keep the old "skip, leave stale" behavior, since a missing
gauge reading genuinely means "unknown", not "zero").

These tests exercise the REAL _get_metric_data_all_pages/_request_chunks
machinery (only cw.get_metric_data is faked), not a shortcut mock of
_execute_gmd's internals, so a future change to chunking/paging that
silently breaks the id_map plumbing would also break these.
"""
import sys
from tests.conftest import load_module, install_stub


def _load_runner(written=None, history=None):
    written = written if written is not None else []
    history = history if history is not None else []
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.aws.sts", get_boto3_session=lambda *a, **k: None)
    install_stub("app.aws.boto_config", STANDARD_RETRY={})
    install_stub(
        "app.collector.metrics_writer",
        write_metrics_batch=lambda rows: written.extend(rows),
        write_metric_history_batch=lambda rows: history.extend(rows),
    )
    install_stub(
        "app.collector.disk_mounts",
        all_cwagent_disk_dims=lambda *a, **k: {},
        ensure_disk_mount_metric_registered=lambda *a, **k: None,
    )
    install_stub("app.collector.api_usage", record=lambda *a, **k: None)
    # polling_model is loaded for REAL (not stubbed) -- these tests want the
    # ACTUAL current Sum/Average/Maximum tagging of AWS_CORE_METRICS
    # exercised, so a future edit there that mis-tags a metric's stat is
    # caught here too, not just masked by a fake. install_stub() above
    # already registered a bare stub module at "app.collector" (as a side
    # effect of registering its children) -- load the real file and hang it
    # off that stub as the actual "app.collector.polling_model" runner.py's
    # `from app.collector import polling_model` needs.
    real_polling_model = load_module("app/collector/polling_model.py")
    sys.modules["app.collector.polling_model"] = real_polling_model
    setattr(sys.modules["app.collector"], "polling_model", real_polling_model)
    return load_module("app/collector/metrics/runner.py")


class FakeCloudWatch:
    """Minimal stand-in for boto3's CloudWatch client: one MetricDataResult
    per requested Id, exactly matching GetMetricData's real contract (empty
    Values/Timestamps for a query with no datapoints, never a missing
    entry), no NextToken (single page)."""
    def __init__(self, values_by_id):
        self.values_by_id = values_by_id  # {qid: [(value, ts), ...]}

    def get_metric_data(self, MetricDataQueries, **kwargs):
        results = []
        for q in MetricDataQueries:
            vals = self.values_by_id.get(q["Id"], [])
            results.append({
                "Id": q["Id"],
                "Values": [v for v, _ in vals],
                "Timestamps": [t for _, t in vals],
            })
        return {"MetricDataResults": results}


def test_sum_metric_with_no_datapoint_is_zero_filled_not_skipped():
    written, history = [], []
    mod = _load_runner(written, history)
    id_map = {"q0": (42, "errors5xx", "Sum")}
    cw = FakeCloudWatch({})  # no datapoints for q0 at all
    count = mod._execute_gmd(cw, [{"Id": "q0", "MetricStat": {}}], id_map)
    assert count == 1
    assert written == [(42, "errors5xx", 0.0)]
    assert len(history) == 1
    assert history[0][0] == 42 and history[0][1] == "errors5xx" and history[0][2] == 0.0


def test_sum_metric_with_a_real_datapoint_is_unaffected():
    written, history = [], []
    mod = _load_runner(written, history)
    id_map = {"q0": (42, "errors5xx", "Sum")}
    cw = FakeCloudWatch({"q0": [(12.0, "2026-09-19T06:46:07Z")]})
    count = mod._execute_gmd(cw, [{"Id": "q0", "MetricStat": {}}], id_map)
    assert count == 1
    assert written == [(42, "errors5xx", 12.0)]


def test_average_metric_with_no_datapoint_is_left_stale_not_zero_filled():
    """A missing Average (e.g. CPUUtilization) means 'unknown', not '0%' --
    the old behavior is correct here and must stay unchanged."""
    written, history = [], []
    mod = _load_runner(written, history)
    id_map = {"q0": (42, "cpuutilization", "Average")}
    cw = FakeCloudWatch({})
    count = mod._execute_gmd(cw, [{"Id": "q0", "MetricStat": {}}], id_map)
    assert count == 0
    assert written == [] and history == []


def test_maximum_metric_with_no_datapoint_is_also_left_stale():
    """Lambda ConcurrentExecutions/IteratorAge use Maximum, added in the
    2026-09-23 polling-model rewrite -- must not be swept into the
    Sum-only zero-fill."""
    written, history = [], []
    mod = _load_runner(written, history)
    id_map = {"q0": (5, "concurrentexecutions", "Maximum")}
    cw = FakeCloudWatch({})
    count = mod._execute_gmd(cw, [{"Id": "q0", "MetricStat": {}}], id_map)
    assert count == 0
    assert written == []


def test_cwagent_mem_and_disk_id_map_entries_are_average_and_never_zero_filled():
    """Regression guard: _collect_ec2_cwagent_mem / _disk build their OWN
    id_map dicts (not via _build_queries) and must carry a stat so
    _execute_gmd's 3-tuple unpack doesn't crash them; both are gauge-shaped
    and must keep the 'leave stale' behavior, never fabricate a 0% reading."""
    written, history = [], []
    mod = _load_runner(written, history)
    id_map = {
        "cwmem0": (7, "mem_used_percent", "Average"),
        "cwdisk0": (7, "disk_used_percent", "Average"),
    }
    cw = FakeCloudWatch({})  # neither reports this cycle
    count = mod._execute_gmd(cw, [{"Id": k, "MetricStat": {}} for k in id_map], id_map)
    assert count == 0
    assert written == []


def test_build_queries_tags_every_metric_with_its_real_statistic_from_polling_model():
    """Exercises the REAL, current AWS_CORE_METRICS table (via _legacy()),
    not a hand-written stand-in -- so this breaks if polling_model.py ever
    re-tags one of these metrics' stat without updating this test.
    ELB_METRICS is the "critical"-tier subset (errors5xx/requestcount/
    httpcode_elb_5xx_count are all Sum there); RDS_METRICS gives an
    Average-stat example from a different resource type for contrast."""
    mod = _load_runner()
    elb_resources = [{"id": 1, "resource_type": "elb", "resource_id": "arn:...",
                       "name": "xrai-alb", "tags": "{}"}]
    _, elb_id_map = mod._build_queries(elb_resources, mod.ELB_METRICS)
    elb_stats = {db_name: stat for (_rid, db_name, stat) in elb_id_map.values()}
    assert elb_stats["errors5xx"] == "Sum"
    assert elb_stats["requestcount"] == "Sum"

    rds_resources = [{"id": 2, "resource_type": "rds", "resource_id": "db-1",
                       "name": "db-1", "tags": "{}"}]
    _, rds_id_map = mod._build_queries(rds_resources, mod.RDS_METRICS)
    rds_stats = {db_name: stat for (_rid, db_name, stat) in rds_id_map.values()}
    assert rds_stats["cpuutilization"] == "Average"
    assert rds_stats["dbconnections"] == "Average"


def test_multiple_sum_metrics_in_one_call_each_zero_fill_independently():
    """Lambda Errors/Invocations/Throttles are all Sum -- a function with a
    quiet period should get all three zeroed, not just the first."""
    written, history = [], []
    mod = _load_runner(written, history)
    id_map = {
        "q0": (9, "errors", "Sum"),
        "q1": (9, "invocations", "Sum"),
        "q2": (9, "throttles", "Sum"),
    }
    cw = FakeCloudWatch({"q1": [(3.0, "t")]})  # only invocations reported
    count = mod._execute_gmd(cw, [{"Id": k, "MetricStat": {}} for k in id_map], id_map)
    assert count == 3
    assert set(written) == {(9, "errors", 0.0), (9, "invocations", 3.0), (9, "throttles", 0.0)}


def test_sum_zero_fill_survives_chunking_across_more_than_500_queries():
    """_request_chunks caps a real GMD request at GMD_MAX_QUERIES; the
    zero-fill must apply per-chunk, not just to the first chunk."""
    written, history = [], []
    mod = _load_runner(written, history)
    n = mod.GMD_MAX_QUERIES + 3
    id_map = {f"q{i}": (100 + i, f"metric{i}", "Sum") for i in range(n)}
    queries = [{"Id": qid, "MetricStat": {}} for qid in id_map]
    cw = FakeCloudWatch({})  # nothing reports anything
    count = mod._execute_gmd(cw, queries, id_map)
    assert count == n
    assert len(written) == n
    assert all(v == 0.0 for (_rid, _name, v) in written)
