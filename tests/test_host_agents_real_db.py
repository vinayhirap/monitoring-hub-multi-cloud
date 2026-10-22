# tests/test_host_agents_real_db.py
"""
REAL-DATABASE tests for the host agent server foundation: the concurrency guarantees (a single-use enrollment key can
be spent exactly once under contention; two first-time enrolls of one host cannot deadlock or leave two live tokens)
and the exact SQL of every route against a real MySQL 8 / MariaDB.

    mysql -uroot -e "create database mh_test; create user 'mh'@'%' identified by 'mh'; grant all on mh_test.* to 'mh'@'%'"
    # the base schema + every migration up to and including 120 (migrate.py apply --all-pending), then:
    MH_TEST_DB=1 pytest tests/test_host_agents_real_db.py

Skipped automatically unless MH_TEST_DB is set. Env overrides: MH_TEST_DB_HOST (127.0.0.1) / _PORT (3306) /
_USER (mh) / _PASSWORD (mh) / _NAME (mh_test). The tests write only host_agent_* rows and two aws_accounts rows
(ids 990001 / 990002), which they remove again.
"""
import json
import os
import threading
from datetime import timedelta

import pytest
from fastapi import HTTPException

from tests.hostagent_support import Env, utcnow

pytestmark = pytest.mark.skipif(not os.getenv("MH_TEST_DB"), reason="set MH_TEST_DB=1 to run real-DB tests")

if os.getenv("MH_TEST_DB"):
    import mysql.connector

BASE = "/api/host-agents"
ACCT_A, ACCT_B = 990001, 990002


def _connect():
    return mysql.connector.connect(
        host=os.getenv("MH_TEST_DB_HOST", "127.0.0.1"), port=int(os.getenv("MH_TEST_DB_PORT", 3306)),
        user=os.getenv("MH_TEST_DB_USER", "mh"), password=os.getenv("MH_TEST_DB_PASSWORD", "mh"),
        database=os.getenv("MH_TEST_DB_NAME", "mh_test"), use_pure=True, autocommit=False)


def _sql(sql, params=(), fetch=False):
    conn = _connect()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


def _clean():
    for t in ("host_agent_tokens", "host_agents", "host_agent_enrollment_keys"):
        _sql(f"DELETE FROM {t}")
    _sql("DELETE FROM aws_accounts WHERE id IN (%s, %s)", (ACCT_A, ACCT_B))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("HOST_AGENT_ENABLED", "true")
    monkeypatch.delenv("HOST_AGENT_STALE_SECONDS", raising=False)
    _clean()
    for i, name in ((ACCT_A, "RealDb A"), (ACCT_B, "RealDb B")):
        _sql("INSERT INTO aws_accounts (id, account_name, account_id, role_arn) VALUES (%s, %s, %s, 'arn:test')",
             (i, name, str(i)))
    e = Env(connect=_connect)
    e.as_user("admin")
    yield e
    _clean()


def _enroll_direct(env, key, hostname):
    """Call the route function itself (no HTTP layer) so many threads can contend. -> (status, body)."""
    body = env.mod.EnrollRequest(enrollment_key=key, hostname=hostname, os="linux", arch="amd64", agent_version="0.1.0")
    try:
        return 200, env.mod.enroll(body, None)
    except HTTPException as exc:
        return exc.status_code, exc.detail


def _race(env, jobs):
    results, errors = [None] * len(jobs), []
    barrier = threading.Barrier(len(jobs))

    def run(i, key, host):
        try:
            barrier.wait(timeout=20)
            results[i] = _enroll_direct(env, key, host)
        except BaseException as exc:                  # a deadlock / driver error would land here
            errors.append(repr(exc))
    threads = [threading.Thread(target=run, args=(i, k, h)) for i, (k, h) in enumerate(jobs)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert not errors, errors
    return results


def test_single_use_key_is_spent_exactly_once_under_contention(env):
    for _ in range(5):
        key = env.make_key(account=ACCT_A, max_uses=1)
        res = _race(env, [(key["key"], f"race-{key['id']}-{i}") for i in range(16)])
        assert sorted(r[0] for r in res) == [200] + [401] * 15, res
        row = _sql("SELECT used_count FROM host_agent_enrollment_keys WHERE id = %s", (key["id"],), fetch=True)[0]
        assert row["used_count"] == 1
        assert _sql("SELECT COUNT(*) n FROM host_agents WHERE hostname LIKE %s", (f"race-{key['id']}-%",), fetch=True)[0]["n"] == 1


def test_multi_use_key_never_exceeds_max_uses_under_contention(env):
    key = env.make_key(account=ACCT_A, max_uses=3)
    res = _race(env, [(key["key"], f"multi-{i}") for i in range(12)])
    assert sorted(r[0] for r in res) == [200] * 3 + [401] * 9
    assert _sql("SELECT used_count FROM host_agent_enrollment_keys WHERE id = %s", (key["id"],), fetch=True)[0]["used_count"] == 3
    assert _sql("SELECT COUNT(*) n FROM host_agents", fetch=True)[0]["n"] == 3


def test_two_first_enrolls_of_one_host_do_not_deadlock_and_leave_one_live_token(env):
    for i in range(10):
        a, b = env.make_key(account=ACCT_A), env.make_key(account=ACCT_A)
        res = _race(env, [(a["key"], f"dup-{i}"), (b["key"], f"dup-{i}")])
        assert [r[0] for r in res] == [200, 200], res
        host = _sql("SELECT id FROM host_agents WHERE hostname = %s", (f"dup-{i}",), fetch=True)
        assert len(host) == 1
        live = _sql("SELECT COUNT(*) n FROM host_agent_tokens WHERE host_agent_id = %s AND revoked_at IS NULL",
                    (host[0]["id"],), fetch=True)[0]["n"]
        assert live == 1
        assert sum(1 for _, body in res if env.auth.authenticate_agent(body["agent_token"])) == 1


def test_expiry_is_enforced_by_the_database_clock(env):
    key = env.make_key(account=ACCT_A, hours=1)
    assert _enroll_direct(env, key["key"], "fresh")[0] == 200
    _sql("UPDATE host_agent_enrollment_keys SET expires_at = %s WHERE id = %s", (utcnow() - timedelta(seconds=5), key["id"]))
    assert _enroll_direct(env, key["key"], "late")[0] == 401
    row = _sql("SELECT expires_at FROM host_agent_enrollment_keys WHERE id = %s", (key["id"],), fetch=True)[0]
    assert row["expires_at"] < utcnow()
    k2 = env.make_key(account=ACCT_A, hours=1)
    exp = _sql("SELECT expires_at FROM host_agent_enrollment_keys WHERE id = %s", (k2["id"],), fetch=True)[0]["expires_at"]
    assert timedelta(minutes=58) < exp - utcnow() < timedelta(minutes=62)         # stored in UTC


def test_full_flow_on_real_sql_scope_state_revoke_config(env):
    ka, kb = env.make_key(account=ACCT_A, max_uses=2), env.make_key(account=ACCT_B)
    ea = env.enroll(ka["key"], "alpha").json()
    eb = env.enroll(kb["key"], "beta").json()
    _sql("UPDATE host_agents SET last_seen_at = UTC_TIMESTAMP() WHERE id = %s", (ea["host_agent_id"],))

    listed = {h["hostname"]: h for h in env.client.get(BASE).json()}
    assert listed["alpha"]["state"] == "online" and listed["beta"]["state"] == "offline" and listed["alpha"]["account"] == "RealDb A"

    env.as_user("admin", accessible={ACCT_A})
    assert [h["hostname"] for h in env.client.get(BASE).json()] == ["alpha"]
    assert [k["id"] for k in env.client.get(f"{BASE}/enrollment-keys").json()] == [ka["id"]]
    assert env.client.post(f"{BASE}/{eb['host_agent_id']}/revoke").status_code == 404
    assert env.client.patch(f"{BASE}/{eb['host_agent_id']}/config", json={"interval_seconds": 30}).status_code == 404
    assert env.client.delete(f"{BASE}/enrollment-keys/{kb['id']}").status_code == 404
    assert env.client.post(f"{BASE}/enrollment-keys", json={"aws_account_id": ACCT_B}).status_code == 404
    assert env.auth.authenticate_agent(eb["agent_token"]) is not None
    env.as_user("admin", accessible=set())
    assert env.client.get(BASE).json() == []

    env.as_user("admin")
    p = env.client.patch(f"{BASE}/{ea['host_agent_id']}/config", json={"interval_seconds": 30, "watch_services": ["nginx"]})
    assert p.status_code == 200 and p.json()["config_version"] == 2
    stored = _sql("SELECT config_json, config_version FROM host_agents WHERE id = %s", (ea["host_agent_id"],), fetch=True)[0]
    assert json.loads(stored["config_json"]) == {"watch_services": ["nginx"], "interval_seconds": 30}
    again = env.enroll(ka["key"], "alpha").json()
    assert again["host_agent_id"] == ea["host_agent_id"] and again["config"]["interval_seconds"] == 30
    assert env.auth.authenticate_agent(ea["agent_token"]) is None
    ctx = env.auth.authenticate_agent(again["agent_token"])
    assert ctx["config_version"] == 2 and ctx["aws_account_id"] == ACCT_A

    assert env.client.post(f"{BASE}/{ea['host_agent_id']}/revoke").json()["tokens_revoked"] == 1
    assert env.auth.authenticate_agent(again["agent_token"]) is None
    assert env.client.delete(f"{BASE}/enrollment-keys/{ka['id']}").status_code == 200
    assert _sql("SELECT revoked FROM host_agent_enrollment_keys WHERE id = %s", (ka["id"],), fetch=True)[0]["revoked"] == 1
    # only hashes are stored
    dump = json.dumps(_sql("SELECT * FROM host_agent_tokens", fetch=True), default=str) + \
        json.dumps(_sql("SELECT * FROM host_agent_enrollment_keys", fetch=True), default=str)
    assert ea["agent_token"] not in dump and again["agent_token"] not in dump and ka["key"] not in dump and kb["key"] not in dump


def test_deleting_a_host_cascades_its_tokens(env):
    e = env.enroll(env.make_key(account=ACCT_A)["key"], "gone").json()
    _sql("DELETE FROM host_agents WHERE id = %s", (e["host_agent_id"],))
    assert _sql("SELECT COUNT(*) n FROM host_agent_tokens", fetch=True)[0]["n"] == 0
