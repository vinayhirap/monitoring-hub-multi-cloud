# tests/test_tag_sync_integration.py
"""
REAL-DATABASE tests for the tag sync (MH_TEST_DB=1; same scratch DB as test_alert_lifecycle_integration.py, with
migrations 074..083, 086 and 095 applied on top of tests/fixtures/alerts_test_schema.sql).
"""
import json
import os

import pytest

from tests.conftest import install_stub, load_module
from tests.test_alert_lifecycle_integration import _connect, db  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(not os.getenv("MH_TEST_DB"), reason="set MH_TEST_DB=1 to run real-DB tests")

REG = "ap-south-1"
NUM = "1002"          # the fixture gives account 2 the number "1002"


def _setup(db):
    db.x("UPDATE aws_accounts SET default_region=%s WHERE id=2", (REG,))
    for rtype, rid in (("sqs", "orders"), ("sqs", "legacy"), ("kms", "key-1"), ("s3", "bucket-a")):
        db.resource(2, rtype, rid)
    db.x("UPDATE resources SET region=%s WHERE aws_account_id=2", (REG,))


def _sync(monkeypatch, items, ok=True, dry_run=False):
    monkeypatch.setenv("TAG_SYNC_ENABLED", "true")
    install_stub("app.db", get_connection=_connect)
    ts = load_module("app/collector/tag_sync.py")

    def accounts():
        c = _connect()
        try:
            cur = c.cursor(dictionary=True)
            cur.execute("SELECT id, account_name, account_id, default_region FROM aws_accounts WHERE id = 2")
            return cur.fetchall()
        finally:
            c.close()
    ts._get_accounts = accounts
    ts._get_session = lambda a: object()
    sweeps = []

    def sweeper(account, spec):
        region = spec["region"]
        sweeps.append(region)
        return {"region": region, "scope": spec["scope"], "ok": ok, "pages": 1,
                "error": None if ok else RuntimeError("Throttling"),
                "items": [{"arn": a, "tags": t} for a, t in (items if ok else [])]}
    rep = ts.run_tag_sync(sweeper=sweeper, dry_run=dry_run)
    return rep, sweeps


def _classify():
    install_stub("app.db", get_connection=_connect)
    return load_module("app/collector/env_sync.py").sync_resource_environments()


def _row(db, rid):
    return db.one("SELECT tag_state, synced_tags, tags_synced_at FROM resources WHERE aws_account_id=2 AND resource_id=%s", (rid,))


ARNS = [(f"arn:aws:sqs:{REG}:{NUM}:orders", {"Environment": "prod", "aws:cloudformation:stack-name": "s"}),
        (f"arn:aws:kms:{REG}:{NUM}:key/key-1", {"Owner": "platform"})]


def test_sync_then_classify_gives_real_status_and_flag_off_returns_to_not_checked(db, monkeypatch):
    _setup(db)
    rep, sweeps = _sync(monkeypatch, ARNS)
    assert sweeps == [REG, "us-east-1"]            # the account has an S3 row, so the global sweep runs too
    assert rep["written"] == 3                     # orders, legacy (absent), key-1; s3 is never called untagged
    assert json.loads(_row(db, "orders")["synced_tags"]) == {"Environment": "prod"}
    assert json.loads(_row(db, "legacy")["synced_tags"]) == {}
    assert _row(db, "bucket-a")["synced_tags"] is None

    _classify()
    assert _row(db, "orders")["tag_state"] == "ok"
    assert _row(db, "legacy")["tag_state"] == "missing"
    assert _row(db, "key-1")["tag_state"] == "missing"          # tagged, but without an environment tag
    assert _row(db, "bucket-a")["tag_state"] == "not_checked"   # unverified type, not in the sweep

    monkeypatch.setenv("TAG_SYNC_ENABLED", "false")
    _classify()
    assert {_row(db, r)["tag_state"] for r in ("orders", "legacy", "key-1", "bucket-a")} == {"not_checked"}


def test_flag_off_discovery_and_classification_never_touch_the_new_columns(db, monkeypatch):
    _setup(db)
    monkeypatch.delenv("TAG_SYNC_ENABLED", raising=False)
    _classify()
    assert all(_row(db, r)["synced_tags"] is None for r in ("orders", "legacy", "key-1", "bucket-a"))


def test_a_failed_sweep_leaves_the_stored_result_and_classification_alone(db, monkeypatch):
    _setup(db)
    _sync(monkeypatch, ARNS)
    _classify()
    before = _row(db, "orders")
    rep, _ = _sync(monkeypatch, ARNS, ok=False)
    assert rep["written"] == 0
    _classify()
    after = _row(db, "orders")
    assert (after["tag_state"], after["synced_tags"], after["tags_synced_at"]) == \
        (before["tag_state"], before["synced_tags"], before["tags_synced_at"]) and after["tag_state"] == "ok"


def test_discovery_overwriting_tags_does_not_lose_the_synced_result(db, monkeypatch):
    _setup(db)
    _sync(monkeypatch, ARNS)
    db.x("UPDATE resources SET tags=%s WHERE aws_account_id=2 AND resource_id='orders'", (json.dumps({"queue_url": "u"}),))
    _classify()
    assert _row(db, "orders")["tag_state"] == "ok"


def test_a_result_older_than_the_max_age_is_ignored(db, monkeypatch):
    _setup(db)
    _sync(monkeypatch, ARNS)
    db.x("UPDATE resources SET tags_synced_at = DATE_SUB(NOW(), INTERVAL 7 HOUR) WHERE aws_account_id=2")
    _classify()
    assert _row(db, "orders")["tag_state"] == "not_checked"


def test_dry_run_writes_nothing(db, monkeypatch):
    _setup(db)
    rep, _ = _sync(monkeypatch, ARNS, dry_run=True)
    assert rep["written"] == 0 and _row(db, "orders")["synced_tags"] is None


def test_environment_tag_feeds_classification_only_with_its_own_flag(db, monkeypatch):
    _setup(db)
    _sync(monkeypatch, [(f"arn:aws:sqs:{REG}:{NUM}:orders", {"Environment": "dev"})])
    monkeypatch.delenv("TAG_SYNC_FEED_ENVIRONMENT", raising=False)
    _classify()
    assert db.one("SELECT env_source FROM resources WHERE resource_id='orders'")["env_source"] != "tag"
    monkeypatch.setenv("TAG_SYNC_FEED_ENVIRONMENT", "true")
    _classify()
    r = db.one("SELECT env_resolved, env_source FROM resources WHERE resource_id='orders'")
    assert (r["env_resolved"], r["env_source"]) == ("nonprod", "tag")


def _sync_direct(monkeypatch, items, looker, direct_on=True):
    monkeypatch.setenv("TAG_SYNC_ENABLED", "true")
    if direct_on:
        monkeypatch.setenv("TAG_SYNC_DIRECT_LOOKUP", "true")
    else:
        monkeypatch.delenv("TAG_SYNC_DIRECT_LOOKUP", raising=False)
    install_stub("app.db", get_connection=_connect)
    ts = load_module("app/collector/tag_sync.py")

    def accounts():
        c = _connect()
        try:
            cur = c.cursor(dictionary=True)
            cur.execute("SELECT id, account_name, account_id, default_region FROM aws_accounts WHERE id = 2")
            return cur.fetchall()
        finally:
            c.close()
    ts._get_accounts = accounts
    ts._get_session = lambda a: object()
    sweeper = lambda account, spec: {"region": spec["region"], "scope": spec["scope"], "ok": True, "pages": 1,
                                     "items": [{"arn": a, "tags": t} for a, t in items]}
    return ts.run_tag_sync(sweeper=sweeper, looker=looker)


def test_direct_lookup_proves_an_untagged_bucket_and_flag_off_does_not(db, monkeypatch):
    _setup(db)
    db.x("UPDATE resources SET region='ap-south-2' WHERE aws_account_id=2 AND resource_id='bucket-a'")
    asked = []

    def looker(account, rows):
        asked.append([r["resource_id"] for r in rows])
        return {r["id"]: {} for r in rows}, len(rows)

    _sync_direct(monkeypatch, ARNS, looker, direct_on=False)
    _classify()
    assert asked == [] and _row(db, "bucket-a")["tag_state"] == "not_checked"

    _sync_direct(monkeypatch, ARNS, looker, direct_on=True)
    assert asked == [["bucket-a"]]                    # only the undecided S3 row; the queues and the key were decided
    _classify()
    assert json.loads(_row(db, "bucket-a")["synced_tags"]) == {} and _row(db, "bucket-a")["tag_state"] == "missing"


def test_an_undecided_direct_lookup_leaves_the_bucket_not_checked(db, monkeypatch):
    _setup(db)
    _sync_direct(monkeypatch, ARNS, lambda account, rows: ({r["id"]: None for r in rows}, len(rows)))
    _classify()
    assert _row(db, "bucket-a")["tag_state"] == "not_checked" and _row(db, "bucket-a")["synced_tags"] is None


# ---- Azure / GCP: tag status from the tags discovery already stored
def _provider_rows(db):
    db.x("UPDATE aws_accounts SET provider='azure' WHERE id=1")
    db.x("UPDATE aws_accounts SET provider='gcp' WHERE id=2")
    db.resource(1, "vm", "az-vm-ok", tags={"Environment": "prod"})
    db.resource(1, "vm", "az-vm-none", tags={})
    db.resource(1, "some_generic_type", "az-gen", tags={})
    db.resource(2, "compute_instance", "gcp-vm-ok", tags={"environment": "nonprod"})
    db.resource(2, "gcs_bucket", "gcp-bkt", tags={})
    db.resource(2, "gke_node", "gcp-node", tags={})


def _state(db, rid):
    return db.one("SELECT tag_state FROM resources WHERE resource_id=%s", (rid,))["tag_state"]


def test_azure_and_gcp_get_real_status_only_with_the_flag_on(db, monkeypatch):
    _provider_rows(db)
    monkeypatch.delenv("TAG_SYNC_ENABLED", raising=False)
    _classify()
    assert {_state(db, r) for r in ("az-vm-ok", "az-vm-none", "az-gen", "gcp-vm-ok", "gcp-bkt", "gcp-node")} == {"not_checked"}
    monkeypatch.setenv("TAG_SYNC_ENABLED", "true")
    _classify()
    got = {r: _state(db, r) for r in ("az-vm-ok", "az-vm-none", "az-gen", "gcp-vm-ok", "gcp-bkt", "gcp-node")}
    assert got == {"az-vm-ok": "ok", "az-vm-none": "missing", "az-gen": "not_checked",
                   "gcp-vm-ok": "ok", "gcp-bkt": "missing", "gcp-node": "not_checked"}
    monkeypatch.setenv("TAG_SYNC_ENABLED", "false")
    _classify()
    assert _state(db, "az-vm-none") == "not_checked"                       # roll back by removing the flag
