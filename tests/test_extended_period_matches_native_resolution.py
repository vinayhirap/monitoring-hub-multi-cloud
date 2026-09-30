# tests/test_extended_period_matches_native_resolution.py
"""
2026-09-29: same bug class as test_gmd_period_matches_native_resolution.py
(EC2/EBS core metrics), found while auditing every currently-onboarded
AWS/Azure/GCP service and resource for it, per an explicit request to
check the whole catalog rather than just the one instance already found
live. app/collector/metrics/extended.py's _collect_extended_service()
queried EVERY extended-tier metric across every service (sqs, dynamodb,
ecs, eks, s3, cloudfront, wafv2, certificatemanager, and dozens more)
with a single hardcoded Period=300, regardless of that metric's actual
CloudWatch publish behavior.

Full audit result: safe for the large majority (most AWS services
publish at 1-min resolution; querying coarser than native is normal,
safe aggregation, not the bug direction -- see
CoreMetric.period_sec's docstring in polling_model.py for which
direction actually breaks). Exactly two metrics are genuinely
DAILY-SCHEDULED gauges hitting the same bug class as EC2's CPU:
s3.BucketSizeBytes, s3.NumberOfObjects (AWS-confirmed daily-only
publish), plus certificatemanager.DaysToExpiry (a computed daily
value). Everything else in the slow_extended-tier services (s3's
request metrics, logs, backup, cloudfront, wafv2) is
ACTIVITY-DRIVEN -- it only publishes alongside a real event/request/
job, so there is no fixed native-resolution schedule for Period to
mismatch against in the first place, even for the Average-stat
entries (CloudFront's *ErrorRate/OriginLatency/CacheHitRate): a rate
or latency computed from zero requests in a period is genuinely
undefined, not "zero", so these only publish alongside real traffic
too.

Azure: already correct -- _plan_calls() in
app/providers/azure/metrics_collector.py derives grain per-metric via
_HOURLY_GRAIN_METRICS, not a blanket default. Only storage_account.
UsedCapacity is genuinely hourly-native in the current catalog, and
it's already correctly classified there.

GCP: no currently-onboarded metric is plausibly coarser than 60s by
catalog inspection, so there is no concrete instance to fix. Flagged,
not fixed -- this project has no live GCP access to empirically
confirm GCP's alignment_period mechanism handles a genuine mismatch
the same safe way AWS's core metrics turned out not to.

THE FUTURE-PROOFING THIS FILE EXISTS FOR: test_every_slow_extended_
metric_is_explicitly_classified below walks the REAL, current metric
catalog (app/aws/metric_catalog_data.py's CURATED) and asserts every
metric belonging to a slow_extended-tier service or override is in
EXACTLY ONE of AWS_EXTENDED_PERIOD_OVERRIDES (schedule-driven, needs
its real period) or AWS_EXTENDED_ACTIVITY_DRIVEN_METRICS (activity-
driven, 300 is correct as-is) in polling_model.py. A future metric
added to a slow-tier service that isn't in either set fails this test
immediately with a message telling the author to classify it --
turning "forgot to think about native resolution" into a loud,
specific CI failure instead of a silent multi-hour-offset bug the way
it shipped undetected the first time for EC2/EBS.
"""
import sys

from tests.conftest import load_module, install_stub


def _load_polling_model_and_catalog():
    """No stubbing needed -- both files are pure data/logic with zero
    external imports, so this is cheap and always exercises the real,
    current thing, not a snapshot that can drift from what ships."""
    pm = load_module("app/collector/polling_model.py")
    mcd = load_module("app/aws/metric_catalog_data.py")
    return pm, mcd


def test_every_slow_extended_metric_is_explicitly_classified():
    """THE enforcement test. Walks every metric currently in the
    'extended' category catalog, computes its real tier the same way
    _collect_extended_service does, and requires every slow_extended
    one to be deliberately classified -- not silently defaulted."""
    pm, mcd = _load_polling_model_and_catalog()

    unclassified = []
    for service, (display, namespace, category, metrics) in mcd.CURATED.items():
        if category != "extended" or service == "nlb":
            continue
        for (name, unit, stat, is_default, desc) in metrics:
            if (service, name) in pm.AWS_EXTENDED_UNSUPPORTED:
                continue
            tier = pm.aws_extended_tier(service, name)
            if tier != "slow_extended":
                continue
            in_period_overrides = (service, name) in pm.AWS_EXTENDED_PERIOD_OVERRIDES
            in_activity_driven = (service, name) in pm.AWS_EXTENDED_ACTIVITY_DRIVEN_METRICS
            if in_period_overrides and in_activity_driven:
                unclassified.append(
                    f"{service}.{name}: in BOTH AWS_EXTENDED_PERIOD_OVERRIDES and "
                    f"AWS_EXTENDED_ACTIVITY_DRIVEN_METRICS -- pick one"
                )
            elif not in_period_overrides and not in_activity_driven:
                unclassified.append(
                    f"{service}.{name} (tier=slow_extended, stat={stat}): not classified. "
                    f"Add it to AWS_EXTENDED_PERIOD_OVERRIDES in polling_model.py if it "
                    f"publishes on a fixed coarse schedule (like S3's storage metrics), or "
                    f"to AWS_EXTENDED_ACTIVITY_DRIVEN_METRICS if it only publishes "
                    f"alongside real events/requests/jobs (like CloudFront's or WAF's)."
                )

    assert not unclassified, (
        "Found slow_extended-tier metric(s) with no native-resolution "
        "classification -- see AWS_EXTENDED_PERIOD_OVERRIDES' docstring "
        "in polling_model.py:\n  " + "\n  ".join(unclassified)
    )


def test_known_daily_scheduled_metrics_are_in_period_overrides():
    pm, _ = _load_polling_model_and_catalog()
    assert pm.AWS_EXTENDED_PERIOD_OVERRIDES[("s3", "BucketSizeBytes")] == 86400
    assert pm.AWS_EXTENDED_PERIOD_OVERRIDES[("s3", "NumberOfObjects")] == 86400
    assert pm.AWS_EXTENDED_PERIOD_OVERRIDES[("certificatemanager", "DaysToExpiry")] == 86400
    # Found by the catalog-wide enforcement test below, not by manual
    # review -- proof the enforcement mechanism actually works, not
    # just a list of what I happened to notice by hand.
    assert pm.AWS_EXTENDED_PERIOD_OVERRIDES[("kms", "SecondsUntilKeyMaterialExpiration")] == 86400


def test_aws_extended_period_sec_defaults_to_300_for_unclassified_1min_metrics():
    pm, _ = _load_polling_model_and_catalog()
    assert pm.aws_extended_period_sec("dynamodb", "ThrottledRequests") == 300
    assert pm.aws_extended_period_sec("ecs", "CPUUtilization") == 300


def _load_extended_module():
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.aws.sts", get_boto3_session=lambda *a, **k: None)
    install_stub("app.aws.boto_config", STANDARD_RETRY=None, CONCURRENT_CLIENT_RETRY=None)
    install_stub("app.collector.disk_mounts", all_cwagent_disk_dims=lambda cw, iid: [],
                 ensure_disk_mount_metric_registered=lambda *a, **k: None)
    install_stub("app.collector.metrics_writer", write_metrics_batch=lambda rows: None,
                 write_metric_history_batch=lambda rows, **kw: None)
    install_stub("app.collector.api_usage", record=lambda *a, **k: None)

    sys.modules["app.utils.time_json"] = load_module("app/utils/time_json.py")

    real_polling_model = load_module("app/collector/polling_model.py")
    sys.modules["app.collector.polling_model"] = real_polling_model
    setattr(sys.modules["app.collector"], "polling_model", real_polling_model)

    sys.modules["app.threshold_defaults"] = load_module("app/threshold_defaults.py")
    sys.modules["app.aws.metric_catalog_data"] = load_module("app/aws/metric_catalog_data.py")

    real_runner = load_module("app/collector/metrics/runner.py")
    sys.modules["app.collector.metrics.runner"] = real_runner

    return load_module("app/collector/metrics/extended.py")


class _CapturingCloudWatch:
    """Records every MetricDataQueries list passed to get_metric_data,
    returns empty results (this test only cares what was ASKED for,
    not what comes back)."""
    def __init__(self):
        self.captured_queries = []

    def get_metric_data(self, MetricDataQueries, **kwargs):
        self.captured_queries.extend(MetricDataQueries)
        return {"MetricDataResults": [
            {"Id": q["Id"], "Values": [], "Timestamps": []} for q in MetricDataQueries
        ]}


def test_s3_storage_metrics_actually_query_at_86400_not_300():
    """End-to-end proof, not just a catalog assertion: the REAL
    _collect_extended_service, with the REAL EXTENDED_METRICS built
    from the REAL catalog, sends the corrected Period to CloudWatch."""
    ext = _load_extended_module()
    cw = _CapturingCloudWatch()
    resources = [{"id": 1, "resource_id": "my-bucket", "resource_type": "s3",
                  "name": "my-bucket", "region": "ap-south-1", "tags": "{}"}]
    enabled = {("s3", "BucketSizeBytes"), ("s3", "NumberOfObjects")}

    ext._collect_extended_service(cw, resources, "s3", enabled, minutes=1470, tier="slow_extended")

    periods = {q["MetricStat"]["Metric"]["MetricName"]: q["MetricStat"]["Period"]
               for q in cw.captured_queries}
    assert periods["BucketSizeBytes"] == 86400
    assert periods["NumberOfObjects"] == 86400


def test_dynamodb_extended_metric_still_queries_at_300_unaffected():
    """Regression guard: the fix must not touch genuinely 1-min-native
    extended metrics."""
    ext = _load_extended_module()
    cw = _CapturingCloudWatch()
    resources = [{"id": 1, "resource_id": "my-table", "resource_type": "dynamodb",
                  "name": "my-table", "region": "ap-south-1", "tags": "{}"}]
    enabled = {("dynamodb", "ReadThrottleEvents")}

    ext._collect_extended_service(cw, resources, "dynamodb", enabled, minutes=8, tier="standard")

    periods = {q["MetricStat"]["Metric"]["MetricName"]: q["MetricStat"]["Period"]
               for q in cw.captured_queries}
    assert periods["ReadThrottleEvents"] == 300
