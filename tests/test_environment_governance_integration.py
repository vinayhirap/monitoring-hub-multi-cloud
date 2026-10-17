# tests/test_environment_governance_integration.py
"""
REAL-DATABASE tests for workstream C (MH_TEST_DB=1; same scratch DB as test_environment_sync_integration.py, with
migrations 098-100 applied on top of the stale fixture; see the harness note in the handoff).

audit_logs, users and user_group_memberships are not in the stale fixture, so this module creates minimal copies.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

from tests.conftest import install_stub, load_module
from tests.test_alert_lifecycle_integration import _connect, db  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(not os.getenv("MH_TEST_DB"), reason="set MH_TEST_DB=1 to run real-DB tests")

EXTRA_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS audit_logs (
         id BIGINT AUTO_INCREMENT PRIMARY KEY, actor VARCHAR(100), action VARCHAR(100), payload JSON,
         ip_address VARCHAR(45), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS users (
         id BIGINT AUTO_INCREMENT PRIMARY KEY, email VARCHAR(255), role VARCHAR(20) DEFAULT 'viewer', active TINYINT DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS user_group_memberships (user_id BIGINT NOT NULL, group_id BIGINT NOT NULL)""",
]


@pytest.fixture
def gov_db(db):
    for ddl in EXTRA_SCHEMA:
        db.x(ddl)
    db.x("SET FOREIGN_KEY_CHECKS=0")
    for t in ("audit_logs", "users", "user_group_memberships", "org_groups",
              "environment_compliance_snapshots", "environment_digest_log"):
        db.x(f"TRUNCATE TABLE {t}")
    db.x("SET FOREIGN_KEY_CHECKS=1")
    return db


def _wire():
    """Fresh modules wired to the real scratch DB. The sync module is registered under its dotted name (and marked
    as a stub so conftest removes it afterwards) because the governance layer imports it lazily."""
    install_stub("app.db", get_connection=_connect)
    sync = load_module("app/collector/env_sync.py")
    sync._pytest_conftest_stub = True
    install_stub("app.collector.env_sync")
    sys.modules["app.collector.env_sync"] = sync
    gov = load_module("app/env_governance_db.py")
    return sync, gov


def _api(gov, accessible=None, audit=None):
    install_stub("app.audit", write_audit=(lambda actor, action, **k: audit.append((actor, action, k))) if audit is not None else (lambda *a, **k: None))
    install_stub("app.auth.permissions", require_permission=lambda code: (lambda: None))
    install_stub("app.auth.authorization", get_accessible_account_ids=lambda user: accessible)
    install_stub("app.collector.tag_plan", DEFAULT_SOURCES=("manual", "name", "related"), build_tag_plan=lambda *a, **k: {})
    api = load_module("app/api/environments.py")
    api.gov = gov
    return api


def _edge(db, parent, child, acct=1):
    db.x("INSERT INTO resource_relationships (aws_account_id, source_resource_id, target_resource_id, "
         "relationship_type, source) VALUES (%s,%s,%s,'attached_to','auto')", (acct, parent, child))


def _row(db, rid):
    return db.one("SELECT env_resolved AS env, env_source AS src, env_warning AS warn, env_override AS ov FROM resources "
                  "WHERE resource_id=%s", (rid,))


USER = {"id": 1, "username": "vinay", "role": "admin"}


# ---- migrations ---------------------------------------------------------------------------------------

def test_migrations_098_to_100_created_what_the_code_reads(gov_db):
    cols = {r["COLUMN_NAME"] for r in gov_db.q("SELECT COLUMN_NAME FROM information_schema.columns "
                                              "WHERE table_schema = DATABASE() AND table_name='resources'")}
    assert "env_warning" in cols
    tables = {r["TABLE_NAME"] for r in gov_db.q("SELECT TABLE_NAME FROM information_schema.tables WHERE table_schema = DATABASE()")}
    assert {"environment_compliance_snapshots", "environment_digest_log"} <= tables


def test_migrations_can_be_run_twice(gov_db):
    """Idempotence: apply 098-100 again through the mysql client (the Python connector cannot run the PREPARE pattern
    cleanly). Skipped when the client is not installed."""
    import shutil
    import subprocess
    if not shutil.which("mysql"):
        pytest.skip("mysql client not installed")
    cmd = ["mysql", "-h", os.getenv("MH_TEST_DB_HOST", "127.0.0.1"), "-P", str(os.getenv("MH_TEST_DB_PORT", 3306)),
           "-u", os.getenv("MH_TEST_DB_USER", "mh"), "-p" + os.getenv("MH_TEST_DB_PASSWORD", "mh"),
           os.getenv("MH_TEST_DB_NAME", "mh_test")]
    for f in ("098_resource_env_warning.sql", "099_environment_compliance_snapshots.sql", "100_environment_digest_log.sql"):
        path = os.path.join(os.path.dirname(__file__), "..", "db", "migrations", f)
        with open(path, "rb") as fh:
            r = subprocess.run(cmd, stdin=fh, capture_output=True)
        assert r.returncode == 0, (f, r.stderr.decode())


# ---- 1. mismatch warnings -----------------------------------------------------------------------------

def test_sync_persists_warnings_and_never_changes_the_environment(gov_db):
    sync, _ = _wire()
    gov_db.resource(1, "ec2", "i-1", name="web-prod-01", tags={"Environment": "dev"})          # tag dev, name prod
    gov_db.resource(1, "ec2", "i-2", name="orders-prd-2", tags={"Environment": "prod"})        # agrees
    gov_db.resource(1, "ec2", "i-3", name="x-dev-3")
    gov_db.x("UPDATE resources SET env_override='uat' WHERE resource_id='i-3'")               # manual vs name
    rep = sync.sync_resource_environments()
    a, b, c = _row(gov_db, "i-1"), _row(gov_db, "i-2"), _row(gov_db, "i-3")
    assert (a["env"], a["src"]) == ("dev", "tag") and a["warn"].startswith("tag_vs_name:")      # the TAG still decides
    assert b["warn"] is None
    assert (c["env"], c["src"]) == ("uat", "manual") and c["warn"].startswith("manual_vs_name:")
    assert rep["warnings"] == 2 and rep["warning_applied"] == 2


def test_warning_run_is_idempotent_and_clears_when_the_evidence_is_fixed(gov_db):
    sync, _ = _wire()
    gov_db.resource(1, "ec2", "i-1", name="web-prod-01", tags={"Environment": "dev"})
    sync.sync_resource_environments()
    again = sync.sync_resource_environments()
    assert again["applied"] == 0 and again["warning_changed"] == 0 and again["warning_applied"] == 0
    gov_db.x("UPDATE resources SET tags=%s WHERE resource_id='i-1'", (json.dumps({"Environment": "prod"}),))
    fixed = sync.sync_resource_environments()
    assert _row(gov_db, "i-1")["warn"] is None and fixed["warning_applied"] == 1


def test_prod_instance_with_a_dev_volume_is_flagged_on_the_volume_and_the_volume_keeps_its_own_answer(gov_db):
    sync, _ = _wire()
    gov_db.resource(1, "ec2", "i-1", name="HCS-PROD-MD-01")
    gov_db.resource(1, "ebs", "vol-1", name="scratch-dev-vol")
    gov_db.resource(1, "ebs", "vol-2", name=None)
    _edge(gov_db, "i-1", "vol-1"); _edge(gov_db, "i-1", "vol-2")
    sync.sync_resource_environments()
    v1, v2, inst = _row(gov_db, "vol-1"), _row(gov_db, "vol-2"), _row(gov_db, "i-1")
    assert (v1["env"], v1["src"]) == ("dev", "name") and v1["warn"].startswith("volume_vs_instance:")
    assert (v2["env"], v2["src"]) == ("prod", "related") and v2["warn"] is None
    assert inst["warn"] is None


def test_list_endpoint_filters_and_summary_counts_the_mismatches(gov_db):
    sync, gov = _wire()
    gov_db.resource(1, "ec2", "i-1", name="web-prod-01", tags={"Environment": "dev"})
    gov_db.resource(1, "ec2", "i-2", name="x-dev-2")
    gov_db.x("UPDATE resources SET env_override='prod' WHERE resource_id='i-2'")
    gov_db.resource(1, "ec2", "i-3", name="fine-prod-3")
    sync.sync_resource_environments()
    api = _api(gov)
    kw = dict(env=None, source=None, tag_state=None, account_id=None, resource_type=None, region=None, q=None,
              warning_kind=None, suggest=False, limit=50, offset=0, current_user=USER)
    out = api.list_resources(view="mismatches", **kw)
    assert out["total"] == 2 and {i["resource_id"] for i in out["items"]} == {"i-1", "i-2"}
    assert {i["warning_kind"] for i in out["items"]} == {"tag_vs_name", "manual_vs_name"}
    assert all("tags" not in i for i in out["items"])
    only = api.list_resources(view="mismatches", **{**kw, "warning_kind": "tag_vs_name"})
    assert only["total"] == 1
    s = api.summary(current_user=USER)
    assert s["mismatches"] == 2 and s["mismatch_kinds"] == {"tag_vs_name": 1, "manual_vs_name": 1}


# ---- 0. account default preview -----------------------------------------------------------------------

def _seed_default_scenario(db):
    db.resource(1, "ec2", "i-1", name="HCS-PROD-MD-01")                  # by name: never changes with the default
    for i in range(2, 7):
        db.resource(1, "ec2", f"i-{i}", name=f"mystery-{i}")             # unknown now
    db.resource(1, "ebs", "vol-1", name=None)
    _edge(db, "i-1", "vol-1")
    db.resource(2, "ec2", "other-1", name="other-account")               # another account, must be untouched


def _fingerprint(db):
    return db.q("SELECT id, env_resolved, env_source, env_reason, tag_state, env_warning, env_override FROM resources ORDER BY id")


def test_preview_writes_nothing_and_matches_what_the_write_then_does(gov_db):
    sync, gov = _wire()
    _seed_default_scenario(gov_db)
    sync.sync_resource_environments()
    before = _fingerprint(gov_db)
    audit_before = gov_db.one("SELECT COUNT(*) AS n FROM audit_logs")["n"]

    prev = gov.preview_default(1, "uat")
    assert _fingerprint(gov_db) == before                                        # nothing written
    assert gov_db.one("SELECT default_env FROM aws_accounts WHERE id=1")["default_env"] is None
    assert gov_db.one("SELECT COUNT(*) AS n FROM audit_logs")["n"] == audit_before
    assert prev["new_default"] == "uat" and prev["current_default"] is None
    assert prev["environment_changes"] == 5                                      # five unknown rows become uat by account
    assert prev["by_env"] == {"prod": 2, "uat": 5} and prev["by_source"]["account"] == 5
    assert {(m["from"], m["to"]): m["count"] for m in prev["moves"]}[("unknown", "uat")] == 5

    audit = []
    api = _api(gov, audit=audit)
    out = api.set_account_default(1, request=None, payload={"env": "uat"}, current_user=USER)
    assert out["reclassified"] == prev["rows_rewritten"] == 5                    # the figure the dialog showed is the figure written
    after = {r["env_resolved"] for r in gov_db.q("SELECT env_resolved FROM resources WHERE aws_account_id=1")}
    assert after == {"prod", "uat"}
    assert audit and audit[0][1] == "Account default environment changed"        # the write still audits


def test_preview_of_the_current_default_changes_nothing(gov_db):
    sync, gov = _wire()
    _seed_default_scenario(gov_db)
    gov_db.x("UPDATE aws_accounts SET default_env='dev' WHERE id=1")
    sync.sync_resource_environments()
    prev = gov.preview_default(1, "dev")
    assert prev["unchanged"] is True and prev["rows_rewritten"] == 0 and prev["environment_changes"] == 0


def test_preview_clearing_the_default_returns_resources_to_unknown(gov_db):
    sync, gov = _wire()
    _seed_default_scenario(gov_db)
    gov_db.x("UPDATE aws_accounts SET default_env='dev' WHERE id=1")
    sync.sync_resource_environments()
    prev = gov.preview_default(1, None)
    assert prev["environment_changes"] == 5 and {(m["from"], m["to"]) for m in prev["moves"]} == {("dev", "unknown")}


def test_preview_scopes_alert_impact_to_the_account_and_to_the_flag(gov_db, monkeypatch):
    sync, gov = _wire()
    _seed_default_scenario(gov_db)
    sync.sync_resource_environments()
    now = datetime.utcnow().replace(microsecond=0)
    for rid, sev, acct in (("i-2", "WARNING", 1), ("i-3", "CRITICAL", 1), ("other-1", "WARNING", 2)):
        gov_db.x("INSERT INTO alerts (aws_account_id, resource_id, metric_name, severity, current_value, threshold, status, triggered_at) "
                 "VALUES (%s,%s,'CPUUtilization',%s,90,80,'ACTIVE',%s)", (acct, rid, sev, now))
    gov_db.x("INSERT INTO alerts (aws_account_id, resource_id, metric_name, severity, current_value, threshold, status, triggered_at, resolved_at) "
             "VALUES (1,'i-4','CPUUtilization','WARNING',90,80,'RESOLVED',%s,%s)", (now, now))

    monkeypatch.delenv("ENV_AWARE_CLASSIFICATION", raising=False)
    off = gov.preview_default(1, "dev")["alerts"]
    assert off["flag_on"] is False and off["open_alerts_affected"] == 0

    monkeypatch.setenv("ENV_AWARE_CLASSIFICATION", "true")
    monkeypatch.setenv("PRIORITY_ENABLED", "true")
    on = gov.preview_default(1, "dev")["alerts"]
    assert on["flag_on"] and on["priority_on"]
    assert on["warning_to_info"] == 1                                            # i-2 only: the other account and the resolved alert are out
    shifts = {(s["from"], s["to"]): s["count"] for s in on["priority_shifts"]}
    assert shifts == {("P2", "P4"): 1, ("P1", "P3"): 1}                          # WARNING P2->P4, CRITICAL P1->P3


def test_the_default_preview_for_an_account_the_caller_cannot_see_is_refused_by_the_route(gov_db):
    _, gov = _wire()
    api = _api(gov, accessible={2})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        api.preview_account_default(1, env="prod", current_user=USER)
    assert e.value.status_code == 403


# ---- 3. suggestions -----------------------------------------------------------------------------------

def test_suggestions_come_from_classified_siblings_and_apply_nothing(gov_db):
    sync, gov = _wire()
    for i in range(3):
        gov_db.resource(1, "ec2", f"i-{i}", name=f"HCS-PROD-MD-0{i}", tags={"Application": "orders"})
    gov_db.resource(1, "ec2", "i-u", name="mystery", tags={"Application": "orders"})           # unknown, same application
    gov_db.resource(1, "ec2", "i-n", name="lonely")                                           # no evidence at all
    gov_db.resource(2, "ec2", "j-1", name="HCS-PROD-MD-9", tags={"Application": "orders"})
    sync.sync_resource_environments()
    api = _api(gov)
    before = _fingerprint(gov_db)
    out = api.list_resources(view="needs_classification", env=None, source=None, tag_state=None, account_id=None,
                             resource_type=None, region=None, q=None, warning_kind=None, suggest=True, limit=50,
                             offset=0, current_user=USER)
    by = {i["resource_id"]: i for i in out["items"]}
    assert by["i-u"]["suggestion"]["env"] == "prod" and by["i-u"]["suggestion"]["confidence"] == "high"
    assert by["i-n"]["suggestion"] is None                                                    # no suggestion is a normal result
    assert _fingerprint(gov_db) == before                                                     # nothing applied
    assert all("tags" not in i for i in out["items"])


def test_suggestions_stay_inside_the_account(gov_db):
    sync, gov = _wire()
    for i in range(3):
        gov_db.resource(2, "ec2", f"j-{i}", name=f"HCS-PROD-MD-0{i}", tags={"Application": "orders"})
    gov_db.resource(1, "ec2", "i-u", name="mystery", tags={"Application": "orders"})
    sync.sync_resource_environments()
    api = _api(gov)
    out = api.list_resources(view="needs_classification", env=None, source=None, tag_state=None, account_id=1,
                             resource_type=None, region=None, q=None, warning_kind=None, suggest=True, limit=50,
                             offset=0, current_user=USER)
    assert out["items"][0]["suggestion"] is None


def test_accepting_a_suggestion_uses_the_audited_classify_path_and_the_history_shows_it(gov_db):
    sync, gov = _wire()
    for i in range(3):
        gov_db.resource(1, "ec2", f"i-{i}", name=f"HCS-PROD-MD-0{i}", tags={"Application": "orders"})
    pk = gov_db.resource(1, "ec2", "i-u", name="mystery", tags={"Application": "orders"})
    sync.sync_resource_environments()
    audit = []
    api = _api(gov, audit=audit)
    out = api.classify(request=None, payload={"env": "prod", "resource_ids": [pk], "via": "suggestion"}, current_user=USER)
    assert out["updated"] == 1
    assert (_row(gov_db, "i-u")["env"], _row(gov_db, "i-u")["src"]) == ("prod", "manual")
    actor, action, kw = audit[0]
    assert action == "Environment classification changed" and kw["payload"]["via"] == "suggestion"
    assert kw["payload"]["previous"][str(pk)] == ["unknown", "unknown"]
    # the same payload, stored as audit_logs would store it, drives the history
    gov_db.x("INSERT INTO audit_logs (actor, action, payload) VALUES (%s,%s,%s)", (actor, action, json.dumps(kw["payload"])))
    h = gov.resource_history(pk, None)
    ev = h["events"][0]
    assert ev["kind"] == "classified" and ev["actor"] == "vinay" and ev["to_env"] == "prod"
    assert (ev["from_env"], ev["from_source"], ev["via"]) == ("unknown", "unknown", "suggestion")
    assert h["current"]["override_by"] == "vinay" and h["current"]["env"] == "prod"


# ---- 4. history ---------------------------------------------------------------------------------------

def _audit(db, actor, action, payload, ago_minutes=0):
    db.x("INSERT INTO audit_logs (actor, action, payload, created_at) VALUES (%s,%s,%s, UTC_TIMESTAMP() - INTERVAL %s MINUTE)",
         (actor, action, json.dumps(payload), ago_minutes))


def test_history_lists_classifications_and_default_changes_newest_first(gov_db):
    _, gov = _wire()
    pk = gov_db.resource(1, "ec2", "i-1", name="x")
    other = gov_db.resource(1, "ec2", "i-2", name="y")
    _audit(gov_db, "ann", "Environment classification changed", {"env": "dev", "resource_ids": [pk, other], "account_ids": [1]}, 30)
    _audit(gov_db, "bob", "Account default environment changed", {"account_id": 1, "before": None, "after": "prod", "detail": "d"}, 20)
    _audit(gov_db, "cy", "Environment classification changed", {"env": "uat", "resource_ids": [other], "account_ids": [1]}, 10)
    _audit(gov_db, "dee", "Account default environment changed", {"account_id": 2, "before": None, "after": "dev"}, 5)
    h = gov.resource_history(pk, None)
    assert [(e["kind"], e["actor"]) for e in h["events"]] == [("default_changed", "bob"), ("classified", "ann")]
    assert h["incomplete"] is False


def test_history_says_when_a_large_batch_may_have_hidden_this_resource(gov_db):
    _, gov = _wire()
    pk = gov_db.resource(1, "ec2", "i-1", name="x")
    _audit(gov_db, "ann", "Environment classification changed",
           {"env": "dev", "resource_ids": [9001, 9002], "resource_ids_truncated": True, "account_ids": [1]}, 5)
    h = gov.resource_history(pk, None)
    assert h["events"] == [] and h["incomplete"] is True


def test_history_is_scoped_and_other_actions_are_ignored(gov_db):
    _, gov = _wire()
    pk = gov_db.resource(1, "ec2", "i-1", name="x")
    _audit(gov_db, "ann", "User deleted", {"resource_ids": [pk]})
    assert gov.resource_history(pk, None)["events"] == []
    assert gov.resource_history(pk, [2]) is None                                  # account 1 is outside the caller's scope
    assert gov.resource_history(pk, []) is None
    assert gov.resource_history(424242, None) is None


# ---- 2. compliance ------------------------------------------------------------------------------------

def _compliance_fixture(db, sync):
    db.resource(1, "ec2", "i-1", name="a-prod-1", tags={"Environment": "prod"})                      # ok, tag
    db.resource(1, "ec2", "i-2", name="b-dev-1", tags={"Name": "b"})                                  # missing, name
    db.resource(1, "ec2", "i-3", name="mystery", tags={"Env": "Production"})                          # nonstandard, tag
    db.resource(1, "s3", "bucket-1", name="bucket-1")                                                 # not_checked
    db.resource(1, "s3", "bucket-2", name="bucket-2")
    db.resource(2, "ec2", "j-1", name="j-1", tags={})                                                 # missing, unknown
    sync.sync_resource_environments()


def test_compliance_report_keeps_not_checked_separate_and_counts_by_source(gov_db):
    sync, gov = _wire()
    _compliance_fixture(gov_db, sync)
    rep = gov.compliance_report(None)
    t = rep["totals"]
    assert t["total"] == 6
    assert t["by_tag_state"]["not_checked"]["n"] == 2 and t["by_tag_state"]["missing"]["n"] == 2
    assert t["by_tag_state"]["ok"]["n"] == 1 and t["by_tag_state"]["nonstandard"]["n"] == 1
    assert t["checkable"] == 4 and t["ok_of_checkable_pct"] == 25.0
    assert t["by_source"]["tag"]["n"] == 2 and t["by_source"]["name"]["n"] == 1 and t["by_source"]["unknown"]["n"] == 3
    acct1 = [a for a in rep["accounts"] if a["account_id"] == 1][0]
    s3 = [x for x in acct1["types"] if x["resource_type"] == "s3"][0]
    assert s3["by_tag_state"]["not_checked"]["pct"] == 100.0 and s3["by_tag_state"]["missing"]["n"] == 0


def test_compliance_is_limited_to_the_allowed_accounts(gov_db):
    sync, gov = _wire()
    _compliance_fixture(gov_db, sync)
    assert gov.compliance_report([2])["totals"]["total"] == 1
    assert gov.compliance_report([])["totals"]["total"] == 0
    assert gov.compliance_report(None, account_id=1)["totals"]["total"] == 5


def test_snapshot_is_idempotent_per_day_and_feeds_the_trend(gov_db):
    sync, gov = _wire()
    _compliance_fixture(gov_db, sync)
    assert gov.take_snapshot() == 3                                              # acct1 ec2, acct1 s3, acct2 ec2
    assert gov.take_snapshot() == 3
    assert gov_db.one("SELECT COUNT(*) AS n FROM environment_compliance_snapshots")["n"] == 3
    # yesterday's snapshot, with worse numbers, makes a two-point trend
    gov_db.x("INSERT INTO environment_compliance_snapshots (snapshot_date, aws_account_id, resource_type, total, tag_missing) "
             "VALUES (CURDATE() - INTERVAL 1 DAY, 1, 'ec2', 3, 3)")
    pts = gov.compliance_trend(None, days=7)
    assert [p["tag_missing"] for p in pts] == [3, 2] and pts[-1]["tag_not_checked"] == 2
    assert pts[-1]["total"] == 6 and pts[-1]["ok_of_checkable_pct"] == 25.0
    assert gov.compliance_trend([2], days=7)[-1]["total"] == 1
    assert gov.compliance_trend([], days=7) == []


def test_snapshot_matches_the_live_report(gov_db):
    sync, gov = _wire()
    _compliance_fixture(gov_db, sync)
    gov.take_snapshot()
    live = gov.compliance_report(None)["totals"]
    snap = gov.compliance_trend(None, days=1)[-1]
    assert snap["total"] == live["total"]
    for k in ("ok", "nonstandard", "invalid", "missing", "not_checked"):
        assert snap[f"tag_{k}"] == live["by_tag_state"][k]["n"], k
    for k in ("manual", "tag", "name", "related", "account", "unknown"):
        assert snap[f"src_{k}"] == live["by_source"][k]["n"], k


def test_snapshot_hook_never_raises_and_can_be_switched_off(gov_db, monkeypatch):
    _, gov = _wire()
    monkeypatch.setenv("ENV_COMPLIANCE_SNAPSHOT", "false")
    assert gov.snapshot_after_discovery() == 0
    monkeypatch.setenv("ENV_COMPLIANCE_SNAPSHOT", "true")
    gov_db.x("DROP TABLE environment_compliance_snapshots")
    try:
        assert gov.snapshot_after_discovery() == 0                               # table missing: skipped quietly
    finally:
        sql = open(os.path.join(os.path.dirname(__file__), "..", "db", "migrations", "099_environment_compliance_snapshots.sql"),
                   encoding="utf-8").read()
        gov_db.x("\n".join(l for l in sql.splitlines() if not l.strip().startswith("--")))


# ---- 6. tagging gaps ----------------------------------------------------------------------------------

def test_tagging_gaps_use_missing_tags_and_skip_types_whose_tags_are_not_read(gov_db):
    sync, gov = _wire()
    full = {k: "x" for k in ("Environment", "Application", "Owner", "CostCenter", "Criticality", "DataClassification", "ManagedBy")}
    full["Environment"] = "prod"
    gov_db.resource(1, "ec2", "i-ok", name="complete-prod", tags=full)
    gov_db.resource(1, "ec2", "i-part", name="partial-prod", tags={"Environment": "prod", "Owner": "team"})
    gov_db.resource(1, "ec2", "i-none", name="=SUM(A1)", tags={})
    gov_db.resource(1, "s3", "bucket-1", name="bucket-1")
    gov_db.resource(2, "ec2", "j-1", name="other-account", tags={})
    sync.sync_resource_environments()
    rows, truncated = gov.tagging_gap_rows(None)
    by = {r[3]: r for r in rows}
    assert truncated is False and set(by) == {"i-part", "i-none", "j-1"}                  # complete one and the s3 bucket are absent
    assert by["i-part"][9] == "Application; CostCenter; Criticality; DataClassification; ManagedBy"
    assert by["i-none"][8] == "missing" and by["i-part"][8] == "ok"                       # a resource can be 'ok' for Environment and still lack others
    ess, _ = gov.tagging_gap_rows(None, essential_only=True)
    assert {r[3]: r[9] for r in ess}["i-part"] == "Criticality; Application"
    assert {r[3] for r in gov.tagging_gap_rows([1], account_id=1, resource_type="ec2")[0]} == {"i-part", "i-none"}
    assert gov.tagging_gap_rows([2])[0][0][3] == "j-1" and gov.tagging_gap_rows([])[0] == []


def test_the_csv_route_neutralises_a_hostile_name(gov_db):
    sync, gov = _wire()
    gov_db.resource(1, "ec2", "i-none", name="=SUM(A1)", tags={})
    sync.sync_resource_environments()
    api = _api(gov)
    r = api.tagging_gaps_csv(account_id=None, resource_type=None, essential_only=False, current_user=USER)
    text = r.body.decode("utf-8")
    assert "'=SUM(A1)" in text and ",=SUM" not in text


# ---- 5. weekly digest ---------------------------------------------------------------------------------

def _digest_world(db, owner_team="Platform"):
    db.x("UPDATE aws_accounts SET owner_team=%s WHERE id=1", (owner_team,))
    db.x("SET FOREIGN_KEY_CHECKS=0")
    db.x("INSERT INTO users (id, email, role, active) VALUES (10,'ann@example.test','editor',1),(11,'off@example.test','editor',0),"
         "(12,'','editor',1),(13,'bob@example.test','editor',1)")
    db.x("INSERT INTO org_groups (id, name, level, created_by) VALUES (1,'platform','L1',1),(2,'Other','L1',1)")
    db.x("INSERT INTO user_group_memberships (user_id, group_id) VALUES (10,1),(11,1),(12,1),(13,1),(13,2)")
    db.x("SET FOREIGN_KEY_CHECKS=1")


def _digest(monkeypatch, sent, configured=True, accessible=None):
    install_stub("app.db", get_connection=_connect)
    sync = load_module("app/collector/env_sync.py")
    sync._pytest_conftest_stub = True
    install_stub("app.collector.env_sync")
    sys.modules["app.collector.env_sync"] = sync
    install_stub("app.email.mailer", is_configured=lambda: configured, get_public_app_url=lambda: "http://cloudops.test",
                 send_email=lambda to, subj, body: sent.append((to, subj, body)) or True)
    install_stub("app.auth.authorization",
                 get_accessible_account_ids=lambda u: accessible(u) if callable(accessible) else accessible)
    monkeypatch.setenv("ENV_DIGEST_ENABLED", "true")
    monkeypatch.setenv("ENV_DIGEST_WEEKDAY", "0")
    return load_module("app/env_digest.py"), sync


NOW = datetime(2026, 10, 19, 9, 0)          # a Monday


def _unknown(db, rid, name, created=None):
    db.resource(1, "ec2", rid, name=name)
    if created:
        db.x("UPDATE resources SET created_at=%s WHERE resource_id=%s", (created, rid))


def test_digest_goes_to_active_members_of_the_group_named_like_the_owner_team(gov_db, monkeypatch):
    _digest_world(gov_db)
    sent = []
    d, sync = _digest(monkeypatch, sent)
    _unknown(gov_db, "i-1", "mystery-1", datetime.utcnow())
    sync.sync_resource_environments()
    res = d.send_weekly_digest(datetime.utcnow())
    assert res == {1: "sent"}
    assert sorted(t for t, _, _ in sent) == ["ann@example.test", "bob@example.test"]      # inactive and address-less members skipped
    subj, body = sent[0][1], sent[0][2]
    assert "1 new resource(s)" in subj and "mystery-1 (ec2" in body and "http://cloudops.test/environments" in body
    row = gov_db.one("SELECT status, new_unknown, recipients FROM environment_digest_log")
    assert (row["status"], row["new_unknown"], row["recipients"]) == ("sent", 1, 2)


def test_digest_is_sent_once_per_week_and_only_for_new_resources(gov_db, monkeypatch):
    _digest_world(gov_db)
    sent = []
    d, sync = _digest(monkeypatch, sent)
    _unknown(gov_db, "i-1", "mystery-1", datetime.utcnow())
    _unknown(gov_db, "i-old", "ancient", datetime.utcnow() - timedelta(days=60))
    sync.sync_resource_environments()
    d.send_weekly_digest(datetime.utcnow())
    n = len(sent)
    assert d.send_weekly_digest(datetime.utcnow()) == {} and len(sent) == n                # second run the same week: nothing
    assert "ancient" not in sent[0][2] and "2 resource(s) in this account are waiting in total" in sent[0][2]
    # next week, only what arrived since the last SENT digest is listed
    _unknown(gov_db, "i-2", "mystery-2", datetime.utcnow() + timedelta(seconds=5))
    sync.sync_resource_environments()
    res = d.send_weekly_digest(datetime.utcnow() + timedelta(days=7, seconds=10))
    assert res == {1: "sent"} and "mystery-2" in sent[-1][2] and "mystery-1" not in sent[-1][2]


def test_digest_with_no_matching_group_records_it_and_sends_nothing(gov_db, monkeypatch):
    _digest_world(gov_db, owner_team="No Such Team")
    sent = []
    d, sync = _digest(monkeypatch, sent)
    _unknown(gov_db, "i-1", "mystery-1", datetime.utcnow())
    sync.sync_resource_environments()
    assert d.send_weekly_digest(datetime.utcnow()) == {1: "no_recipients"} and sent == []
    assert "No Such Team" in gov_db.one("SELECT detail FROM environment_digest_log")["detail"]
    pv = d.preview(datetime.utcnow())["accounts"][0]
    assert pv["group_found"] is False and "nobody would receive" in pv["verdict"] or "already handled" in pv["verdict"]


def test_digest_members_who_cannot_see_the_account_are_not_emailed(gov_db, monkeypatch):
    _digest_world(gov_db)
    sent = []
    d, sync = _digest(monkeypatch, sent, accessible=lambda u: {2} if u["id"] == 13 else None)
    _unknown(gov_db, "i-1", "mystery-1", datetime.utcnow())
    sync.sync_resource_environments()
    d.send_weekly_digest(datetime.utcnow())
    assert [t for t, _, _ in sent] == ["ann@example.test"]                                  # bob is scoped to account 2 only


def test_digest_waits_without_smtp_and_does_not_burn_the_week(gov_db, monkeypatch):
    _digest_world(gov_db)
    sent = []
    d, sync = _digest(monkeypatch, sent, configured=False)
    _unknown(gov_db, "i-1", "mystery-1", datetime.utcnow())
    sync.sync_resource_environments()
    assert d.send_weekly_digest(datetime.utcnow()) == {} and gov_db.one("SELECT COUNT(*) AS n FROM environment_digest_log")["n"] == 0


def test_digest_preview_is_read_only_and_shows_no_addresses(gov_db, monkeypatch):
    _digest_world(gov_db)
    sent = []
    d, sync = _digest(monkeypatch, sent)
    _unknown(gov_db, "i-1", "mystery-1", datetime.utcnow())
    sync.sync_resource_environments()
    out = d.preview(datetime.utcnow())
    a = out["accounts"][0]
    assert a["verdict"] == "would be sent" and a["recipient_count"] == 2 and a["new_count"] == 1
    assert "@" not in json.dumps(out) and sent == []
    assert gov_db.one("SELECT COUNT(*) AS n FROM environment_digest_log")["n"] == 0


def test_digest_does_nothing_when_the_flag_is_off(gov_db, monkeypatch):
    _digest_world(gov_db)
    sent = []
    d, sync = _digest(monkeypatch, sent)
    monkeypatch.setenv("ENV_DIGEST_ENABLED", "false")
    _unknown(gov_db, "i-1", "mystery-1", datetime.utcnow())
    sync.sync_resource_environments()
    assert d.maybe_send_weekly_digest(NOW) == {} and sent == []
    assert gov_db.one("SELECT COUNT(*) AS n FROM environment_digest_log")["n"] == 0


# ---- 7. onboarding ------------------------------------------------------------------------------------

def test_summary_flags_recently_onboarded_accounts(gov_db):
    sync, gov = _wire()
    gov_db.x("UPDATE aws_accounts SET created_at = NOW() - INTERVAL 90 DAY WHERE id = 2")
    gov_db.resource(1, "ec2", "i-1", name="mystery")
    sync.sync_resource_environments()
    api = _api(gov)
    s = api.summary(current_user=USER)
    flags = {a["account_id"]: a["recently_onboarded"] for a in s["accounts"]}
    assert flags[1] is True and flags[2] is False
    assert [a["needs_classification"] for a in s["accounts"] if a["account_id"] == 1] == [1]
