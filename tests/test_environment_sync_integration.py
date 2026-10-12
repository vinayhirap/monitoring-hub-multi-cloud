"""
REAL-DATABASE tests for the environment sync and attached-resource inheritance
(MH_TEST_DB=1, same fixture schema as test_alert_lifecycle_integration.py).
"""
import os

import pytest

from tests.conftest import install_stub, load_module
from tests.test_alert_lifecycle_integration import _connect, db  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(not os.getenv("MH_TEST_DB"), reason="set MH_TEST_DB=1 to run real-DB tests")


def _sync(**kw):
    """Load env_sync wired to the real test DB (the fixture already creates accounts 1 and 2)."""
    install_stub("app.db", get_connection=_connect)
    return load_module("app/collector/env_sync.py").sync_resource_environments(**kw)


def _edge(db, parent, child, kind="attached_to", acct=1):
    db.x("INSERT INTO resource_relationships (aws_account_id, source_resource_id, target_resource_id, "
         "relationship_type, source) VALUES (%s,%s,%s,%s,'auto')", (acct, parent, child, kind))


def _env(db, rid):
    return db.one("SELECT env_resolved AS env, env_source AS src, env_reason AS why FROM resources "
                  "WHERE resource_id=%s", (rid,))


def test_unnamed_volume_follows_its_prod_named_instance(db):
    db.resource(1, "ec2", "i-1", name="HCS-PROD-MD-01")
    db.resource(1, "ebs", "vol-1", name=None)
    _edge(db, "i-1", "vol-1")
    _sync()
    v = _env(db, "vol-1")
    assert (v["env"], v["src"]) == ("prod", "related") and "HCS-PROD-MD-01" in v["why"]


def test_classifying_the_parent_re_evaluates_its_children(db):
    p = db.resource(1, "ec2", "i-1", name="TEST-U4RAD-PROD-ORTHANC")      # conflicting name
    db.resource(1, "ebs", "vol-1", name="TEST-U4RAD-PROD-ORTHANC")        # conflicting name too
    _edge(db, "i-1", "vol-1")
    _sync()
    assert _env(db, "i-1")["env"] == "unknown" and _env(db, "vol-1")["env"] == "unknown"

    db.x("UPDATE resources SET env_override='dev' WHERE id=%s", (p,))
    rep = _sync(resource_pks=[p])                      # only the parent is named
    assert _env(db, "i-1")["src"] == "manual"
    v = _env(db, "vol-1")
    assert (v["env"], v["src"]) == ("dev", "related")
    assert rep["applied"] == 2


def test_only_attached_to_edges_are_followed(db):
    db.resource(1, "elb", "alb-1", name="prod-alb")
    db.resource(1, "ec2", "i-9", name=None)
    _edge(db, "alb-1", "i-9", kind="routes_to")
    _sync()
    assert _env(db, "i-9")["src"] == "unknown"


def test_a_parents_account_default_is_not_passed_down(db):
    db.x("UPDATE aws_accounts SET default_env='uat' WHERE id=1")
    db.resource(1, "ec2", "i-1", name=None)
    db.resource(1, "ebs", "vol-1", name=None)
    _edge(db, "i-1", "vol-1")
    _sync()
    assert _env(db, "i-1")["src"] == "account"
    assert _env(db, "vol-1")["src"] == "account"       # its own default, not "related"


def test_children_beat_the_account_default_but_not_their_own_name(db):
    db.x("UPDATE aws_accounts SET default_env='prod' WHERE id=1")
    db.resource(1, "ec2", "i-1", name="dev-box")
    db.resource(1, "ebs", "vol-a", name=None)
    db.resource(1, "ebs", "vol-b", name="uat-vol")
    _edge(db, "i-1", "vol-a")
    _edge(db, "i-1", "vol-b")
    _sync()
    a, b = _env(db, "vol-a"), _env(db, "vol-b")
    assert (a["env"], a["src"]) == ("dev", "related")
    assert (b["env"], b["src"]) == ("uat", "name")


def test_dry_run_writes_nothing_and_a_second_run_is_a_no_op(db):
    db.resource(1, "ec2", "i-1", name="prod-a")
    db.resource(1, "ebs", "vol-1", name=None)
    _edge(db, "i-1", "vol-1")
    rep = _sync(dry_run=True)
    assert rep["changed"] == 2 and rep["applied"] == 0
    assert _env(db, "vol-1")["env"] is None
    assert _sync()["applied"] == 2
    assert _sync()["changed"] == 0


def test_same_resource_id_in_another_account_is_not_mixed_up(db):
    db.resource(1, "ec2", "i-1", name="prod-a")
    db.resource(2, "ec2", "i-1", name="dev-a")
    db.resource(2, "ebs", "vol-1", name=None)
    _edge(db, "i-1", "vol-1", acct=2)
    _sync()
    v = db.one("SELECT env_resolved AS env FROM resources WHERE resource_id='vol-1'")
    assert v["env"] == "dev"
