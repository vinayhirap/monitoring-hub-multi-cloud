# tests/test_tag_sync_report.py
"""scripts/tag_sync_report.py: works from a recorded sweep (no AWS, no database) and is read-only."""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
ROOT = __file__.rsplit("/tests/", 1)[0]
SCRIPT = os.path.join(ROOT, "scripts", "tag_sync_report.py")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "tag_sync_report_fixture.json")


def _run(*args):
    env = dict(os.environ)
    env.pop("DB_PASSWORD", None)            # fixtures mode must not need a database
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env, cwd=ROOT)


def test_fixtures_mode_runs_without_aws_or_database_and_says_it_wrote_nothing():
    p = _run("--fixtures", FIXTURE)
    assert p.returncode == 0, p.stderr
    assert "READ-ONLY: nothing was written" in p.stdout
    assert "ARN not matched to a row: arn:aws:sqs:ap-south-1:123456789012:deleted-queue" in p.stdout
    assert "row not matched: id=8 resource_id=hcs-logs" in p.stdout


def test_json_output_has_the_per_type_numbers():
    p = _run("--fixtures", FIXTURE, "--json")
    data = json.loads(p.stdout)["AuroGov (fixture)"]
    sqs = data["types"]["sqs"]
    assert (sqs["rows"], sqs["matched"], sqs["absent"]) == (2, 1, 1)
    assert sqs["tag_state"]["ok"] == 1 and sqs["tag_state"]["missing"] == 1
    assert data["types"]["s3"]["may_claim_untagged"] is False and data["types"]["s3"]["tag_state"]["not_checked"] == 1


def test_script_contains_no_write_statements():
    src = open(SCRIPT).read()
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE|ALTER|DROP|TRUNCATE)\b", code)
    assert "executemany" not in code and "tag_resources" not in code and "untag_resources" not in code
