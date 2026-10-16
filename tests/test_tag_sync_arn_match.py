# tests/test_tag_sync_arn_match.py
"""Pure tests for app/aws/arn_match.py (workstream B): ARN parsing, per-type ARN <-> resources-row matching,
and the rule that "untagged" is only ever said from a completed sweep for a verified type."""
import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from app.aws import arn_match as am  # noqa: E402
from app import environment_classifier as ec  # noqa: E402

ACCT = "123456789012"
REG = "ap-south-1"

# type -> (row resource_id as DISCOVERY stores it, row region, the ARN GetResources returns for it)
CASES = {
    "s3": ("my-bucket", "ap-south-1", "arn:aws:s3:::my-bucket"),
    "logs": ("/aws/lambda/orders", REG, f"arn:aws:logs:{REG}:{ACCT}:log-group:/aws/lambda/orders"),
    "kms": ("1234abcd-12ab-34cd-56ef-1234567890ab", REG,
            f"arn:aws:kms:{REG}:{ACCT}:key/1234abcd-12ab-34cd-56ef-1234567890ab"),
    "lambda": (f"arn:aws:lambda:{REG}:{ACCT}:function:orders-api", REG,
               f"arn:aws:lambda:{REG}:{ACCT}:function:orders-api"),
    "elb": (f"arn:aws:elasticloadbalancing:{REG}:{ACCT}:loadbalancer/app/web-alb/50dc6c495c0c9188", REG,
            f"arn:aws:elasticloadbalancing:{REG}:{ACCT}:loadbalancer/app/web-alb/50dc6c495c0c9188"),
    "sqs": ("orders-queue", REG, f"arn:aws:sqs:{REG}:{ACCT}:orders-queue"),
    "sns": ("alerts-topic", REG, f"arn:aws:sns:{REG}:{ACCT}:alerts-topic"),
    "natgateway": ("nat-0a1b2c3d4e5f60718", REG, f"arn:aws:ec2:{REG}:{ACCT}:natgateway/nat-0a1b2c3d4e5f60718"),
    "certificatemanager": (f"arn:aws:acm:{REG}:{ACCT}:certificate/9f3c1c2e-aaaa-bbbb-cccc-0123456789ab", REG,
                           f"arn:aws:acm:{REG}:{ACCT}:certificate/9f3c1c2e-aaaa-bbbb-cccc-0123456789ab"),
    "cloudfront": ("E2ABCDEFGHIJKL", "global", f"arn:aws:cloudfront::{ACCT}:distribution/E2ABCDEFGHIJKL"),
    "kinesis": ("clickstream", REG, f"arn:aws:kinesis:{REG}:{ACCT}:stream/clickstream"),
    "wafv2": ("web-acl-prod", REG, f"arn:aws:wafv2:{REG}:{ACCT}:regional/webacl/web-acl-prod/a1b2c3d4-1111-2222-3333-444455556666"),
    "backup": ("Default", REG, f"arn:aws:backup:{REG}:{ACCT}:backup-vault:Default"),
    "events": ("nightly-job", REG, f"arn:aws:events:{REG}:{ACCT}:rule/nightly-job"),
    "dynamodb": ("Orders", REG, f"arn:aws:dynamodb:{REG}:{ACCT}:table/Orders"),
    "eks": ("prod-cluster", REG, f"arn:aws:eks:{REG}:{ACCT}:cluster/prod-cluster"),
    "firehose": ("logs-delivery", REG, f"arn:aws:firehose:{REG}:{ACCT}:deliverystream/logs-delivery"),
}
# a resource of a type no test row uses, so a sweep is never empty and never trips the per-type canary
FILLER = f"arn:aws:states:{REG}:{ACCT}:stateMachine:unrelated"


def row(rtype, rid, region, pk=1, tags=None):
    return {"id": pk, "resource_type": rtype, "resource_id": rid, "region": region, "tags": tags or {}}


def sweep(items, region=REG, ok=True):
    return {"region": region, "ok": ok, "items": [{"arn": a, "tags": t} for a, t in items]}


def plan(rows, sweeps, **kw):
    return am.plan_account(rows, sweeps, ACCT, **kw)


def test_every_listed_type_has_a_case():
    assert set(CASES) == set(am.SYNC_TYPES)


def test_classifier_and_matcher_agree_on_the_synced_types():
    assert ec.TAG_SYNC_TYPES == am.SYNC_TYPES


def test_absence_list_is_a_subset_and_leaves_out_the_unverified_types():
    assert am.ABSENCE_VERIFIED_TYPES <= am.SYNC_TYPES
    assert not ({"s3", "cloudfront", "wafv2"} & am.ABSENCE_VERIFIED_TYPES)


@pytest.mark.parametrize("rtype", sorted(CASES))
def test_a_matching_arn_proves_the_rows_tags(rtype):
    rid, region, arn = CASES[rtype]
    sweeps = [sweep([(arn, {"Environment": "prod", "Owner": "team-a", "aws:cloudformation:stack-name": "x"})],
                    region="us-east-1" if rtype in am.GLOBAL_TYPES else REG)]
    out = plan([row(rtype, rid, region)], sweeps)
    d = out["decisions"][1]
    assert d.status == "tagged"
    assert d.tags == {"Environment": "prod", "Owner": "team-a"}            # aws: system tags are dropped
    assert out["types"][rtype]["matched"] == 1 and not out["types"][rtype]["unmatched_arns"]


@pytest.mark.parametrize("rtype", sorted(am.ABSENCE_VERIFIED_TYPES))
def test_verified_type_absent_from_a_completed_sweep_is_untagged(rtype):
    rid, region, _ = CASES[rtype]
    out = plan([row(rtype, rid, region)], [sweep([(FILLER, {"k": "v"})])])
    assert out["decisions"][1].status == "absent" and out["decisions"][1].tags == {}
    assert out["types"][rtype]["absent"] == 1


@pytest.mark.parametrize("rtype", sorted(set(am.SYNC_TYPES) - set(am.ABSENCE_VERIFIED_TYPES)))
def test_unverified_type_stays_unknown_even_when_the_sweep_succeeded_and_it_is_absent(rtype):
    rid, region, _ = CASES[rtype]
    out = plan([row(rtype, rid, region)], [sweep([(FILLER, {"k": "v"})]), sweep([(FILLER, {})], region="us-east-1")])
    assert 1 not in out["decisions"]
    assert out["types"][rtype]["unknown"] == 1 and out["types"][rtype]["absence_blocked"]


def test_config_can_only_narrow_the_absence_list():
    rid, region, _ = CASES["logs"]
    r = row("logs", rid, region)
    assert 1 not in plan([r], [sweep([(FILLER, {})])], absence_types=frozenset())["decisions"]
    assert plan([r], [sweep([(FILLER, {})])], absence_types={"logs"})["decisions"][1].status == "absent"
    # asking for an unverified type adds nothing
    s3r = row("s3", "b", "ap-south-1")
    assert 1 not in plan([s3r], [sweep([(FILLER, {})])], absence_types={"s3"})["decisions"]


@pytest.mark.parametrize("kind", ["failed", "partial"])
def test_a_failed_sweep_decides_nothing(kind):
    rid, region, arn = CASES["sqs"]
    sw = sweep([(arn, {"Environment": "prod"})], ok=False)       # AccessDenied / throttled / a page missing
    out = plan([row("sqs", rid, region)], [sw])
    assert out["decisions"] == {}                                # not even the tagged item from a partial read is trusted


def test_an_empty_sweep_claims_nothing():
    rid, region, _ = CASES["sqs"]
    assert plan([row("sqs", rid, region)], [sweep([])])["decisions"] == {}


def test_row_in_a_region_that_was_not_swept_is_unknown():
    rid, _, _ = CASES["sqs"]
    out = plan([row("sqs", rid, "eu-west-1")], [sweep([(FILLER, {})])])
    assert out["decisions"] == {}


def test_canary_blocks_untagged_when_arns_of_the_type_exist_but_none_matched():
    # discovery stored 'q1'; the sweep knows 'orders-queue' only. If the mapping were wrong this is what it would look like.
    out = plan([row("sqs", "q1", REG)], [sweep([(f"arn:aws:sqs:{REG}:{ACCT}:orders-queue", {"a": "b"})])])
    assert out["decisions"] == {}
    assert out["types"]["sqs"]["unmatched_arns"] and any("mapping suspect" in b for b in out["types"]["sqs"]["absence_blocked"])


def test_canary_passes_when_at_least_one_row_of_the_type_matched():
    rows = [row("sqs", "q1", REG, pk=1), row("sqs", "q2", REG, pk=2)]
    out = plan(rows, [sweep([(f"arn:aws:sqs:{REG}:{ACCT}:q1", {"a": "b"})])])
    assert out["decisions"][1].status == "tagged" and out["decisions"][2].status == "absent"


def test_a_tagged_resource_whose_row_id_cannot_be_keyed_is_never_reported_missing():
    # a lambda row holding a bare name (discovery stores the ARN) must not turn the tagged function into 'missing'
    out = plan([row("lambda", "orders-api", REG)], [sweep([(CASES["lambda"][2], {"Environment": "prod"}), (FILLER, {})])])
    assert out["decisions"] == {}
    assert out["types"]["lambda"]["unknown"] == 1


def test_an_arn_for_another_account_never_matches():
    rid, region, _ = CASES["sqs"]
    other = f"arn:aws:sqs:{REG}:999999999999:{rid}"
    out = plan([row("sqs", rid, region)], [sweep([(other, {"Environment": "prod"}), (FILLER, {})])])
    assert out["foreign_arns"] == [other] and out["decisions"][1].status == "absent"  # foreign ARN is not our row


@pytest.mark.parametrize("arn", [
    f"arn:aws:lambda:{REG}:{ACCT}:function:orders-api:live",              # alias / version
    f"arn:aws:lambda:{REG}:{ACCT}:function:orders-api:7",
    f"arn:aws:sns:{REG}:{ACCT}:alerts-topic:6b0e71bd-7e97-4d97-80ce-4a0994e55286",   # subscription
    f"arn:aws:kinesis:{REG}:{ACCT}:stream/clickstream/consumer/c1:1700000000",
    f"arn:aws:dynamodb:{REG}:{ACCT}:table/Orders/index/by-user",
    f"arn:aws:events:{REG}:{ACCT}:rule/custom-bus/nightly-job",           # custom event bus
    f"arn:aws:kms:{REG}:{ACCT}:alias/my-alias",
    f"arn:aws:elasticloadbalancing:{REG}:{ACCT}:targetgroup/tg/73e2d6bc24d8a067",
    f"arn:aws:elasticloadbalancing:{REG}:{ACCT}:loadbalancer/classic-lb",
    f"arn:aws:wafv2:us-east-1:{ACCT}:global/webacl/web-acl-prod/a1b2c3d4-1111-2222-3333-444455556666",
    "arn:aws:s3:::my-bucket/object.txt",
    "not-an-arn",
    "",
])
def test_arns_that_are_not_one_of_our_rows_are_refused(arn):
    assert am.arn_key(arn) is None


def test_log_group_arn_with_wildcard_suffix_matches():
    k, _ = am.arn_key(f"arn:aws:logs:{REG}:{ACCT}:log-group:/aws/rds/instance/db1/error:*")
    assert k.name == "/aws/rds/instance/db1/error"


def test_two_rows_with_the_same_key_are_both_refused():
    rows = [row("sqs", "dup", REG, pk=1), row("sqs", "dup", REG, pk=2)]
    out = plan(rows, [sweep([(f"arn:aws:sqs:{REG}:{ACCT}:dup", {"a": "b"})])])
    assert out["decisions"] == {} and out["types"]["sqs"]["ambiguous"]


def test_the_same_arn_with_different_tags_is_refused():
    arn = f"arn:aws:sqs:{REG}:{ACCT}:q1"
    out = plan([row("sqs", "q1", REG)], [sweep([(arn, {"a": "1"}), (arn, {"a": "2"})])])
    assert out["decisions"] == {} and out["types"]["sqs"]["ambiguous"]


def test_the_same_arn_seen_twice_with_equal_tags_matches():
    arn = "arn:aws:s3:::my-bucket"
    out = plan([row("s3", "my-bucket", "ap-south-1")],
               [sweep([(arn, {"Environment": "dev"})], region=REG), sweep([(arn, {"Environment": "dev"})], region="us-east-1")])
    assert out["decisions"][1].tags == {"Environment": "dev"}


def test_wafv2_acl_id_must_agree_with_the_stored_one():
    arn = CASES["wafv2"][2]
    good = plan([row("wafv2", "web-acl-prod", REG, tags={"acl_id": "a1b2c3d4-1111-2222-3333-444455556666"})],
                [sweep([(arn, {"Owner": "x"})])])
    assert good["decisions"][1].status == "tagged"
    bad = plan([row("wafv2", "web-acl-prod", REG, tags='{"acl_id": "ffffffff-0000-0000-0000-000000000000"}')],
               [sweep([(arn, {"Owner": "x"}), (FILLER, {})])])
    assert bad["decisions"] == {} and bad["types"]["wafv2"]["ambiguous"]


def test_unmatched_arns_and_rows_are_reported_for_the_report_script():
    rows = [row("sqs", "q1", REG, pk=1)]
    out = plan(rows, [sweep([(f"arn:aws:sqs:{REG}:{ACCT}:q1", {}), (f"arn:aws:sqs:{REG}:{ACCT}:ghost", {"a": "b"})])])
    assert out["types"]["sqs"]["unmatched_arns"] == [f"arn:aws:sqs:{REG}:{ACCT}:ghost"]
    out2 = plan([row("sqs", "q1", REG)], [sweep([(f"arn:aws:sqs:{REG}:{ACCT}:ghost", {"a": "b"})])])
    assert out2["types"]["sqs"]["unmatched_rows"] == [(1, "q1")]


def test_existing_tag_aware_and_unlisted_types_are_not_touched():
    rows = [row("ec2", "i-0abc", REG, pk=1), row("rds", "db1", REG, pk=2), row("dms", "t1", REG, pk=3), row("ebs", "vol-1", REG, pk=4)]
    out = plan(rows, [sweep([(FILLER, {})])])
    assert out["decisions"] == {}
