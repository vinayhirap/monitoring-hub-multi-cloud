# tests/test_gmd_period_matches_native_resolution.py
"""
2026-09-29: app/collector/metrics/runner.py's _build_queries() hardcoded
"Period": 60 for every AWS_CORE_METRICS query, regardless of that metric's
actual CloudWatch publish cadence. EC2 (on basic monitoring -- confirmed via
describe_polling's own monitoring-mode audit) and EBS both publish on a
5-minute cadence, not 1-minute. CloudWatch still returns the correct VALUE
when queried at a mismatched Period, but it shifts the TIMESTAMP it reports
for that value away from the metric's true publish boundary.

Found live on prod 2026-09-29 (U4RAD-JUMP, i-0424cb66e22e05a21): three
consecutive CPUUtilization values matched exactly between metric_history
and a Period=300 CloudWatch query for the same instance, but every
metric_history.metric_timestamp was 3 minutes later than CloudWatch's real
one -- a silent, deterministic mislabeling that anything doing freshness/
staleness math off metric_timestamp (alert_rules.py, any "as of" display)
would inherit without any error or exception to flag it.

Fix: CoreMetric gained an explicit period_sec field (polling_model.py),
threaded through _collect_core -> _build_queries -> the actual
MetricStat.Period sent to CloudWatch. These tests pin the values that
matter so a future edit can't silently reintroduce the mismatch for any
resource type, cloud, or metric added later without deliberately setting
period_sec.
"""
from tests.conftest import load_module, install_stub


def _load_runner():
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.aws.sts", get_boto3_session=lambda *a, **k: None)
    install_stub("app.aws.boto_config", STANDARD_RETRY={})
    install_stub(
        "app.collector.metrics_writer",
        write_metrics_batch=lambda rows: None,
        write_metric_history_batch=lambda rows, **kw: None,
    )
    install_stub(
        "app.collector.disk_mounts",
        all_cwagent_disk_dims=lambda *a, **k: {},
        ensure_disk_mount_metric_registered=lambda *a, **k: None,
    )
    install_stub("app.collector.api_usage", record=lambda *a, **k: None)
    import sys
    real_polling_model = load_module("app/collector/polling_model.py")
    sys.modules["app.collector.polling_model"] = real_polling_model
    setattr(sys.modules["app.collector"], "polling_model", real_polling_model)
    return load_module("app/collector/metrics/runner.py")


def _periods_by_metric(mod, resource_type, metric_defs):
    """Build one query per metric via the REAL _build_queries() and return
    {cw_metric_name: Period actually sent to CloudWatch}."""
    resources = [{"id": 1, "resource_type": resource_type,
                  "resource_id": "test-id", "name": "test", "tags": "{}"}]
    queries, _id_map = mod._build_queries(resources, metric_defs)
    return {q["MetricStat"]["Metric"]["MetricName"]: q["MetricStat"]["Period"]
            for q in queries}


def test_ec2_core_metrics_query_at_their_real_5min_publish_period():
    """EC2 basic monitoring (confirmed the norm via _log_monitoring_mode_mismatch
    in runner.py) publishes every 5 min -- querying at anything else shifts
    the reported timestamp even though the value comes back correct."""
    mod = _load_runner()
    periods = _periods_by_metric(mod, "ec2", mod.EC2_METRICS_CRITICAL)
    assert periods["CPUUtilization"] == 300
    assert periods["NetworkIn"] == 300
    assert periods["NetworkOut"] == 300


def test_ebs_core_metrics_query_at_their_real_5min_publish_period():
    """No EBS "detailed monitoring" option exists -- every standard EBS
    CloudWatch metric is 5-min native, same as EC2 basic monitoring."""
    mod = _load_runner()
    periods = _periods_by_metric(mod, "ebs", mod.EBS_METRICS)
    assert periods["VolumeQueueLength"] == 300
    assert periods["VolumeReadOps"] == 300
    assert periods["VolumeWriteOps"] == 300


def test_rds_elb_lambda_core_metrics_are_still_1min_unaffected():
    """These are genuinely 1-min-native sources -- the old Period=60 was
    correct for them and must stay 60, not get swept into the EC2/EBS fix."""
    mod = _load_runner()
    rds_periods = _periods_by_metric(mod, "rds", mod.RDS_METRICS)
    assert rds_periods["CPUUtilization"] == 60
    assert rds_periods["DatabaseConnections"] == 60

    elb_periods = _periods_by_metric(mod, "elb", mod.ELB_METRICS)
    assert elb_periods["RequestCount"] == 60

    lambda_periods = _periods_by_metric(mod, "lambda", mod.LAMBDA_METRICS_STANDARD)
    assert lambda_periods["Errors"] == 60


def test_core_metric_period_sec_defaults_to_60_when_not_set():
    """New CoreMetric entries that don't explicitly set period_sec must
    keep today's (correct-for-1-min-metrics) behavior, not silently
    inherit a stale or wrong value."""
    mod = _load_runner()
    m = mod.polling_model.CoreMetric(
        "newsvc", "SomeMetric", "somemetric", "Average", "AWS/NewSvc",
        "standard", 8, None,
    )
    assert m.period_sec == 60
