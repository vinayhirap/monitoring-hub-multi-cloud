# tests/test_event_source_resource_id.py
"""
Covers app/collector/discovery/runner.py's _event_source_resource_id(),
added alongside the Lambda event-source-mapping topology auto-sync
(roadmap phase 4/7 follow-up, 2026-09-13).

Why this needs its own test rather than just eyeballing the ARN
parsing: EventSourceArn is a full ARN, but resources.resource_id for
SQS/DynamoDB/Kinesis is stored as a bare name (see _discover_sqs/
_discover_dynamodb/_discover_kinesis in this same file) -- getting the
normalization wrong silently produces a "topology edge" that can never
resolve to a real node (every single one renders as a ghost/unresolved
node instead), which wouldn't fail loudly anywhere, just quietly never
work. Worth pinning down with real ARN shapes rather than trusting the
string-splitting logic by inspection alone.
"""
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.conftest import load_module, install_stub


def _load():
    install_stub("app.db", get_connection=lambda: None)
    install_stub("app.aws.sts", get_boto3_session=lambda account: None)
    return load_module("app/collector/discovery/runner.py")


def test_sqs_arn():
    mod = _load()
    assert mod._event_source_resource_id("arn:aws:sqs:ap-south-1:924922671984:my-queue") == "my-queue"


def test_sqs_fifo_arn_keeps_suffix():
    mod = _load()
    assert mod._event_source_resource_id("arn:aws:sqs:ap-south-1:924922671984:my-queue.fifo") == "my-queue.fifo"


def test_kinesis_arn():
    mod = _load()
    assert mod._event_source_resource_id("arn:aws:kinesis:ap-south-1:924922671984:stream/my-stream") == "my-stream"


def test_dynamodb_stream_arn():
    mod = _load()
    arn = "arn:aws:dynamodb:ap-south-1:924922671984:table/my-table/stream/2026-09-13T00:00:00.000"
    assert mod._event_source_resource_id(arn) == "my-table"


def test_unrecognized_service_returns_none():
    mod = _load()
    # e.g. MSK / self-managed Kafka -- not a tracked resource type in
    # this app, so there's nothing for an edge to resolve against;
    # must return None rather than a wrong guess.
    assert mod._event_source_resource_id("arn:aws:secretsmanager:ap-south-1:924922671984:secret:foo") is None


def test_garbage_input_returns_none():
    mod = _load()
    assert mod._event_source_resource_id("not-an-arn") is None
    assert mod._event_source_resource_id(None) is None
    assert mod._event_source_resource_id("") is None
