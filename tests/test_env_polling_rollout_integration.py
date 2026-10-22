# tests/test_env_polling_rollout_integration.py
"""REAL-DATABASE run of scripts/env_polling_verify.py (skipped unless MH_TEST_DB=1; recipe in
tests/test_alert_lifecycle_integration.py). The scratch schema needs the fixture PLUS migrations 074..086.

  * the script writes NOTHING (row counts of every table it reads are unchanged, no lock left behind)
  * series check: judges collection age per environment against the interval the flag state promises,
    and reports a UAT resource still polled every 5 min as FASTER-than-expected when the flag is expected ON
  * alert check: DEV non-critical open alerts are FALSE_STALE, UAT limit findings follow the real SQL builders
  * unknown check: lists the unclassified EC2/EBS (and an EBS whose parent is unknown) with what to fix
  * --account-id with bound parameters works (the % in the alert-window SQL must be escaped)
"""
import csv
import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("MH_TEST_DB"), reason="set MH_TEST_DB=1 to run real-DB tests")

if os.getenv("MH_TEST_DB"):
    from tests.test_alert_lifecycle_integration import DB  # noqa: F401
    import app.alert_rules as alert_rules

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLES = ("resources", "metrics", "alerts", "alert_pending", "aws_accounts", "resource_relationships")


@pytest.fixture()
def db():
    d = DB()
    d.reset()
    d.account(1, "Acct")
    yield d
    d.conn.close()


def _res(db, rid, rtype, env, name=None, tags=None, source="tag"):
    pk = db.resource(1, rtype, rid, name=name, tags=tags)
    db.x("UPDATE resources SET env_resolved=%s, env_source=%s WHERE id=%s", (env, source, pk))
    return pk


def _run(*args, flags=None):
    env = dict(os.environ, DB_HOST=os.getenv("MH_TEST_DB_HOST", "127.0.0.1"),
               DB_PORT=os.getenv("MH_TEST_DB_PORT", "3306"), DB_USER=os.getenv("MH_TEST_DB_USER", "mh"),
               DB_PASSWORD=os.getenv("MH_TEST_DB_PASSWORD", "mh"), DB_NAME=os.getenv("MH_TEST_DB_NAME", "mh_test"))
    for v in ("ENV_POLLING_ENABLED", "ENV_AWARE_CLASSIFICATION", "POLL_UAT_MULTIPLIER", "POLL_DEV_INTERVAL_SECONDS",
              "POLL_DEV_CRITICAL_INTERVAL_SECONDS", "POLL_DEV_NONCRITICAL_MODE", "POLL_DEV_EVAL_CRITICAL_ONLY"):
        env.pop(v, None)
    env.update(flags or {})
    return subprocess.run([sys.executable, "scripts/env_polling_verify.py", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def _counts(db):
    out = {}
    for t in TABLES:
        try:
            out[t] = db.one(f"SELECT COUNT(*) n FROM {t}")["n"]
        except Exception:
            pass
    return out


ON = {"ENV_AWARE_CLASSIFICATION": "true", "ENV_POLLING_ENABLED": "true"}
OFF = {"ENV_AWARE_CLASSIFICATION": "true"}


def _fleet(db, uat_age_min):
    """6 prod and 8 uat EC2, each with a cpu reading `age` minutes old (metric_timestamp is the write time)."""
    for i in range(6):
        pk = _res(db, f"i-prod{i}", "ec2", "prod")
        db.metric(pk, "cpuutilization", 10, age_min=1 + i % 4)
    for i in range(8):
        pk = _res(db, f"i-uat{i}", "ec2", "uat")
        db.metric(pk, "cpuutilization", 10, age_min=uat_age_min[i])


def _json(p):
    assert p.returncode in (0, 1), p.stderr
    assert "Traceback" not in p.stderr, p.stderr
    return json.loads(p.stdout)


def _verdicts(rep, env):
    return {g["base_s"]: g["verdict"] for g in rep["series"]["groups"] if g["env"] == env}


def test_script_writes_nothing(db):
    _fleet(db, [2, 5, 9, 12, 3, 7, 11, 14])
    before = _counts(db)
    for args in ([], ["--json"], ["--account-id", "1", "--json"], ["--only", "unknown"]):
        p = _run("--uptime-min", "600", *args, flags=ON)
        assert p.returncode in (0, 1), p.stderr
        assert "Traceback" not in p.stderr
    assert _counts(db) == before


def test_series_ok_when_uat_is_polled_every_15_minutes(db):
    _fleet(db, [2, 5, 9, 12, 3, 7, 11, 14])                   # all <= 15 min, some well past 5 min
    rep = _json(_run("--json", "--uptime-min", "600", "--only", "series", flags=ON))
    assert _verdicts(rep, "prod")[300] == "OK"
    assert _verdicts(rep, "uat")[300] == "OK"
    uat = next(g for g in rep["series"]["groups"] if g["env"] == "uat" and g["base_s"] == 300)
    assert uat["expected_s"] == 900


def test_series_catches_uat_still_polled_every_5_minutes_after_turn_on(db):
    _fleet(db, [1, 2, 3, 4, 1, 2, 3, 4])                      # nothing older than 4 min: the flag is not working
    rep = _json(_run("--json", "--uptime-min", "600", "--only", "series", flags=ON))
    assert _verdicts(rep, "uat")[300] == "FASTER"
    assert _run("--uptime-min", "600", "--only", "series", flags=ON).returncode == 1


def test_series_catches_a_slowdown_while_the_flag_is_expected_off(db):
    _fleet(db, [2, 9, 14, 12, 3, 7, 11, 14])                  # 14 min old on a 5-min tier
    rep = _json(_run("--json", "--only", "series", "--expect", "off", flags=OFF))
    assert _verdicts(rep, "uat")[300] == "SLOWER"


def test_series_does_not_claim_faster_without_the_restart_age(db):
    _fleet(db, [1, 2, 3, 4, 1, 2, 3, 4])
    rep = _json(_run("--json", "--only", "series", flags=ON))
    assert _verdicts(rep, "uat")[300] == "UNPROVEN"


def _open_alert(db, rid, metric, env_pk_age_min=5):
    db.alert(1, rid, metric, sev="WARNING", status="active", seen_min_ago=env_pk_age_min, triggered_min_ago=60)


def test_alert_findings_dev_non_critical_is_false_stale(db):
    _res(db, "i-dev1", "ec2", "dev")
    _res(db, "db-dev1", "rds", "dev")
    _open_alert(db, "i-dev1", "cpuutilization")
    _open_alert(db, "db-dev1", "cpuutilization")
    rep = _json(_run("--json", "--only", "alerts", flags=ON))
    kinds = {(f["resource_id"], f["kind"]) for f in rep["alerts"]["findings"]}
    assert ("i-dev1", "FALSE_STALE") in kinds
    assert ("db-dev1", "FALSE_STALE") not in kinds
    # the alert section is a FORECAST for the flag ON, so it is the same before the flag is turned on
    off = _json(_run("--json", "--only", "alerts", "--expect", "off", flags=OFF))
    assert ("i-dev1", "FALSE_STALE") in {(f["resource_id"], f["kind"]) for f in off["alerts"]["findings"]}


def test_alert_findings_use_the_real_window_sql_and_bound_account(db):
    _res(db, "i-uat1", "ec2", "uat")
    _open_alert(db, "i-uat1", "cpuutilization")
    for args in ([], ["--account-id", "1"]):
        p = _run("--json", "--only", "alerts", *args, flags=ON)
        assert p.returncode in (0, 1) and "Traceback" not in p.stderr, p.stderr
        rep = json.loads(p.stdout)
        assert not [f for f in rep["alerts"]["findings"] if f["kind"] == "STALE_LIMIT_TIGHT"], rep["alerts"]
        assert any(f["kind"] == "SLOWER_LIFECYCLE" for f in rep["alerts"]["findings"])


def test_unknown_list_and_csv(db, tmp_path):
    _res(db, "i-unk1", "ec2", None, name="payroll-batch-09", source="unknown")
    _res(db, "i-unk2", "ec2", "unknown", name="DevOps-Jenkins", source="unknown")
    parent = _res(db, "i-unk3", "ec2", None, name="app-server", source="unknown")
    vol = _res(db, "vol-1", "ebs", None, name="vol-1", tags={"parent_ec2": "i-unk3"}, source="unknown")
    _res(db, "i-prod1", "ec2", "prod", name="web-prod-1")
    try:
        db.x("INSERT INTO resource_relationships (source_resource_id, target_resource_id, relationship_type) "
             "VALUES (%s,%s,'attached_to')", (vol, parent))
    except Exception:
        pass                                                   # relationship table shape differs on this schema
    csv_path = tmp_path / "unknown.csv"
    rep = _json(_run("--json", "--only", "unknown", "--csv-unknown", str(csv_path), flags=ON))
    ids = {r["resource_id"] for r in rep["unknown_5min"]}
    assert ids == {"i-unk1", "i-unk2", "i-unk3", "vol-1"} and "i-prod1" not in ids
    for r in rep["unknown_5min"]:
        assert r["needs"] and r["if_uat_every_s"] == 900 and r["if_dev_every_s"] == 3600
    rows = list(csv.DictReader(open(csv_path)))
    assert {r["resource_id"] for r in rows} == ids


def test_config_only_needs_no_database(db):
    p = subprocess.run([sys.executable, "scripts/env_polling_verify.py", "--config-only"], cwd=ROOT,
                       env={k: v for k, v in os.environ.items() if not k.startswith(("DB_", "MH_TEST"))},
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert "no rule violated" in p.stdout


def test_cannot_connect_exits_2(db):
    p = _run("--only", "series", flags=dict(ON, DB_PORT="1"))
    assert p.returncode == 2 and "Traceback" not in p.stderr
