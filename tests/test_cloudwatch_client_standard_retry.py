# tests/test_cloudwatch_client_standard_retry.py
"""
Regression tests for three CloudWatch client construction sites that
were missing `config=STANDARD_RETRY` (botocore's adaptive retry mode
-- see app/aws/boto_config.py's docstring for why this matters under
this app's actual GetMetricData call volume). Every OTHER
`.client("cloudwatch", ...)` site in the app already passes it; these
three were plain oversights, found by grepping every call site after
fixing the unrelated S3 connection-pool bug in the same area.

Source-inspected directly against the file text rather than loaded
through each file's own (differently-shaped) test-isolation stubs --
that's simpler and sufficient here, since this is a "the config kwarg
is present at this call" check, not a behavioural one.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _source(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text()


def test_ecs_raw_cloudwatch_client_uses_standard_retry():
    src = _source("app/aws/collector_direct.py")
    assert 'cw  = session.client("cloudwatch", config=STANDARD_RETRY)' in src, (
        "_ecs_raw's CloudWatch client must pass config=STANDARD_RETRY, "
        "matching every sibling client construction in this file -- "
        "found missing it while auditing the S3 connection-pool fix"
    )


def test_metrics_runner_cloudwatch_client_uses_standard_retry():
    src = _source("app/collector/metrics/runner.py")
    assert "from app.aws.boto_config import STANDARD_RETRY" in src
    assert (
        'cw = session.client("cloudwatch", region_name=res_region, config=STANDARD_RETRY)'
        in src
    ), (
        "This is the CloudWatch client behind EC2/EBS/RDS/ELB/Lambda "
        "critical/standard/low-tier metric collection -- the app's "
        "actual highest-volume GetMetricData path -- and it must use "
        "adaptive retry, not botocore's legacy default"
    )


def test_metrics_extended_cloudwatch_client_uses_standard_retry():
    src = _source("app/collector/metrics/extended.py")
    assert "from app.aws.boto_config import STANDARD_RETRY" in src
    assert (
        'cw = session.client("cloudwatch", region_name=cw_region, config=STANDARD_RETRY)'
        in src
    )
