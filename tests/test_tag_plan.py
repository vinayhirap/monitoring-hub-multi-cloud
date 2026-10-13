# tests/test_tag_plan.py -- the generated AWS tagging script (app/collector/tag_plan.py).
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from datetime import datetime, timezone

from tests.conftest import load_module

tp = load_module("app/collector/tag_plan.py")
NOW = datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc)


def row(rid, rtype="ec2", env="prod", src="manual", state="missing", acct=1, region="ap-south-1",
        tags=None, aws="123456789012", name="acct"):
    return {"id": hash((rid, acct)) % 10**6, "aws_account_id": acct, "account_id": aws, "account_name": name,
            "resource_type": rtype, "resource_id": rid, "region": region, "tags": tags or {},
            "env_resolved": env, "env_source": src, "tag_state": state}


def plan(rows, **kw):
    return tp.build_tag_plan(rows, now=NOW, **kw)


def test_exports_a_create_tags_command_that_touches_only_the_environment_key():
    p = plan([row("i-0abc12345")])
    assert "aws ec2 create-tags --region ap-south-1 --resources i-0abc12345 --tags Key=Environment,Value=prod" in p["script"]
    assert "--tags Key=Environment,Value=prod\n" in p["script"]
    assert p["resources"] == 1 and p["commands"] == 1 and p["accounts"] == 1


def test_guards_against_running_in_the_wrong_account():
    s = plan([row("i-0abc12345")])["script"]
    assert 'aws sts get-caller-identity --query Account --output text)" = "123456789012"' in s
    assert "set -euo pipefail" in s


def test_only_evidence_based_sources_are_exported_by_default():
    rows = [row("i-0000000a", src="manual"), row("i-0000000b", src="name"), row("i-0000000c", src="related"),
            row("i-0000000d", src="account"), row("i-0000000e", env="unknown", src="unknown")]
    p = plan(rows)
    assert p["resources"] == 3
    assert "i-0000000d" not in p["script"] and "i-0000000e" not in p["script"]
    assert plan(rows, sources=("account",))["resources"] == 1


def test_resources_that_are_already_tagged_to_standard_or_not_checked_are_left_out():
    rows = [row("i-0000000a", state="ok"), row("i-0000000b", state="not_checked"), row("i-0000000c", state="nonstandard"),
            row("i-0000000d", state="invalid"), row("i-0000000e", state="missing")]
    s = plan(rows)["script"]
    assert "i-0000000a" not in s and "i-0000000b" not in s
    assert all(x in s for x in ("i-0000000c", "i-0000000d", "i-0000000e"))


def test_attached_volumes_follow_their_instance_and_unattached_ones_use_their_own_state():
    rows = [row("i-0000000a", env="dev"),
            row("vol-0000000a", "ebs", env="dev", state="not_checked", tags={"parent_ec2": "i-0000000a"}),
            row("vol-0000000b", "ebs", env="uat", state="missing", tags={"_ebs_state": "available"}),
            row("vol-0000000c", "ebs", env="prod", state="not_checked", tags={"parent_ec2": "i-0000000z"})]
    s = plan(rows)["script"]
    assert "vol-0000000a" in s and "Value=dev" in s
    assert "vol-0000000b" in s and "Value=uat" in s
    assert "vol-0000000c" not in s            # its instance is not being exported


def test_groups_by_account_region_and_environment_and_batches_at_100():
    rows = [row(f"i-{n:08x}") for n in range(250)]
    rows += [row("i-0000ffff", env="dev"), row("i-0001ffff", region="us-east-1"),
             row("i-0002ffff", acct=2, aws="210987654321", name="other")]
    p = plan(rows)
    assert p["commands"] == 3 + 1 + 1 + 1 and p["accounts"] == 2
    assert "= \"210987654321\"" in p["script"]


def test_unsafe_values_never_reach_the_script():
    rows = [row("i-0abc12345; rm -rf /"), row("i-0abc12345", region="ap-south-1; id"),
            row("i-0abc12345", aws="12345"), row("$(whoami)")]
    p = plan(rows)
    assert p["commands"] == 0 and p["skipped_invalid"] >= 1
    for bad in ("rm -rf", "whoami", "ap-south-1; id", "`"):
        assert bad not in p["script"]
    assert not [l for l in p["script"].split("\n") if l.startswith("aws ")]


def test_account_name_cannot_break_out_of_its_comment_line():
    s = plan([row("i-0abc12345", name='Evil"; rm -rf / $(id) `x` #\ninjected')])["script"]
    line = [l for l in s.split("\n") if l.startswith("# ===== Account")][0]
    assert not any(c in line for c in '";`$/\\')
    assert "injected" not in [l.strip() for l in s.split("\n") if not l.startswith("#")]


def test_other_types_are_counted_not_exported():
    p = plan([row("db-1", "rds"), row("bucket", "s3"), row("i-0abc12345")])
    assert p["not_covered"] == {"rds": 1, "s3": 1}
    assert "Not covered by this script" in p["script"] and "db-1" not in p["script"].split("Not covered")[0]


def test_empty_plan_says_so():
    p = plan([row("i-0abc12345", state="ok")])
    assert p["commands"] == 0 and "Nothing to export" in p["script"]
