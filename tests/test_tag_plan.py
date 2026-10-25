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
    # the block is a function that only the matching account's credentials can reach
    assert "tag_account_123456789012() {" in s
    assert 'CURRENT_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"' in s
    assert "123456789012) echo" in s and "which has no block in this script" in s and "exit 1" in s
    assert "set -euo pipefail" in s


def test_only_evidence_based_sources_are_exported_by_default():
    rows = [row("i-0000000a", src="manual"), row("i-0000000b", src="name"), row("i-0000000c", src="related"),
            row("i-0000000d", src="account"), row("i-0000000e", env="prod", src="unknown")]
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
    rows = [row("i-0000000a", env="nonprod"),
            row("vol-0000000a", "ebs", env="nonprod", state="not_checked", tags={"parent_ec2": "i-0000000a"}),
            row("vol-0000000b", "ebs", env="prod", state="missing", tags={"_ebs_state": "available"}),
            row("vol-0000000c", "ebs", env="prod", state="not_checked", tags={"parent_ec2": "i-0000000z"})]
    s = plan(rows)["script"]
    assert "vol-0000000a" in s and "Value=nonprod" in s
    assert "vol-0000000b" in s and "Value=prod" in s
    assert "vol-0000000c" not in s            # its instance is not being exported


def test_groups_by_account_region_and_environment_and_batches_at_100():
    rows = [row(f"i-{n:08x}") for n in range(250)]
    rows += [row("i-0000ffff", env="nonprod"), row("i-0001ffff", region="us-east-1"),
             row("i-0002ffff", acct=2, aws="210987654321", name="other")]
    p = plan(rows)
    assert p["commands"] == 3 + 1 + 1 + 1 and p["accounts"] == 2
    assert "210987654321) echo" in p["script"] and "tag_account_210987654321() {" in p["script"]
    assert p["by_env"] == {"prod": 251 + 1, "nonprod": 1}
    assert {"account_id": "123456789012", "account_name": "acct", "region": "ap-south-1", "env": "prod", "count": 250} in p["groups"]


def test_unsafe_values_never_reach_the_script():
    rows = [row("i-0abc12345; rm -rf /"), row("i-0abc12345", region="ap-south-1; id"),
            row("i-0abc12345", aws="12345"), row("$(whoami)")]
    p = plan(rows)
    assert p["commands"] == 0 and p["skipped_invalid"] >= 1
    for bad in ("rm -rf", "whoami", "ap-south-1; id", "`"):
        assert bad not in p["script"]
    assert not [l for l in p["script"].split("\n") if l.strip().startswith("run aws ")]


def test_account_name_cannot_break_out_of_its_comment_line():
    s = plan([row("i-0abc12345", name='Evil"; rm -rf / $(id) `x` #\ninjected')])["script"]
    lines = [l for l in s.split("\n") if "Evil" in l]
    assert lines                                    # the header comment and the echo
    for l in lines:
        name = l[l.index("Evil"):l.index("injected") + len("injected")]
        assert not any(c in name for c in '"\';`$/\\#\n'), name       # nothing that can end a quote or a comment
    assert not [l for l in s.split("\n") if l.startswith("injected") or l.startswith("rm ")]


def test_other_types_are_counted_not_exported():
    p = plan([row("db-1", "rds"), row("bucket", "s3"), row("i-0abc12345")])
    assert p["not_covered"] == {"rds": 1, "s3": 1}
    assert "Not covered by this script" in p["script"] and "db-1" not in p["script"].split("Not covered")[0]


def test_empty_plan_says_so():
    p = plan([row("i-0abc12345", state="ok")])
    assert p["commands"] == 0 and "Nothing to export" in p["script"]


def test_script_is_valid_bash_and_dry_run_prints_instead_of_running():
    import shutil, subprocess, tempfile, os
    if not shutil.which("bash"):
        return
    p = plan([row("i-0abc12345", name="web 1"), row("i-0abc12346")])
    f = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    f.write(p["script"]); f.close()
    try:
        assert subprocess.run(["bash", "-n", f.name]).returncode == 0
        fake = tempfile.mkdtemp()
        aws = os.path.join(fake, "aws")           # a stand-in aws that answers sts and records everything else
        open(aws, "w").write("#!/bin/sh\nif [ \"$1\" = sts ]; then echo 123456789012; else echo CALLED \"$@\" >> " + fake + "/calls; fi\n")
        os.chmod(aws, 0o755)
        env = dict(os.environ, PATH=fake + ":" + os.environ["PATH"])
        out = subprocess.run(["bash", f.name], env=dict(env, DRY_RUN="1"), capture_output=True, text=True)
        assert out.returncode == 0 and "[dry run] aws ec2 create-tags" in out.stdout
        assert not os.path.exists(fake + "/calls")             # dry run touched nothing
        out = subprocess.run(["bash", f.name], env=env, capture_output=True, text=True)
        assert out.returncode == 0 and "CALLED ec2 create-tags" in open(fake + "/calls").read()
        open(aws, "w").write("#!/bin/sh\necho 999999999999\n")  # credentials of another account: nothing runs
        out = subprocess.run(["bash", f.name], env=env, capture_output=True, text=True)
        assert out.returncode == 1 and "no block in this script" in out.stdout
    finally:
        os.unlink(f.name)


def test_header_says_where_to_run_and_summarises_by_environment():
    s = plan([row("i-0000000a"), row("i-0000000b", env="nonprod"), row("i-0000000c", env="nonprod")])["script"]
    assert "WHERE TO RUN IT" in s and "AWS CloudShell" in s and "ec2:CreateTags" in s and "DRY_RUN=1" in s
    assert "SUMMARY  (nonprod 2, prod 1; 3 resource(s) in 1 account(s))" in s
    assert "i-0000000b" in s.split("set -euo")[0] or "  #   i-0000000b" in s    # names/ids listed as review comments


def test_filters_apply_after_volumes_are_matched_to_their_instance():
    rows = [row("i-0000000a", env="prod"), row("i-0000000b", env="nonprod"),
            row("vol-0000000a", "ebs", env="prod", tags={"parent_ec2": "i-0000000a"}),
            row("i-0000000c", env="prod", region="us-east-1")]
    assert plan(rows, env="nonprod")["resources"] == 1
    assert plan(rows, resource_type="ebs")["resources"] == 1
    assert plan(rows, region="us-east-1")["resources"] == 1
    p = plan(rows, env="prod", region="ap-south-1")
    assert p["resources"] == 2 and "i-0000000b" not in p["script"] and "us-east-1" not in p["script"]
